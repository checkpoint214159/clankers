# ADR-0005: Brain and controller are separate machines

**Status:** Accepted · **Date:** 2026-09-10 · **Amends:** ADR-0002

## Context

Both serial buses are physically attached to the Pi: the Damiao bridge for the arm and the
U2D2 for the hand. Until now the Pi was also expected to run everything that *decides* what
the robot should do, because `hand_gateway.bus` reached the hardware through LeRobot's
`DynamixelMotorsBus` (ADR-0002) and the task runner drove `ClankerStation` directly.

That made `lerobot` a hard requirement for touching a servo, and `lerobot` depends on torch.
On this Pi 4 the consequence was concrete and absurd: `uv run clankers-detect` — a read-only
broadcast ping — told the operator to `uv sync --extra hardware`, which installed torch and
the NVIDIA CUDA runtime onto a 4-core ARM board with no GPU and 15 GB of storage, for the
sake of a serial ping. Cache and venv reached 1.7 GB.

The perception work forced the same boundary from the other side. Hand-tracking teleop takes
its input from the *operator's* camera, which is on the laptop, not from the two
workspace-facing cameras bolted to the hand. The operator's machine was already going to be
where intent is formed.

## Decision

Split the stack by what each machine physically owns.

- **The controller** (the Pi) owns the serial buses and nothing else. It runs the hand
  gateway on :9003 and motorbridge's arm gateway on :9002, applies the ADR-0004 safety
  layers, and executes motion. It installs `--extra controller`: `dynamixel-sdk` and nothing
  further. No lerobot, no torch.
- **The brain** (a laptop, or later a GPU box) decides what should happen — teleop
  retargeting, policies, planning, training, recording — and reaches the hardware *only*
  through the WS envelope. It installs whatever it needs; weight there is fine.
- **`clankers.hand_gateway.sdk_bus.DynamixelBus`** speaks Dynamixel protocol 2.0 directly and
  is the default bus for `clankers-hand-gateway`. `LerobotDynamixelBus` stays, behind
  `--via-lerobot`, for cross-checking the two paths and for same-machine use.

This amends ADR-0002's "one Dynamixel implementation" clause: there are now two, and that is
a deliberate trade. See Consequences.

## Consequences

+ The controller install is ~1 MB of pure Python on top of the core deps. `clankers-detect`
  and the real hand gateway both run on a Pi with no torch anywhere near it.
+ The boundary is honest about physics: with the brain on another machine, *every* hardware
  access is already forced through WS, because the Mac cannot open `/dev/ttyUSB0` on the Pi.
  Nothing can accidentally depend on being co-located.
+ The operator camera question answers itself — tracking runs where the operator is.
− **Two Dynamixel implementations that must not drift.** This is the real cost. Mitigations:
  both implement the same `HandBus` ABC; everything they could disagree about lives in
  `hand_gateway.dynamixel_compat` (tick↔radian conversion, the X-series control table, LSB
  units); and `test_hand_sdk_bus.py` asserts the shared constants are equal in both. A
  register name or a unit that exists in only one of them is a bug.
− `DynamixelBus` is not yet hardware-validated (docs/plans/bringup.md). It is tested against
  a fake register file, which pins the wire format but not the servos' actual behaviour.
− The task runner still needs lerobot, so it is currently brain-side only. Making it
  controller-side would mean a `RobotAdapter` implemented over WS rather than over LeRobot
  classes; the Protocol seam for that already exists in `tasks/robot_adapter.py`.
