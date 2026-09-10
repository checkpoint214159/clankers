# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# clankers

Operations stack for a Seeed reBot B601-DM 6-DOF arm (Damiao motors, dm-serial) + LEAP hand
(16 Dynamixel XL330-M288 over U2D2), combined via a custom adapter plate.

Training lives in the separate `dexterousmanipulation` repo (Python 3.8 / IsaacGym — frozen
toolchain). This repo never imports from it; it only consumes exported artifacts (joint maps,
checkpoints via an explicit export step).

## Commands

```bash
uv sync                                       # core (light: pyyaml/websockets/numpy only)
uv sync --extra controller                    # + dynamixel-sdk. THE PI INSTALLS THIS (ADR-0005)
uv sync --extra hardware --extra viz          # + lerobot/dynamixel-sdk/rerun — BRAIN ONLY (torch)
uv sync --extra camera                        # + opencv, for scripts/camera_debug.py
uv sync --extra teleop                        # + ai-edge-litert/opencv, for hand tracking

uv run pytest                                 # whole python suite (~130 tests, seconds)
uv run pytest packages/clankers/tests/test_tasks_runner.py::test_name   # one test
uv run pytest -k watchdog                      # by keyword
uv run ruff check .                            # lint gate — must pass

cd apps/studio && npm install
npm test                                       # vitest run
npm test -- src/lib/faults.test.js             # one test file
npm run lint && npm run typecheck              # eslint --max-warnings=0, tsc --noEmit
npm run dev                                    # :18110
```

`uv run ruff format` is **not** applied to this repo — ~18 files disagree with it and the
formatting is deliberate (aligned dict literals, hand-wrapped messages). Lint with
`ruff check`; do not run `ruff format` across the tree.

Entry points (`packages/clankers/pyproject.toml`):

| Command | What it does |
|---|---|
| `clankers-detect` | Read-only: enumerate serial adapters, broadcast-ping the hand bus. Bring-up step 0. |
| `clankers-hand-gateway [--mock\|--serial-port PATH]` | Hand WS gateway on :9003. Raw-SDK bus by default; `--via-lerobot` to cross-check against the lerobot path. |
| `clankers-task-runner [--fake\|--real]` | Task runner WS on :9010. |
| `clankers-export-config` | Regenerate `apps/studio/src/generated/robotsConfig.json` from robots.yaml. |
| `clankers-build-urdf` | Compose arm + adapter + LEAP into one URDF under `apps/studio/public/resources/clankers/`. |
| `clankers-teleop-demo [--synthetic]` | Webcam → hand retargeting demo. |

Camera work is `scripts/camera_debug.py` (Linux/V4L2) and `scripts/camera_preview.py`
(macOS). Neither is an entry point; run them directly. `camera_debug.py --list` names each
camera by USB port rather than index, which is what tells two identical cameras apart, and
its default view serves an MJPEG stream plus a control panel over HTTP so it works over
ssh/VS Code with no display.

`scripts/clankersctl <debug|task|studio|export-config>` wraps these and enforces bus
exclusivity via pidfiles in `/tmp/clankers/` (`--force` overrides, with a warning). It never
starts the arm gateway — that needs the physical Damiao device, so it prints the
`motorbridge-gateway` command for you to run in another terminal.

## Layout

- `packages/clankers/` — core package: `hand_gateway/`, `tasks/`, `safety/`, `viz/`, `urdf/`,
  `teleop/`, `config/`.
- `packages/lerobot_robot_clankers/` — LeRobot plugin (`LeapHand`, `ClankerStation`).
- `apps/studio/` — fork of motorbridge-studio (React/Vite/vitest). See `apps/studio/FORK.md`
  for provenance and what was added/deleted. If an upstream copy is checked out alongside
  this repo, treat it as read-only reference — never edit it.
- `docs/adr/0001–0005` — read before changing architecture. `docs/glossary.md` is the
  canonical vocabulary (joint index vs motor_id vs feedback_id vs servo_id vs canonical);
  naming collisions are the #1 source of silent bugs here.
- `docs/plans/bringup.md` — hardware checklist and current blockers. `docs/plans/roadmap.md`
  — what's built vs deferred.
- `data/` — gitignored, created on demand. LeRobotDataset episodes (source of truth) +
  `data/rrd/` (ephemeral, size-capped cache).

## Architecture

**One config source of truth.** `packages/clankers/src/clankers/config/robots.yaml` holds
topology, IDs, limits, gains, ports, and safety constants. `config/loader.py` parses it into
frozen dataclasses (`load_robots()`); every module reads it there. The studio never parses
YAML — it imports the generated JSON, so **run `uv run clankers-export-config` after every
robots.yaml edit**. `robots.yaml` and its schema test change together.

**One WS envelope, three services.** All of them speak the motorbridge ws_gateway shape:
`{op, req_id, ...}` → `{ok, op, req_id, data|error}`, plus unsolicited
`{type: "state"|"task"|"event", data}` pushes with no `req_id`. That's why the studio's
`WsGatewayClient` is reused verbatim across connections.

| Service | Port | Owner |
|---|---|---|
| arm ws_gateway (motorbridge, external) | 9002 | Damiao serial bus |
| hand gateway (`clankers.hand_gateway`) | 9003 | Dynamixel U2D2 bus |
| task runner (`clankers.tasks`) | 9010 | commands robots via LeRobot classes |
| studio dev server | 18110 | — |

**Brain and controller are different machines** (ADR-0005). The Pi is the *controller*: it
owns both serial buses, serves the gateways, enforces safety, and installs `--extra
controller` — `dynamixel-sdk` only, no lerobot and therefore no torch. The *brain* (your Mac,
later a GPU box) decides what to do — teleop, policies, planning, recording — and reaches the
hardware only over the WS envelope. This is not a preference: the Mac cannot open the Pi's
`/dev/ttyUSB0`, so anything brain-side is forced through WS anyway.

Consequence for the hand bus: there are **two** `HandBus` implementations against the same
servos. `sdk_bus.DynamixelBus` (protocol 2.0 direct, the default) and `bus.LerobotDynamixelBus`
(`--via-lerobot`). They must never disagree about where zero is or what an LSB means, so
everything shared — tick↔radian conversion, the X-series control table, LSB units — lives in
`hand_gateway/dynamixel_compat.py`, and a test asserts the constants match. Add a register to
one and you add it to `dynamixel_compat`, not to the bus.

**Bus exclusivity.** One process per physical serial bus per session (ADR-0002). The arm has
two separate consumers by design — motorbridge for debug, LeRobot for runtime — and that
duplication is accepted, not a bug to fix.

**Lazy seams for heavy deps.** Native/optional imports live behind exactly one module each
with clear-error getters: `clankers.hand_gateway.bus` (lerobot/dynamixel), `clankers.viz.rr_backend`
(rerun), `clankers.tasks.robot_adapter.LerobotAdapter` (lerobot), `clankers.teleop.landmarker`
(ai-edge-litert/cv2), `clankers.hand_gateway.sdk_bus` (dynamixel-sdk). Unit tests run against `MockBus` / `FakeRobot` / `NullBackend` with none
of them installed. Keep it that way — a top-level `import lerobot` anywhere in `clankers`
breaks the light test path.

**Safety is three enforced layers** (ADR-0004), outermost first: motor firmware limits →
`clankers.safety` clamps in *every* software command path (gateway ops AND `Robot.send_action`
AND the task runner's adapter, never only the UI) → heartbeat watchdog + explicit operator
fault acknowledgment. Faults never auto-clear; a tripped Dynamixel shutdown requires an
explicit `reboot` op.

**Task layer.** `tasks/library.py` loads `poses.yaml` and validates it against robots.yaml at
load time (unknown joint or out-of-limit → loud failure, not a silent clamp later). Poses are
partial; `TaskRunner` fills the rest from the robot's current position, plans a min-jerk move
(`tasks/profiles.py`), and paces frames against an injectable clock so tests settle instantly.

**Rerun viz.** `.rrd` is ephemeral and derived; LeRobotDataset episodes are archival. One
`VizSession` per process, `mode="off"` never imports rerun. Entity schema is LeRobot-compatible
and documented in `viz/session.py`.

**Hand tracking.** `teleop/landmarker.py` is a from-scratch port of mediapipe's hand graph
onto LiteRT (see Known rough edges for why). It downloads mediapipe's `hand_landmarker.task`
bundle once into `~/.cache/clankers/models` (`$CLANKERS_MODEL_DIR` overrides) and runs the two
`.tflite` models inside it. Two stages: the palm detector runs only to acquire, then the ROI
is carried frame to frame off the previous landmarks — so steady-state cost is the landmark
model alone (~45 ms, ~22 fps on this Pi at 4 threads; ~106 ms if acquisition reruns). Output
is mediapipe's 21-point layout unchanged, which is what `teleop/retarget.py` consumes.

The tracking camera is the **operator's** camera (normally the Mac's webcam), never one of
the two mounted on the hand — those face the workspace and are reserved for
`observation.images.*`. Both teleop deps ship macOS arm64 wheels and the hand gateway binds
0.0.0.0, so the intended split is tracking on the Mac, `--url ws://<pi-host>:9003` to the
gateway on the Pi. On the Pi, `--camera 0` is a hand camera and will detect nothing.

**URDF composition.** `urdf/compose.py` renames the LEAP URDF's numeric joints (`"0".."15"`) to
their robots.yaml semantic names and prefixes hand links with `hand_`, so the combined tree
speaks the same vocabulary as everything else.

## House rules

- Python ≥3.12 (lerobot 0.6 floor), managed by `uv` (workspace at repo root).
- Quaternions are `(x, y, z, w)` everywhere (rerun convention; scipy/ROS use wxyz — convert at
  boundaries only).
- EEPROM writes to Dynamixel servos are batched and explicitly confirmed (wear-limited).
  `set_mechanical_zero` is refused while torque is on and returns the previous offsets so it
  can be undone.
- Don't weaken tests to make an implementation pass.
- The distribution name `lerobot_robot_clankers` uses underscores on purpose — lerobot's
  `register_third_party_plugins()` only auto-imports dists whose metadata Name starts with
  `lerobot_robot_`. Don't hyphenate it.
- Current limit stays at 300 mA (plastic-gear XL330s) and the 70 °C temperature limit is never
  raised, until `hand.revision` is confirmed V1 vs V2.

## Calibration state (check before trusting geometry)

- `hand.calibrated: true` — finger *labels* were confirmed on hardware 2026-08-29 (servos 0-3
  index, 4-7 middle, 8-11 ring, 12-15 thumb). The per-joint **direction signs have still not
  been swept independently**; a single reversed joint would not have shown up in that check.
- `adapter.calibrated: false` — the arm-side flange geometry is measured off the STL, but the
  hand-side placement and yaw clocking are assumed. The 6-hole ring allows 60° increments;
  adjust `adapter.hand_mount.rpy[2]` if the physical build is clocked differently.
- `hand.revision: unconfirmed` — V1 vs V2 decides whether the current limit may ever be raised.
- Open bring-up blocker: servo ids 14/15 answer `clankers-detect` intermittently. `sync_read`
  addresses all 16 at once, so one dropout fails the whole telemetry read. Fix the cabling
  before trusting hand telemetry — see `docs/plans/bringup.md`.

## Known rough edges

- **mediapipe is not installable-and-runnable on this Pi, and never will be.** Only 1.0.1
  ships a linux-aarch64 wheel, and that binary is compiled with the ARMv8 crypto extensions
  enabled; this Cortex-A72 has no `aes` in `/proc/cpuinfo` Features, so the first call
  aborts the process with `FATAL ERROR: This binary was compiled with aes enabled ...
  (go/sigill-fail-fast)`. It installs fine and then dies — do not "fix" the pin. The models
  are plain `.tflite` and are unaffected, so `clankers.teleop.landmarker` runs them on
  ai-edge-litert and reimplements the graph glue mediapipe would have done (SSD anchor
  decode, palm→hand ROI, rotated crop, ROI carry-over). Keep the `camera` extra separate
  from `teleop` anyway: they have different install footprints.
- OpenCV 5.0 changed `putText` glyph advance to grow with `thickness` (a string measures
  387 px at thickness 1 and 409 px at 3), which silently breaks the draw-dark-stroke-then-
  light-fill overlay idiom — the two copies drift apart and every line renders doubled.
  `camera_debug.py` draws on a darkened band instead; `camera_preview.py` still uses the old
  idiom and will look doubled under cv2 5.
- `opencv-contrib-python`, `opencv-python`, and `opencv-python-headless` all install the same
  `cv2` package and overwrite each other; lerobot pulls headless transitively. If `import cv2`
  yields an empty namespace, repair with `uv pip uninstall opencv-python opencv-python-headless
  opencv-contrib-python && uv pip install --reinstall opencv-contrib-python`.
- The studio's Combined page is the single operating page; the Robot Arm half is preview-only
  there (the Robot Arm section owns the motorbridge bus).
