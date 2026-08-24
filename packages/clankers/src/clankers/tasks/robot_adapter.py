"""RobotAdapter seam between the task runner and robot backends.

`FakeRobot` is a zero-dependency in-memory robot for tests and `--fake` runs.
`LerobotAdapter` wraps the real `lerobot_robot_clankers.ClankerStation` behind a lazy
import (CLAUDE.md "heavy/optional native deps" rule) so unit tests never pull in the
lerobot/torch stack.

Every adapter clamps every outgoing command through `clankers.safety.SafetyClamp`
before it reaches the backend — ADR-0004 layer 2 applies to this command path exactly
like the hand gateway and `Robot.send_action`.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from typing import Protocol

from clankers.config import ArmConfig, RobotsConfig, load_robots
from clankers.safety import SafetyClamp

logger = logging.getLogger(__name__)


def joint_names(cfg: RobotsConfig) -> list[str]:
    """Canonical joint name order: arm joints (incl. gripper while it's mounted), then hand."""
    return [j.name for j in cfg.arm.joints] + [j.name for j in cfg.hand.joints]


def _arm_safety_clamp(arm: ArmConfig) -> SafetyClamp:
    """Build an arm `SafetyClamp` the way `SafetyClamp.for_hand` builds the hand's.

    Uses the single `max_step_rad` for every arm joint; the gripper's tighter
    `max_step_rad_gripper` override isn't modeled per-joint here (`SafetyClamp` only
    carries one step limit). TODO(bringup): revisit once the LEAP adapter replaces the
    gripper and its step behavior actually matters.
    """
    return SafetyClamp(
        limits={j.name: j.limit for j in arm.joints},
        max_step=float(arm.safety["max_step_rad"]),
    )


def _clamp_split(
    arm_clamp: SafetyClamp, hand_clamp: SafetyClamp, targets: dict[str, float]
) -> dict[str, float]:
    """Route a mixed arm+hand command through the matching clamp. Unknown joints raise."""
    arm_part = {k: v for k, v in targets.items() if k in arm_clamp.limits}
    hand_part = {k: v for k, v in targets.items() if k in hand_clamp.limits}
    unknown = targets.keys() - arm_part.keys() - hand_part.keys()
    if unknown:
        raise KeyError(f"unknown joint(s): {sorted(unknown)}")
    out: dict[str, float] = {}
    if arm_part:
        out.update(arm_clamp.apply(arm_part))
    if hand_part:
        out.update(hand_clamp.apply(hand_part))
    return out


class RobotAdapter(Protocol):
    """Uniform interface the task runner drives, independent of backend.

    Joint names are the canonical names from `robots.yaml` (see `joint_names`): the
    arm's `joint1..joint6[, gripper]` plus the 16 LEAP hand joints (docs/glossary.md
    "joint index"). `get_positions()`/`send_targets()` both use `{joint_name: radians}`.
    """

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def get_positions(self) -> dict[str, float]: ...

    def send_targets(self, targets: dict[str, float]) -> dict[str, float]: ...

    def stop_safe(self) -> None:
        """Best-effort transition to a safe state after a mid-task fault (ADR-0004):
        stop commanding and, where the backend supports it, torque off. Must never raise."""
        ...


class FakeRobot:
    """In-memory robot: no hardware, no heavy deps. Backs tests and `--fake` runs.

    Starts at all-zeros clamped into the configured joint limits. Each `get_positions()`
    call advances a first-order lag toward the last commanded targets, using `clock()`
    to measure elapsed time — inject a fake clock in tests for deterministic settling
    without real sleeps.
    """

    def __init__(
        self,
        cfg: RobotsConfig | None = None,
        tau_s: float = 0.15,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if tau_s <= 0:
            raise ValueError("tau_s must be positive")
        self._cfg = cfg or load_robots()
        self._tau_s = tau_s
        self._clock = clock
        self._names = joint_names(self._cfg)
        self._arm_clamp = _arm_safety_clamp(self._cfg.arm)
        self._hand_clamp = SafetyClamp.for_hand(self._cfg.hand)
        zeros = {name: 0.0 for name in self._names}
        self._positions = _clamp_split(self._arm_clamp, self._hand_clamp, zeros)
        self._targets = dict(self._positions)
        self.stopped_safe = False
        self._connected = False
        self._last_update: float | None = None

    def connect(self) -> None:
        # Mirrors real bring-up: reset the clamps' step-tracking to measured position so
        # the first post-connect command can't be step-clamped against a stale target.
        self._arm_clamp.reset(
            {n: v for n, v in self._positions.items() if n in self._arm_clamp.limits}
        )
        self._hand_clamp.reset(
            {n: v for n, v in self._positions.items() if n in self._hand_clamp.limits}
        )
        self._last_update = self._clock()
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def stop_safe(self) -> None:
        self.stopped_safe = True  # inspected by tests; no torque to cut in-memory

    def get_positions(self) -> dict[str, float]:
        self._settle()
        return dict(self._positions)

    def send_targets(self, targets: dict[str, float]) -> dict[str, float]:
        if not self._connected:
            raise RuntimeError("FakeRobot is not connected")
        self._settle()
        clamped = _clamp_split(self._arm_clamp, self._hand_clamp, targets)
        self._targets.update(clamped)
        return clamped

    def _settle(self) -> None:
        if self._last_update is None:
            return  # never connected; nothing to advance
        now = self._clock()
        dt = now - self._last_update
        self._last_update = now
        if dt <= 0:
            return
        alpha = 1.0 - math.exp(-dt / self._tau_s)
        for name in self._names:
            self._positions[name] += (self._targets[name] - self._positions[name]) * alpha


def _build_arm_key_map(arm: ArmConfig) -> dict[str, str]:
    """Map robots.yaml arm joint names to upstream `rebot_b601_follower`'s own motor
    names (`shoulder_pan`, `elbow_flex`, ...) — the two naming schemes differ; CAN
    `motor_id` is the only stable link between them (`RebotB601FollowerConfig.motor_can_ids`
    maps its motor names to `(send_can_id, recv_can_id)`, and robots.yaml's `motor_id` is
    that same send_can_id — see docs/glossary.md "motor_id").
    """
    import dataclasses

    from lerobot.robots.rebot_b601_follower.config_rebot_b601_follower import (
        RebotB601FollowerConfig,
    )

    # Read the dataclass field's default rather than instantiating: `port` has no
    # default and isn't relevant to building this name<->CAN-id lookup.
    field = next(f for f in dataclasses.fields(RebotB601FollowerConfig) if f.name == "motor_can_ids")
    motor_can_ids: dict[str, tuple[int, int]] = field.default_factory()
    by_can_id = {send_id: name for name, (send_id, _recv_id) in motor_can_ids.items()}
    try:
        return {j.name: by_can_id[j.motor_id] for j in arm.joints}
    except KeyError as exc:
        raise ValueError(
            f"arm joint with motor_id {exc} has no matching upstream rebot_b601_follower "
            "motor; robots.yaml and lerobot's default motor_can_ids have diverged"
        ) from exc


class LerobotAdapter:
    """Wraps `lerobot_robot_clankers.ClankerStation` for real hardware.

    `ClankerStation` composes a `LeapHand` (observation/action keys `hand.<name>.pos`,
    where `<name>` matches robots.yaml's hand joint names exactly) with, once
    `hand_only=False`, upstream's `RebotB601Follower` (keys `arm.<motor>.pos`, where
    `<motor>` is upstream's own naming, not robots.yaml's `joint1..joint6` — see
    `_build_arm_key_map`).

    TODO(bringup): `hand_only=True` (the default here, matching `ClankerStationConfig`)
    means the arm isn't part of the station at all yet — no combo adapter URDF exists
    (CLAUDE.md, docs/plans/bringup.md step 4). Until then, `get_positions()` /
    `send_targets()` only cover the 16 hand joints; passing arm joint names raises.
    `hand_port` / `arm_port` are physical device paths (e.g. `/dev/tty.usbserial-*`,
    `/dev/cu.usbmodem*`), discovered per-machine (`lerobot-find-port`) and not in
    robots.yaml, so the caller must supply them.
    """

    def __init__(
        self,
        cfg: RobotsConfig | None = None,
        hand_port: str = "",
        arm_port: str | None = None,
        hand_only: bool = True,
    ) -> None:
        if not hand_only and not arm_port:
            raise ValueError("LerobotAdapter: arm_port is required when hand_only=False")
        self._cfg = cfg or load_robots()
        self._hand_port = hand_port
        self._arm_port = arm_port
        self._hand_only = hand_only
        self._arm_clamp = _arm_safety_clamp(self._cfg.arm)
        self._hand_clamp = SafetyClamp.for_hand(self._cfg.hand)
        self._arm_key_map = {} if hand_only else _build_arm_key_map(self._cfg.arm)
        self._station = None

    def _make_station(self):
        try:
            from lerobot_robot_clankers import ClankerStation, ClankerStationConfig
        except ImportError as exc:
            raise ImportError(
                "LerobotAdapter needs the 'hardware' extra and the ClankerStation robot: "
                "run `uv sync --extra hardware` (installs lerobot + dynamixel-sdk) and "
                "confirm packages/lerobot_robot_clankers exposes ClankerStation (ADR-0001)."
            ) from exc
        config = ClankerStationConfig(
            hand_port=self._hand_port,
            arm_port=self._arm_port,
            hand_only=self._hand_only,
        )
        return ClankerStation(config)

    def connect(self) -> None:
        if self._station is None:
            self._station = self._make_station()
        self._station.connect()
        positions = self.get_positions()
        self._arm_clamp.reset({n: v for n, v in positions.items() if n in self._arm_clamp.limits})
        self._hand_clamp.reset(
            {n: v for n, v in positions.items() if n in self._hand_clamp.limits}
        )

    def disconnect(self) -> None:
        if self._station is not None:
            self._station.disconnect()

    def stop_safe(self) -> None:
        """ADR-0004: after a mid-task fault, torque off rather than hold the last target.
        `disconnect()` on the station disables torque on its buses
        (disable_torque_on_disconnect defaults on). Must never raise — this runs inside
        the runner's error path."""
        try:
            self.disconnect()
        except Exception:
            logger.exception("stop_safe: disconnect failed; hardware state unknown")

    def get_positions(self) -> dict[str, float]:
        obs = self._station.get_observation()
        positions = {j.name: float(obs[f"hand.{j.name}.pos"]) for j in self._cfg.hand.joints}
        if not self._hand_only:
            positions.update(
                {
                    name: float(obs[f"arm.{motor}.pos"])
                    for name, motor in self._arm_key_map.items()
                }
            )
        return positions

    def send_targets(self, targets: dict[str, float]) -> dict[str, float]:
        clamped = _clamp_split(self._arm_clamp, self._hand_clamp, targets)
        action = {}
        for name, value in clamped.items():
            if name in self._hand_clamp.limits:
                action[f"hand.{name}.pos"] = value
            elif not self._hand_only and name in self._arm_key_map:
                action[f"arm.{self._arm_key_map[name]}.pos"] = value
            else:
                raise KeyError(f"arm joint {name!r} unreachable: station is hand_only")
        sent = self._station.send_action(action)
        arm_reverse = {v: k for k, v in self._arm_key_map.items()}
        out: dict[str, float] = {}
        for key, value in sent.items():
            if key.startswith("hand.") and key.endswith(".pos"):
                out[key.removeprefix("hand.").removesuffix(".pos")] = float(value)
            elif key.startswith("arm.") and key.endswith(".pos"):
                motor = key.removeprefix("arm.").removesuffix(".pos")
                out[arm_reverse[motor]] = float(value)
        return out
