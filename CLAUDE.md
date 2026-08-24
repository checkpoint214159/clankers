# clankers

Operations stack for a Seeed reBot B601-DM 6-DOF arm (Damiao motors, dm-serial) + LEAP hand
(16 Dynamixel XC330 over U2D2), to be combined via a custom adapter (URDF pending).

Training lives in the separate `dexterousmanipulation` repo (Python 3.8 / IsaacGym — frozen
toolchain). This repo never imports from it; it only consumes exported artifacts (joint maps,
checkpoints via an explicit export step).

## Layout

- `packages/clankers/` — core Python package (hand gateway, rerun viz, task runner, safety).
- `packages/lerobot_robot_clankers/` — LeRobot plugin (auto-discovered via `lerobot_robot_*` naming).
- `apps/studio/` — fork of motorbridge-studio (React/Vite). Upstream reference lives at
  `../motorbridge-studio` — never edit that one.
- `packages/clankers/src/clankers/config/robots.yaml` — single source of truth for topology,
  IDs, limits, gains. The studio consumes a generated JSON copy; regenerate with
  `uv run clankers-export-config`.
- `docs/adr/` — decisions. Read 0001–0004 before changing architecture.
- `data/` — gitignored. LeRobotDataset episodes (source of truth) + `data/rrd/` (ephemeral cache).

## Port map

| Service | Port | Owner |
|---|---|---|
| arm ws_gateway (motorbridge, existing) | 9002 | Damiao serial bus |
| hand gateway (this repo) | 9003 | Dynamixel U2D2 bus |
| task runner | 9010 | commands robots via LeRobot classes |
| studio dev server | 18110 | — |

Serial ports are exclusive: one process per bus at a time. `scripts/clankersctl` switches
modes (debug | teleop | record | infer | task); never open the same bus from two processes.

## House rules

- Python ≥3.12 (lerobot 0.6 floor), managed by `uv` (workspace at repo root). Run tests: `uv run pytest`.
- Studio: `cd apps/studio && npm test` (vitest), `npm run dev` (port 18110).
- Heavy/optional native deps are isolated behind one module with clear-error getters
  (pattern from dexmanip `sim_backend.py`): `clankers.hand_gateway.bus` for lerobot/dynamixel,
  `clankers.viz.rr_backend` for rerun.
- Safety clamps live in `clankers.safety` and are called from every command path
  (gateway ops AND `Robot.send_action`) — never only in the UI.
- Don't weaken tests to make an implementation pass; update `robots.yaml` and its schema
  test together.
- Quaternions are `(x, y, z, w)` everywhere in this repo (rerun convention; scipy/ROS use wxyz — convert at boundaries).
- EEPROM writes to Dynamixel servos are batched + explicitly confirmed (wear-limited).
- The hand joint map in robots.yaml is PROVISIONAL until hardware bring-up confirms
  servo IDs/signs (see docs/plans/bringup.md). Do not enable torque-on motion paths that
  trust the map before `hand.calibrated: true`.
