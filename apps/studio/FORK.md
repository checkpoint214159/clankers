# Fork provenance

Forked from ../../../motorbridge-studio (upstream: https://github.com/motorbridge/motorbridge-studio,
local copy, v1.1.3) on 2026-08-24 for the clankers project. Upstream copy stays untouched as reference.

Local additions: LEAP hand page (Dynamixel via clankers hand gateway :9003), dual gateway
connections, fault-taxonomy panel, Tasks page (:9010).

## Layout (2026-08-29)

Collapsed to a single operating page. The tab strip used to carry General (scan workspace +
motor cards), Robot Arm, LEAP Hand, Combined and Tasks, with a connection panel pinned above
all of them; it is now Combined + Tasks, and connecting happens inside Combined.

- `GatewayConnections` — both gateways in one card (arm :9002, hand :9003) with Connect both.
  Replaces the global `ConnectionPanel`. Calls `ensureRobotArmCards()` on arm connect, which
  the Robot Arm page used to do on mount.
- `HandGatewayProvider` — one hand websocket for the app. Several components now share the
  hand on screen at once; before, each called `useHandGateway()` and only one page being
  mounted kept that from opening several sockets.
- `PoseLibrary` / `lib/poseStore` — named whole-system poses (22 joints) in localStorage.
- `RobotArmPage` and `LeapHandPage` survive as collapsed detail sections inside Combined,
  keeping live per-joint motion, mechanical zeroing, motor params, self-check, per-servo
  telemetry and fault decoding. `RobotArmPage` takes `showViewer={false}` there so the robot
  is not rendered twice.

Deleted as unreachable: `ConnectionPanel`, `ScanWorkspace`, `MotorSection`, `MotorCards`,
`MotorDetailPanel`.
