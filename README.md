# clankers

Operate, visualize, and command a **Seeed reBot B601-DM** 6-DOF arm (Damiao) + **LEAP hand**
(16× Dynamixel) — debug UI, Rerun telemetry, and a click-to-run task layer on a LeRobot backbone.

Training lives in the separate `dexterousmanipulation` repo. This repo is the operations stack.

## The three pillars

1. **Debug UI** — `apps/studio` (fork of motorbridge-studio) talking to two gateways:
   the stock motorbridge ws_gateway for the arm (:9002) and `clankers-hand-gateway` for the
   hand (:9003, same WS envelope).
2. **Rerun viz** — `clankers.viz`: toggleable live/record logging with one entity schema;
   LeRobotDataset episodes are the archive, `.rrd` is an ephemeral size-capped cache.
3. **Task runner** — `clankers-task-runner` (:9010): pose/skill library + policy skills,
   click a card in the studio and the robot does the thing.

## Quick start

```bash
uv sync                          # core (light)
uv sync --extra hardware --extra viz   # + lerobot/dynamixel/rerun for real use

uv run pytest                    # python tests
cd apps/studio && npm install && npm test && npm run dev   # studio on :18110

# what's plugged in? (read-only: enumerates adapters + broadcast-pings the hand bus)
uv run clankers-detect

# combined arm + adapter + LEAP hand model (regenerate after editing `adapter:` in robots.yaml):
uv run clankers-build-urdf     # -> apps/studio/public/resources/clankers/, "Combined" page in the studio

# hand gateway (mock bus, no hardware needed):
uv run clankers-hand-gateway --mock
# task runner against a fake robot:
uv run clankers-task-runner --fake

# webcam hand teleop (uv sync --extra teleop first; gateway must allow `pos` pre-calibration):
uv run clankers-hand-gateway --mock --allow-uncalibrated
uv run clankers-teleop-demo            # camera + preview; press c with an open hand, q quits
uv run clankers-teleop-demo --synthetic  # no camera: open<->curl wave
```

Hardware bring-up: read `docs/plans/bringup.md` first — the hand joint map is provisional
until torque-off calibration. Architecture decisions: `docs/adr/`.
