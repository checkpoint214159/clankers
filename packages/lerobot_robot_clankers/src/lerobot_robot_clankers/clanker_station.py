"""LeRobot `Robot` composing the reBot B601-DM arm + LEAP hand into one station (ADR-0001).

`hand_only=True` (default) is the pre-adapter phase: only `LeapHand` is built and driven;
`arm` stays `None` and no `arm.*` features exist. The custom hand-arm adapter isn't built
yet (URDF pending — see CLAUDE.md, docs/plans/bringup.md step 4); flip `hand_only=False`
once it is, which constructs the upstream `rebot_b601_follower` alongside the hand.

Feature/observation/action keys are namespaced per side: `arm.<motor>.pos` and
`hand.<name>.pos` (the hand's own `LeapHand.observation_features` already carry the
`hand.` prefix — only the arm's bare `<motor>.pos` keys need one added here). Lifecycle
methods (connect/disconnect/calibrate/configure/is_connected/is_calibrated) fan out to
each sub-robot, following the same delegation shape as upstream's
`lerobot.utils.bimanual.BimanualMixin` — not reused directly since that mixin hard-codes
`left_arm`/`right_arm` attribute names and always-present sides, neither of which fits an
(arm optional) + hand pair.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cached_property

from lerobot.lerobot_types import RobotAction, RobotObservation
from lerobot.robots import Robot, RobotConfig
from lerobot.utils.decorators import check_if_already_connected, check_if_not_connected

from .leap_hand import LeapHand, LeapHandConfig

logger = logging.getLogger(__name__)

_ARM_PREFIX = "arm."


@RobotConfig.register_subclass("clanker_station")
@dataclass
class ClankerStationConfig(RobotConfig):
    """Configuration for the composite station: LEAP hand (+ optional reBot B601-DM arm)."""

    hand_port: str
    arm_port: str | None = None
    # Passed straight through to the upstream rebot_b601_follower config's `can_adapter`.
    can_adapter: str = "damiao"
    hand_only: bool = True
    hand_use_mock: bool = False
    hand_allow_uncalibrated: bool = False
    disable_torque_on_disconnect: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.hand_only and not self.arm_port:
            raise ValueError("ClankerStationConfig: arm_port is required when hand_only=False")


class ClankerStation(Robot):
    """reBot B601-DM arm (optional) + LEAP hand, composed into one LeRobot robot."""

    config_class = ClankerStationConfig
    name = "clanker_station"

    def __init__(self, config: ClankerStationConfig) -> None:
        super().__init__(config)
        self.config = config

        self.hand = LeapHand(
            LeapHandConfig(
                id=f"{config.id}_hand" if config.id else None,
                calibration_dir=config.calibration_dir,
                port=config.hand_port,
                use_mock=config.hand_use_mock,
                allow_uncalibrated=config.hand_allow_uncalibrated,
                disable_torque_on_disconnect=config.disable_torque_on_disconnect,
            )
        )

        self.arm: Robot | None = None
        if not config.hand_only:
            # Lazy: RebotB601Follower needs the `motorbridge` package (not installed in
            # hand_only dev/test environments); importing it eagerly would break every
            # hand_only=True use of this module.
            from lerobot.robots.rebot_b601_follower import (
                RebotB601Follower,
                RebotB601FollowerRobotConfig,
            )

            self.arm = RebotB601Follower(
                RebotB601FollowerRobotConfig(
                    id=f"{config.id}_arm" if config.id else None,
                    calibration_dir=config.calibration_dir,
                    port=config.arm_port,
                    can_adapter=config.can_adapter,
                    disable_torque_on_disconnect=config.disable_torque_on_disconnect,
                )
            )

    @cached_property
    def observation_features(self) -> dict[str, type | tuple]:
        features: dict[str, type | tuple] = dict(self.hand.observation_features)
        if self.arm is not None:
            features.update({_ARM_PREFIX + k: v for k, v in self.arm.observation_features.items()})
        return features

    @cached_property
    def action_features(self) -> dict[str, type]:
        features: dict[str, type] = dict(self.hand.action_features)
        if self.arm is not None:
            features.update({_ARM_PREFIX + k: v for k, v in self.arm.action_features.items()})
        return features

    @property
    def is_connected(self) -> bool:
        return self.hand.is_connected and (self.arm is None or self.arm.is_connected)

    @property
    def is_calibrated(self) -> bool:
        return self.hand.is_calibrated and (self.arm is None or self.arm.is_calibrated)

    @check_if_already_connected
    def connect(self, calibrate: bool = True) -> None:
        self.hand.connect(calibrate)
        if self.arm is not None:
            self.arm.connect(calibrate)
        logger.info(f"{self} connected.")

    def calibrate(self) -> None:
        self.hand.calibrate()
        if self.arm is not None:
            self.arm.calibrate()

    def configure(self) -> None:
        self.hand.configure()
        if self.arm is not None:
            self.arm.configure()

    @check_if_not_connected
    def get_observation(self) -> RobotObservation:
        obs: RobotObservation = dict(self.hand.get_observation())
        if self.arm is not None:
            obs.update({_ARM_PREFIX + k: v for k, v in self.arm.get_observation().items()})
        return obs

    @check_if_not_connected
    def send_action(self, action: RobotAction) -> RobotAction:
        hand_action = {k: v for k, v in action.items() if k.startswith("hand.")}
        sent: RobotAction = dict(self.hand.send_action(hand_action))

        if self.arm is not None:
            arm_action = {
                k.removeprefix(_ARM_PREFIX): v
                for k, v in action.items()
                if k.startswith(_ARM_PREFIX)
            }
            sent.update({_ARM_PREFIX + k: v for k, v in self.arm.send_action(arm_action).items()})

        return sent

    @check_if_not_connected
    def disconnect(self) -> None:
        self.hand.disconnect()
        if self.arm is not None:
            self.arm.disconnect()
        logger.info(f"{self} disconnected.")
