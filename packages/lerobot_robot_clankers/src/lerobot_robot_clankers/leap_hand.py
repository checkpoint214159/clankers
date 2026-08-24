"""LeRobot `Robot` for the LEAP hand: 16 Dynamixel XC330 servos over a U2D2 (ADR-0001).

Topology (servo IDs, joint names, radian limits) is never duplicated here — it always
comes from `clankers.config.load_robots().hand`, the single source of truth (robots.yaml).
Every command goes through `clankers.safety.SafetyClamp` before it reaches the bus
(ADR-0004 layer 2): joint-limit clamp + per-command max-step clamp.

`hand.calibrated` (robots.yaml) is PROVISIONAL until hardware bring-up confirms servo
IDs/signs (see docs/plans/bringup.md). `connect()` always succeeds — it only opens the
bus/reads state — but torque is left disabled and `send_action` refuses to move the hand
until either `hand.calibrated: true` or `LeapHandConfig.allow_uncalibrated=True` is set,
mirroring the same rule the hand gateway (`clankers.hand_gateway`) enforces on its ops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Protocol

from clankers.config import HandConfig, load_robots
from clankers.hand_gateway import dynamixel_compat
from clankers.safety import SafetyClamp
from lerobot.lerobot_types import RobotAction, RobotObservation
from lerobot.motors import Motor, MotorNormMode
from lerobot.robots import Robot, RobotConfig
from lerobot.utils.decorators import check_if_already_connected, check_if_not_connected

logger = logging.getLogger(__name__)

# XC330-M288 uses the standard Dynamixel X-series 4096-count encoder (one full turn).
# Ticks are centered on the mechanical mid-point (half-turn homing convention, matching
# `DynamixelMotorsBus.set_half_turn_homings`): raw tick 2048 == 0 rad. Every hand joint
# limit in robots.yaml is well inside +/-pi rad, so this never wraps.
_TICKS_PER_REV = dynamixel_compat.TICKS_PER_REV
_CENTER_TICK = dynamixel_compat.CENTER_TICK
_XC330_M288_MODEL_NUMBER = dynamixel_compat.XC330_M288_MODEL_NUMBER

# Tick<->radian conversion and the local xc330-m288 model-table patch are shared with the
# debug gateway's LerobotDynamixelBus via clankers.hand_gateway.dynamixel_compat — one
# source of truth for "where is zero" across both bus consumers (ADR-0002).
_ticks_to_rad = dynamixel_compat.ticks_to_rad
_rad_to_ticks = dynamixel_compat.rad_to_ticks
_patch_xc330_m288_table = dynamixel_compat.patch_xc330_m288_table



class HandNotCalibratedError(RuntimeError):
    """Raised by `LeapHand.send_action` when robots.yaml has `hand.calibrated: false`
    and `LeapHandConfig.allow_uncalibrated` was not set."""


class _MinimalMotorsBus(Protocol):
    """The subset of `lerobot.motors.motors_bus.SerialMotorsBus` `LeapHand` relies on."""

    motors: dict[str, Motor]

    @property
    def is_connected(self) -> bool: ...
    def connect(self, handshake: bool = True) -> None: ...
    def disconnect(self, disable_torque: bool = True) -> None: ...
    def enable_torque(self, motors: Any = None, num_retry: int = 0) -> None: ...
    def disable_torque(self, motors: Any = None, num_retry: int = 0) -> None: ...
    def configure_motors(self) -> None: ...
    def sync_read(
        self, data_name: str, motors: Any = None, *, normalize: bool = True, num_retry: int = 0
    ) -> dict[str, int]: ...
    def sync_write(
        self, data_name: str, values: Any, *, normalize: bool = True, num_retry: int = 0
    ) -> None: ...


class _MockDynamixelBus:
    """In-memory stand-in for `DynamixelMotorsBus`, selected by `LeapHandConfig.use_mock`.

    No serial I/O, no `dynamixel-sdk` dependency: lets `LeapHand` run end-to-end (dev
    loops, demos, tests) without a U2D2 attached. Every joint starts at raw tick
    `_CENTER_TICK` (0 rad).
    """

    def __init__(self, port: str, motors: dict[str, Motor]) -> None:
        self.port = port
        self.motors = motors
        self._connected = False
        self._ticks: dict[str, int] = dict.fromkeys(motors, _CENTER_TICK)
        self._current: dict[str, int] = dict.fromkeys(motors, 0)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self, handshake: bool = True) -> None:
        self._connected = True

    def disconnect(self, disable_torque: bool = True) -> None:
        self._connected = False

    def enable_torque(self, motors: Any = None, num_retry: int = 0) -> None:
        pass

    def disable_torque(self, motors: Any = None, num_retry: int = 0) -> None:
        pass

    def configure_motors(self) -> None:
        pass

    def _names(self, motors: Any) -> list[str]:
        if motors is None:
            return list(self.motors)
        if isinstance(motors, str):
            return [motors]
        return list(motors)

    def sync_read(
        self, data_name: str, motors: Any = None, *, normalize: bool = True, num_retry: int = 0
    ) -> dict[str, int]:
        names = self._names(motors)
        if data_name == "Present_Position":
            return {name: self._ticks[name] for name in names}
        if data_name == "Present_Current":
            return {name: self._current[name] for name in names}
        raise KeyError(f"_MockDynamixelBus does not model '{data_name}'")

    def sync_write(
        self, data_name: str, values: Any, *, normalize: bool = True, num_retry: int = 0
    ) -> None:
        if data_name != "Goal_Position":
            raise KeyError(f"_MockDynamixelBus does not model '{data_name}'")
        if isinstance(values, (int, float)):
            values = dict.fromkeys(self.motors, values)
        for name, tick in values.items():
            self._ticks[name] = int(tick)


@RobotConfig.register_subclass("leap_hand")
@dataclass
class LeapHandConfig(RobotConfig):
    """Configuration for the LEAP hand robot plugin.

    Servo IDs, joint names, and limits are NOT here — they come from
    `clankers.config.load_robots().hand` (robots.yaml), so there is exactly one place to
    update the (still-provisional) joint map.
    """

    port: str
    use_mock: bool = False
    # Mirrors the hand gateway's rule: robots.yaml `hand.calibrated: false` blocks motion
    # (send_action) until this is explicitly set, even though connect()/get_observation
    # always work. Never flip the default — set it per-instantiation once bring-up confirms
    # the servo map (docs/plans/bringup.md).
    allow_uncalibrated: bool = False
    disable_torque_on_disconnect: bool = True


class LeapHand(Robot):
    """LEAP hand (16 Dynamixel XC330 over U2D2)."""

    config_class = LeapHandConfig
    name = "leap_hand"

    def __init__(self, config: LeapHandConfig) -> None:
        super().__init__(config)
        self.config = config
        self._hand_cfg: HandConfig = load_robots().hand
        self._clamp = SafetyClamp.for_hand(self._hand_cfg)
        self._motors: dict[str, Motor] = {
            joint.name: Motor(
                id=joint.servo_id,
                model=self._hand_cfg.motor_model,
                # Unused: LeapHand always reads/writes the bus with normalize=False and
                # does its own tick<->radian conversion (see _ticks_to_rad/_rad_to_ticks)
                # since lerobot's own normalization needs a completed per-servo
                # calibration file, which bring-up (docs/plans/bringup.md) hasn't run yet.
                norm_mode=MotorNormMode.DEGREES,
            )
            for joint in self._hand_cfg.joints
        }
        self.bus: _MinimalMotorsBus = self._make_bus()

    def _make_bus(self) -> _MinimalMotorsBus:
        if self.config.use_mock:
            return _MockDynamixelBus(self.config.port, self._motors)

        _patch_xc330_m288_table()
        from lerobot.motors.dynamixel import DynamixelMotorsBus

        return DynamixelMotorsBus(port=self.config.port, motors=self._motors)

    @cached_property
    def observation_features(self) -> dict[str, type]:
        return {f"hand.{joint.name}.pos": float for joint in self._hand_cfg.joints}

    @cached_property
    def action_features(self) -> dict[str, type]:
        return dict(self.observation_features)

    @property
    def is_connected(self) -> bool:
        return self.bus.is_connected

    @check_if_already_connected
    def connect(self, calibrate: bool = True) -> None:
        self.bus.connect()
        if calibrate and not self.is_calibrated:
            self.calibrate()
        self.configure()
        self._clamp.reset(self._read_positions_rad())
        logger.info(f"{self} connected.")

    @property
    def is_calibrated(self) -> bool:
        # LeRobot's own per-servo homing-offset calibration file doesn't apply to this
        # plugin (see the norm_mode comment in __init__): whether the servo_id/sign map
        # can be trusted is tracked by robots.yaml's `hand.calibrated` flag instead, which
        # `send_action` checks directly. Per the Robot ABC docstring: "Should be always
        # True if not applicable".
        return True

    def calibrate(self) -> None:
        """No-op: see `is_calibrated`. Bring-up calibration lives outside LeRobot's
        file-based mechanism (docs/plans/bringup.md), driven by robots.yaml edits."""

    def configure(self) -> None:
        """Write minimal runtime bus config and gate torque on the calibrated flag.

        Torque is left OFF unless `hand.calibrated` or `allow_uncalibrated` is set, so an
        uncalibrated hand stays in the safe read-only state bring-up step 1 expects — even
        though `send_action` independently re-checks the same flag (ADR-0004: multiple
        layers, never rely on just one).
        """
        self.bus.configure_motors()
        self.bus.disable_torque()
        if self._hand_cfg.calibrated or self.config.allow_uncalibrated:
            self.bus.enable_torque()

    def _read_positions_rad(self) -> dict[str, float]:
        raw = self.bus.sync_read("Present_Position", normalize=False)
        return {name: _ticks_to_rad(int(ticks)) for name, ticks in raw.items()}

    @check_if_not_connected
    def get_observation(self) -> RobotObservation:
        # Present_Current isn't surfaced here: ADR-0003 puts per-joint current/temp/fault
        # under its own `telemetry/<side>/<joint>/*` entity, separate from
        # `observation.joint_state.hand` — keep this dict 1:1 with observation_features.
        return {f"hand.{name}.pos": rad for name, rad in self._read_positions_rad().items()}

    @check_if_not_connected
    def send_action(self, action: RobotAction) -> RobotAction:
        if not (self._hand_cfg.calibrated or self.config.allow_uncalibrated):
            raise HandNotCalibratedError(
                "robots.yaml hand.calibrated is false and LeapHandConfig.allow_uncalibrated "
                "is not set; refusing to move an unverified servo map "
                "(see docs/plans/bringup.md)."
            )

        targets: dict[str, float] = {}
        for key, value in action.items():
            if key.startswith("hand.") and key.endswith(".pos"):
                targets[key.removeprefix("hand.").removesuffix(".pos")] = float(value)

        clamped = self._clamp.apply(targets)
        ticks = {name: _rad_to_ticks(rad) for name, rad in clamped.items()}
        self.bus.sync_write("Goal_Position", ticks, normalize=False)

        return {f"hand.{name}.pos": rad for name, rad in clamped.items()}

    @check_if_not_connected
    def disconnect(self) -> None:
        self.bus.disconnect(self.config.disable_torque_on_disconnect)
        logger.info(f"{self} disconnected.")
