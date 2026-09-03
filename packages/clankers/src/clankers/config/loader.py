"""Load and validate robots.yaml into typed dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def robots_yaml_path() -> Path:
    return Path(__file__).parent / "robots.yaml"


@dataclass(frozen=True)
class JointLimit:
    min: float
    max: float

    def __post_init__(self) -> None:
        if self.min > self.max:
            raise ValueError(f"joint limit min {self.min} > max {self.max}")

    def clamp(self, value: float) -> float:
        return max(self.min, min(self.max, value))


@dataclass(frozen=True)
class ArmJoint:
    joint: int
    name: str
    motor_id: int
    feedback_id: int
    model: str
    limit: JointLimit
    gains: dict[str, float]


@dataclass(frozen=True)
class HandJoint:
    canonical: int
    servo_id: int
    name: str
    finger: str
    limit: JointLimit


@dataclass(frozen=True)
class ArmConfig:
    name: str
    vendor: str
    transport: str
    serial_baud: int
    lerobot: dict[str, Any]
    joints: list[ArmJoint]
    safety: dict[str, Any]
    urdf: dict[str, Any]


@dataclass(frozen=True)
class HandConfig:
    name: str
    vendor: str
    protocol: str
    transport: str
    baud: int
    revision: str
    calibrated: bool
    motor_model: str
    current_limit_ma: int
    temperature_limit_c: int
    poll: dict[str, float]
    profile: dict[str, float]
    joints: list[HandJoint]
    safety: dict[str, Any]

    def joint_by_servo_id(self, servo_id: int) -> HandJoint:
        for j in self.joints:
            if j.servo_id == servo_id:
                return j
        raise KeyError(f"no hand joint with servo_id {servo_id}")


@dataclass(frozen=True)
class RobotsConfig:
    schema_version: int
    ports: dict[str, int]
    arm: ArmConfig
    hand: HandConfig
    control: dict[str, float]
    viz: dict[str, Any]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def _limit(d: dict[str, Any]) -> JointLimit:
    return JointLimit(min=float(d["min"]), max=float(d["max"]))


def load_robots(path: Path | None = None) -> RobotsConfig:
    p = path or robots_yaml_path()
    raw = yaml.safe_load(p.read_text())

    if raw.get("schema_version") != 1:
        raise ValueError(f"unsupported robots.yaml schema_version: {raw.get('schema_version')}")

    arm_raw = raw["arm"]
    arm = ArmConfig(
        name=arm_raw["name"],
        vendor=arm_raw["vendor"],
        transport=arm_raw["transport"],
        serial_baud=int(arm_raw["serial_baud"]),
        lerobot=arm_raw["lerobot"],
        joints=[
            ArmJoint(
                joint=int(j["joint"]),
                name=j["name"],
                motor_id=int(j["motor_id"]),
                feedback_id=int(j["feedback_id"]),
                model=j["model"],
                limit=_limit(j["limit"]),
                gains={k: float(v) for k, v in j["gains"].items()},
            )
            for j in arm_raw["joints"]
        ],
        safety=arm_raw["safety"],
        urdf=arm_raw["urdf"],
    )

    hand_raw = raw["hand"]
    hand = HandConfig(
        name=hand_raw["name"],
        vendor=hand_raw["vendor"],
        protocol=str(hand_raw["protocol"]),
        transport=hand_raw["transport"],
        baud=int(hand_raw["baud"]),
        revision=hand_raw["revision"],
        calibrated=bool(hand_raw["calibrated"]),
        motor_model=hand_raw["motor_model"],
        current_limit_ma=int(hand_raw["current_limit_ma"]),
        temperature_limit_c=int(hand_raw["temperature_limit_c"]),
        poll={k: float(v) for k, v in hand_raw["poll"].items()},
        profile={k: float(v) for k, v in (hand_raw.get("profile") or {}).items()},
        joints=[
            HandJoint(
                canonical=int(j["canonical"]),
                servo_id=int(j["servo_id"]),
                name=j["name"],
                finger=j["finger"],
                limit=_limit(j["limit"]),
            )
            for j in hand_raw["joints"]
        ],
        safety=hand_raw["safety"],
    )

    _validate(arm, hand)

    return RobotsConfig(
        schema_version=raw["schema_version"],
        ports={k: int(v) for k, v in raw["ports"].items()},
        arm=arm,
        hand=hand,
        control=raw["control"],
        viz=raw["viz"],
        raw=raw,
    )


def _validate(arm: ArmConfig, hand: HandConfig) -> None:
    if len(hand.joints) != 16:
        raise ValueError(f"hand must have 16 joints, got {len(hand.joints)}")
    canonicals = [j.canonical for j in hand.joints]
    if canonicals != list(range(16)):
        raise ValueError("hand joints must be listed in canonical order 0..15")
    servo_ids = [j.servo_id for j in hand.joints]
    if len(set(servo_ids)) != 16:
        raise ValueError("duplicate servo_id in hand joints")

    if len(arm.joints) != 7:
        raise ValueError(f"arm must have 7 joints (6 + gripper), got {len(arm.joints)}")
    motor_ids = [j.motor_id for j in arm.joints]
    if len(set(motor_ids)) != 7:
        raise ValueError("duplicate motor_id in arm joints")
    for j in arm.joints:
        # Damiao convention; a Master ID of 0x00 is invalid and bricks feedback routing.
        if j.feedback_id != 0x10 + j.motor_id:
            raise ValueError(
                f"arm joint {j.joint}: feedback_id {j.feedback_id:#x} != 0x10 + motor_id"
            )
