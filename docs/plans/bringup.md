# Hardware bring-up checklist (hand first, then arm spike, then combo)

## 0. Before touching hardware
- [ ] Confirm LEAP hand revision (V1 vs V2) — decides current-limit policy (robots.yaml `hand.revision`).
- [ ] Physical e-stop / power cutoff for the hand PSU within reach.
- [ ] `uv sync --extra hardware` (pulls lerobot + dynamixel-sdk).

## 1. Hand — torque-off identification (safe)
- [x] Detection (2026-08-24, `clankers-detect`, both adapters on one hub): **all 16 servos
      answered at 4M baud, IDs 0–15, model XL330-M288** (not the assumed XC330 — robots.yaml
      updated). Arm scan same session: joints 0x01–0x06 present; gripper 0x07 no reply.
- [x] Servo IDs 0–15 already assigned. Keep torque OFF.
- [ ] Move each finger joint by hand; record servo_id ↔ semantic joint ↔ sign in the studio hand page.
- [ ] Init the dexmanip submodule; read `leap_hand_rot.py` ~lines 990–1000 for
      `sim_to_real_indices` / `real_to_sim_indices`; reconcile with observed mapping.
- [ ] Update `robots.yaml` hand joints (servo_id, signs, any limit corrections); set `hand.calibrated: true`.
- [ ] Configure firmware: current limit 300 mA, verify temperature limit 70 °C, shutdown register defaults.

## 2. Hand — torque-on smoke
- [ ] Single-servo jog within limits from the studio; verify clamps by commanding past a limit (must clip).
- [ ] Kill the studio mid-hold → watchdog torques off within `4 × heartbeat`.
- [ ] 10-minute hold at a curled pose; watch temp/current live; no servo above 55 °C.

## 3. Arm — LeRobot path spike (plan step 6b)
- [ ] `lerobot-calibrate --robot.type=rebot_b601_follower --robot.can_adapter=damiao --robot.port=<dm-serial>`
      (7-motor stock config while the gripper is still mounted).
- [ ] If it fails, capture the exact error before considering a custom bus.

## 4. Combo (blocked on adapter)
- [ ] Adapter URDF → combined URDF; set `arm.urdf.end_effector_link`.
- [ ] 6-joint no-gripper arm config (`arm.lerobot.use_gripper: false`).
- [ ] TTL daisy-chain check: sustained multi-finger load with the hand powered through the
      adapter run — watch far-end servo voltage/current for sag.
