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

## Pointing the studio at a remote controller (ADR-0005)

The gateways run on the machine that owns the serial buses -- the Pi -- while the studio runs
on the brain. `127.0.0.1` is therefore only correct when the two happen to be the same
machine, which is no longer the normal case.

Set the controller's address once, when starting the dev server:

    VITE_ROBOT_HOST=192.168.2.2 npm run dev

That seeds both gateway URLs (arm :9002, hand :9003). Either can also be retyped in the
Gateways panel while disconnected; the hand URL is remembered per browser in localStorage,
so it survives a reload without needing the env var again.
