# Roadmap

Phases from the founding plan (2026-08-24). Phase 0–1 software is built; hardware steps are
gated on the physical rig (see [bringup.md](bringup.md)).

## Done (software)
- Repo scaffold, config source-of-truth (`robots.yaml` + JSON export), glossary, ADRs 0001–0004.
- Hand gateway (:9003, mock + lerobot-bus backends), watchdog + clamps in every command path.
- Studio fork: LEAP Hand page (scan/enable/jog/telemetry/faults), Tasks page, dual WS connections.
- `clankers.viz` (rerun live/record/both, `.rrd` cache GC), `clankers.tasks` (pose/skill library,
  min-jerk runner, WS service), `lerobot_robot_clankers` plugin (LeapHand + ClankerStation,
  local xc330-m288 table entry), `clankersctl`.

## Next (needs hardware on the bench)
1. Hand bring-up per [bringup.md](bringup.md) → set `hand.calibrated: true`, real servo_ids/signs.
2. Arm spike: `lerobot-calibrate --robot.type=rebot_b601_follower --robot.can_adapter=damiao`
   (ADR-0001; only build a custom bus if this fails).
3. First recorded episodes (proprio-only) → private HF Hub dataset → ACT training on the GPU box
   → `lerobot-eval` on the Mac. Proves the whole loop.

## Later (deferred by design)
- Debug-UI power features: control-table editor, EEPROM backup/restore, firmware-recovery flow,
  packet monitor, bus-health panel (RTT/latency-timer/error counters), temp/current trend alerts.
- Combined arm+hand URDF (blocked on adapter) → studio viewer + rerun scene.
- Cameras (`observation.images.*` reserved) → MediaPipe + dex-retargeting teleoperator plugin.
- Cartesian moves: pink differential IK + reachability grading (waypoint_validate-style, see
  motorbridge-studio `/simu` protocol as the template).
- Grasp library (BODex-precomputed / hand-authored; SpringGrasp fallback), FoundationPose/SAM-6D,
  SmolVLA for language-conditioned skills, rl_games checkpoint exporter for the dexmanip GRU policy.
