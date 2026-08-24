# ADR-0001: LeRobot as the data/robot/policy backbone

**Status:** Accepted · **Date:** 2026-08-24

## Context

We need a robot abstraction, dataset format, record/replay/train/eval loop, and policy zoo for
a reBot B601-DM arm (Damiao) + LEAP hand (Dynamixel). Research (Aug 2026) found LeRobot ≥0.6
speaks both buses natively: upstream `rebot_b601_follower` (with `can_adapter=damiao` — a
serial-bridge mode matching our Mac dm-serial setup, pulling in the `motorbridge` package) and
`DynamixelMotorsBus`. It also provides LeRobotDataset v3 (parquet+MP4), CLIs, Rerun
integration, plugin auto-discovery (`lerobot_robot_*` package naming), and policies benchmarked
on Mac MPS (ACT ≈43 ms/step).

## Decision

Adopt LeRobot instead of a custom stack. Our code is a plugin package
(`lerobot_robot_clankers`) with `LeapHand(Robot)` on `DynamixelMotorsBus` and
`ClankerStation(Robot)` composing the upstream arm robot + hand. LeRobotDataset v3 on a
private HF Hub repo is the source of truth for training data; Mac records + runs inference,
the WSL2 GPU box trains (Hub push/pull; rsync/Tailscale fallback).

Before writing any custom arm bus, spike `lerobot-calibrate --robot.type=rebot_b601_follower
--robot.can_adapter=damiao` with a 6-joint no-gripper config; a custom `MotorbridgeArmBus`
exists only if that fails.

## Consequences

+ record/replay/train/eval, dataset tooling, and policies for free; both buses maintained upstream.
− lerobot is a heavy dependency (torch); isolated behind optional extras so unit tests stay light.
− `rebot_b601_follower` assumes 7 motors incl. gripper — we carry a 6-joint config for post-adapter use.
− XC330-M288 may need a Dynamixel table entry upstream (small PR or local table).
