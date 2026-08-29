"""WS server for the hand gateway (ADR-0002): the studio's `WsGatewayClient` speaks this
verbatim on a second connection to :9003, reusing the motorbridge ws_gateway envelope.

Request:  {"op": str, "req_id": int, ...payload}
Success:  {"ok": true,  "op": str, "req_id": int, "data": {...}}
Error:    {"ok": false, "op": str, "req_id": int, "error": str}
Push:     {"type": "state" | "task" | "event", "data": {...}}  (no req_id)

Op handling is synchronous (bus I/O is a short blocking serial round-trip on a debug-rate
gateway; consistent with the one-process-per-bus model in ADR-0002).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection

from clankers.config import load_robots

from .bus import HandBus, LerobotDynamixelBus, MockBus
from .service import GatewayError, GatewayService

logger = logging.getLogger(__name__)


class _Connection:
    __slots__ = ("state_stream_enabled", "ws")

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.state_stream_enabled = False


class HandGatewayServer:
    def __init__(self, service: GatewayService, host: str = "0.0.0.0", port: int = 9003) -> None:
        self.service = service
        self.host = host
        self.port = port
        self._connections: dict[ServerConnection, _Connection] = {}
        self._ws_server: Any = None
        self._push_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._last_health: list[dict[str, Any]] | None = None
        self._last_health_ts: float = 0.0
        self.service.on_event = self._on_service_event

    async def start(self) -> None:
        self._ws_server = await websockets.serve(self._handler, self.host, self.port)
        sockets = self._ws_server.sockets
        if sockets:
            self.port = sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for attr in ("_push_task", "_watchdog_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                setattr(self, attr, None)
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Future()
        finally:
            await self.stop()

    # -- per-connection dispatch -----------------------------------------------------------

    async def _handler(self, ws: ServerConnection) -> None:
        conn = _Connection(ws)
        self._connections[ws] = conn
        try:
            async for raw in ws:
                await self._dispatch(conn, raw)
        finally:
            self._connections.pop(ws, None)

    async def _dispatch(self, conn: _Connection, raw: Any) -> None:
        req_id = None
        op = None
        try:
            msg = json.loads(raw)
            op = msg.get("op")
            req_id = msg.get("req_id")
            data = self._call_op(conn, op, msg)
            await conn.ws.send(json.dumps({"ok": True, "op": op, "req_id": req_id, "data": data}))
        except GatewayError as exc:
            await conn.ws.send(json.dumps({"ok": False, "op": op, "req_id": req_id, "error": str(exc)}))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            await conn.ws.send(
                json.dumps({"ok": False, "op": op, "req_id": req_id, "error": f"bad request: {exc}"})
            )
        except Exception as exc:  # never crash the connection on an unexpected op-handler bug
            logger.exception("unhandled error in op %r", op)
            await conn.ws.send(
                json.dumps({"ok": False, "op": op, "req_id": req_id, "error": f"internal error: {exc}"})
            )

    def _call_op(self, conn: _Connection, op: str | None, msg: dict[str, Any]) -> dict[str, Any]:
        svc = self.service
        if op == "capabilities":
            return svc.capabilities()
        if op == "scan":
            return svc.scan(baud=msg.get("baud"))
        if op == "enable":
            result = svc.enable(servo_ids=msg.get("servo_ids"))
            self._ensure_watchdog_loop()
            return result
        if op == "disable":
            return svc.disable(servo_ids=msg.get("servo_ids"))
        if op == "pos":
            return svc.pos(targets=msg["targets"])
        if op == "jog":
            return svc.jog(servo_id=int(msg["servo_id"]), delta_rad=float(msg["delta_rad"]))
        if op == "state_once":
            return svc.state_once()
        if op == "state_stream":
            enabled = bool(msg.get("enabled", True))
            result = svc.state_stream(enabled)
            conn.state_stream_enabled = enabled
            self._ensure_push_loop()
            return result
        if op == "error_status":
            return svc.error_status()
        if op == "reboot":
            return svc.reboot(servo_id=int(msg["servo_id"]))
        if op == "set_current_limit":
            return svc.set_current_limit(ma=msg["ma"])
        if op == "heartbeat":
            return svc.heartbeat()
        raise GatewayError(f"unknown op {op!r}")

    # -- watchdog supervision, independent of any state-stream subscriber --------------------

    def _ensure_watchdog_loop(self) -> None:
        """Run whenever torque is enabled: a client that enables and dies without ever
        subscribing to state_stream must still trip the watchdog (ADR-0004 layer 3)."""
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.ensure_future(self._watchdog_loop())

    async def _watchdog_loop(self) -> None:
        interval = float(self.service.hand_cfg.safety["heartbeat_interval_s"])
        while self.service.watchdog.armed:
            self.service.watchdog.check()  # on_trip (if any) fires synchronously here
            await asyncio.sleep(interval)

    # -- periodic state/health push, shared across subscribed connections -------------------

    def _ensure_push_loop(self) -> None:
        if self._push_task is None and any(c.state_stream_enabled for c in self._connections.values()):
            self._push_task = asyncio.ensure_future(self._push_loop())

    async def _push_loop(self) -> None:
        poll = self.service.hand_cfg.poll
        joints_dt = 1.0 / float(poll["state_hz"])
        health_dt = 1.0 / float(poll["health_hz"])
        try:
            while any(c.state_stream_enabled for c in self._connections.values()):
                start = time.monotonic()
                self.service.watchdog.check()  # on_trip (if any) fires synchronously here

                joints = self.service.joints_payload(self.service.bus.read_state())
                if self._last_health is None or (start - self._last_health_ts) >= health_dt:
                    self._last_health = self.service.health_payload(self.service.bus.read_health())
                    self._last_health_ts = start

                msg = {"type": "state", "data": {"ts": start, "joints": joints, "health": self._last_health}}
                await self._broadcast(json.dumps(msg), subscribers_only=True)

                elapsed = time.monotonic() - start
                await asyncio.sleep(max(0.0, joints_dt - elapsed))
        finally:
            self._push_task = None

    def _on_service_event(self, event: dict[str, Any]) -> None:
        # Invoked synchronously from within `_watchdog_loop`/`_push_loop` (the callers of
        # watchdog.check()); a task is always running there, so scheduling a broadcast is safe.
        payload = json.dumps({"type": "event", "data": event})
        asyncio.ensure_future(self._broadcast(payload, subscribers_only=False))

    async def _broadcast(self, payload: str, *, subscribers_only: bool) -> None:
        dead = []
        for ws, conn in list(self._connections.items()):
            if subscribers_only and not conn.state_stream_enabled:
                continue
            try:
                await ws.send(payload)
            except websockets.ConnectionClosed:
                dead.append(ws)
        for ws in dead:
            self._connections.pop(ws, None)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="clankers-hand-gateway", description="LEAP hand debug WS gateway (ADR-0002)."
    )
    parser.add_argument("--mock", action="store_true", help="use the in-process MockBus")
    parser.add_argument("--serial-port", default=None, help="U2D2 serial device; required without --mock")
    parser.add_argument("--port", type=int, default=None, help="override ports.hand_gateway_ws")
    parser.add_argument(
        "--baud", type=int, default=None, help="override hand.baud from robots.yaml (real bus only)"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--allow-uncalibrated",
        action="store_true",
        help="allow the `pos` op before hand.calibrated is true (bring-up escape hatch; prefer `jog`)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level)

    cfg = load_robots()
    port = args.port if args.port is not None else cfg.ports["hand_gateway_ws"]

    bus: HandBus
    if args.mock:
        bus = MockBus(cfg.hand)
    else:
        if not args.serial_port:
            parser.error("--serial-port is required without --mock")
        bus = LerobotDynamixelBus(args.serial_port, cfg.hand, baudrate=args.baud)

    bus.connect()
    try:
        service = GatewayService(bus, cfg.hand, allow_uncalibrated=args.allow_uncalibrated)
        server = HandGatewayServer(service, host=args.host, port=port)
        logger.info("hand gateway listening on %s:%s (mock=%s)", args.host, port, args.mock)
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        pass
    finally:
        bus.disconnect()


if __name__ == "__main__":
    main()
