# ADR-0003: Rerun schema + data lifecycle

**Status:** Accepted · **Date:** 2026-08-24
**Supersedes/extends:** dexmanip ADR-0001 (Rerun as visualization) — same philosophy, adapted
from windowed training snapshots to live hardware sessions.

## Context

Pillar 2 needs toggleable visualization of teleop/inference/replay with bounded storage.
Rerun facts (2026): built-in URDF importer + `rr.urdf`; `Transform3D(parent_frame=,
child_frame=)`; `set_sinks()` records and streams simultaneously; `--memory-limit` on viewer
and gRPC server; `rerun rrd optimize`; **.rrd is NOT stable across minor versions**; web-viewer
npm package must match the SDK version; uncompressed images dominate file size.

## Decision

- **.rrd is ephemeral/derived. LeRobotDataset v3 episodes are the archival source of truth.**
  .rrd lives in `data/rrd/`, size-capped (config `viz.rrd_cache_max_gb`), GC'd oldest-first
  with failures retained longest; regenerable from the dataset on demand.
- One viz module: `clankers.viz` — `init(mode=off|live|record|both)` via `set_sinks()`,
  gated by a single flag; every process that logs uses it (gateway, task runner, record).
- Entity schema (LeRobot-compatible): `observation.joint_state.arm`,
  `observation.joint_state.hand`, `action.arm`, `action.hand`, later
  `observation.images.<camera>`; robot geometry under `robot/` (URDF importer, `static=True`,
  joint updates via `Transform3D` parent/child frames); per-joint health under
  `telemetry/<side>/<joint>/{temp,current,voltage,fault}` so post-mortems correlate faults
  with motion.
- `rerun-sdk` pinned to one minor (config `viz.rerun_sdk_pin`); the studio's web-viewer npm
  package version must match. `--memory-limit` set explicitly everywhere (config
  `viz.memory_limit`); browsers cap far lower (~2 GiB wasm) — don't rely on the web viewer for
  long sessions.
- Camera frames (when they arrive): JPEG/video-encode before logging, never raw.

## Consequences

+ Storage bounded by design; cross-version .rrd fragility neutralized; one schema across
  teleop/inference/replay/training review.
− Regenerating .rrd from episodes needs a small converter script (lerobot↔rerun tooling exists).
