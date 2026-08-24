# ADR-0002: Two debug gateways, exclusive bus ownership, one WS envelope

**Status:** Accepted · **Date:** 2026-08-24

## Context

motorbridge's ws_gateway is one-bus-one-process by design and has no Dynamixel backend (none
planned; its vendor layer is CAN-shaped). The hand is a different physical bus (U2D2
USB-serial, Dynamixel protocol 2.0). The studio frontend assumes a single WS connection.

## Decision

- **Arm debug:** keep the stock motorbridge ws_gateway on :9002 (pinned version; treat its op
  surface as a moving target — reuse the studio's capability-fallback pattern).
- **Hand debug:** new `clankers.hand_gateway` on :9003 speaking the same JSON envelope
  (`{op, req_id, …}` → `{ok, op, req_id, data|error}` + `type:"state"` push) so the studio's
  `WsGatewayClient` is reused verbatim on a second connection.
- **Bus I/O dedup:** hand_gateway uses LeRobot's `DynamixelMotorsBus` as its bus layer (via a
  lazy-import seam, `clankers.hand_gateway.bus`), so scan/enable/pos/sync semantics have ONE
  implementation shared with the LeRobot runtime. The gateway adds only the WS envelope,
  debug ops, and watchdog.
- **Exclusivity:** one process per serial bus per session. `clankersctl` switches modes
  (debug | teleop | record | infer | task). Studio UX during a non-debug session: debug pages
  grey out with a "session owns the bus" banner; the task runner pushes live joint state on
  :9010 so the Tasks page stays informative.

## Consequences

+ Studio client code reused; one Dynamixel implementation; arm path untouched.
− Two gateway processes to run (wrapped by clankersctl).
− Accepted residual duplication: the arm has motorbridge (debug) and LeRobot (runtime) as
  separate consumers — established upstream pattern, not worth unifying.
