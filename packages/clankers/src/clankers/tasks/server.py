"""Task runner WS service (ADR-0002 envelope: `{op, req_id}` -> `{ok, op, req_id, data|error}`).

Ops:
    pose_list             -> {"poses": [{"name": str}, ...], "skills": [{"name": str}, ...]}
    pose_go {name, duration_s?} -> {"name": str}   (runs in the background; watch "task" pushes)
    skill_run {name}      -> {"name": str}          (same)
    stop                  -> {}
    state_once            -> {"positions": {joint: rad}}

Pushes (no req_id):
    {"type": "task", "data": {"event": ..., "name": ..., "frac"?: ..., "message"?: ...}}
    {"type": "state", "data": {"ts": float, "positions": {joint: rad}}}  -- at 10 Hz while a
    task is running (started on "accepted", stopped on "done"/"stopped"/"error").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from typing import Any, Self

from clankers.config import RobotsConfig, load_robots

from .library import PoseLibrary, load_library
from .robot_adapter import FakeRobot, LerobotAdapter, RobotAdapter
from .runner import TaskEvent, TaskRunner

logger = logging.getLogger(__name__)

STATE_PUSH_HZ = 10.0


class TaskRunnerServer:
    """Binds one `TaskRunner` to a WS listener, broadcasting its events to every client."""

    def __init__(
        self,
        adapter: RobotAdapter,
        library: PoseLibrary | None = None,
        host: str = "0.0.0.0",
        port: int = 9010,
    ) -> None:
        self._host = host
        self._port = port
        self._library = library or load_library()
        self._clients: set[Any] = set()
        self._runner = TaskRunner(adapter, self._library, on_event=self._on_task_event)
        self._state_task: asyncio.Task | None = None
        self._server = None

    @property
    def bound_port(self) -> int:
        """Actual listening port (resolves `port=0` after `serve_forever`/`__aenter__`)."""
        if self._server is None:
            raise RuntimeError("server is not listening")
        return self._server.sockets[0].getsockname()[1]

    async def serve_forever(self) -> None:
        import websockets

        async with websockets.serve(self._handle, self._host, self._port) as server:
            self._server = server
            await asyncio.Future()

    async def __aenter__(self) -> Self:
        import websockets

        self._server = await websockets.serve(self._handle, self._host, self._port)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self._stop_state_push()

    async def _handle(self, ws: Any) -> None:
        self._clients.add(ws)
        try:
            async for raw in ws:
                await self._dispatch(ws, raw)
        finally:
            self._clients.discard(ws)

    async def _dispatch(self, ws: Any, raw: str) -> None:
        req_id = None
        op = None
        try:
            msg = json.loads(raw)
            op = msg["op"]
            req_id = msg["req_id"]
        except (TypeError, ValueError, KeyError):
            await ws.send(
                json.dumps({"ok": False, "op": op, "req_id": req_id, "error": "malformed request"})
            )
            return
        try:
            data = await self._call(op, msg)
            await ws.send(json.dumps({"ok": True, "op": op, "req_id": req_id, "data": data}))
        except Exception as exc:  # noqa: BLE001 - reported back to the client, not swallowed
            await ws.send(json.dumps({"ok": False, "op": op, "req_id": req_id, "error": str(exc)}))

    async def _call(self, op: str, msg: dict[str, Any]) -> Any:
        if op == "pose_list":
            return {
                "poses": [{"name": n} for n in self._library.pose_names],
                "skills": [{"name": n} for n in self._library.skill_names],
            }
        if op == "pose_go":
            name = msg["name"]
            duration_s = float(msg.get("duration_s", 2.0))
            asyncio.create_task(self._run_guarded(self._runner.run_pose(name, duration_s)))
            return {"name": name}
        if op == "skill_run":
            name = msg["name"]
            asyncio.create_task(self._run_guarded(self._runner.run_skill(name)))
            return {"name": name}
        if op == "stop":
            self._runner.stop()
            return {}
        if op == "state_once":
            return {"positions": self._runner.current_positions()}
        raise ValueError(f"unknown op {op!r}")

    async def _run_guarded(self, coro: Any) -> None:
        try:
            await coro
        except Exception:
            logger.exception("task failed")

    def _on_task_event(self, event: TaskEvent) -> None:
        data: dict[str, Any] = {"event": event.event, "name": event.name}
        if event.frac is not None:
            data["frac"] = event.frac
        if event.message is not None:
            data["message"] = event.message
        self._broadcast({"type": "task", "data": data})
        if event.event == "accepted":
            self._start_state_push()
        elif event.event in ("done", "stopped", "error"):
            self._stop_state_push()

    def _start_state_push(self) -> None:
        if self._state_task is None or self._state_task.done():
            self._state_task = asyncio.create_task(self._state_push_loop())

    def _stop_state_push(self) -> None:
        if self._state_task is not None:
            self._state_task.cancel()
            self._state_task = None

    async def _state_push_loop(self) -> None:
        try:
            while True:
                positions = self._runner.current_positions()
                self._broadcast({"type": "state", "data": {"ts": time.time(), "positions": positions}})
                await asyncio.sleep(1.0 / STATE_PUSH_HZ)
        except asyncio.CancelledError:
            pass

    def _broadcast(self, payload: dict[str, Any]) -> None:
        if not self._clients:
            return
        raw = json.dumps(payload)
        for ws in list(self._clients):
            asyncio.create_task(self._safe_send(ws, raw))

    async def _safe_send(self, ws: Any, raw: str) -> None:
        try:
            await ws.send(raw)
        except Exception:
            logger.debug("broadcast to a client failed; dropping it", exc_info=True)


def _build_adapter(cfg: RobotsConfig, fake: bool) -> RobotAdapter:
    return FakeRobot(cfg) if fake else LerobotAdapter(cfg)


def main() -> int:
    parser = argparse.ArgumentParser(prog="clankers-task-runner")
    parser.add_argument(
        "--fake", dest="fake", action="store_true", default=True, help="use FakeRobot (default)"
    )
    parser.add_argument(
        "--real", dest="fake", action="store_false", help="use LerobotAdapter (real hardware)"
    )
    parser.add_argument("--port", type=int, default=None, help="default: robots.yaml ports.task_runner_ws")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    cfg = load_robots()
    port = args.port if args.port is not None else cfg.ports["task_runner_ws"]
    adapter = _build_adapter(cfg, args.fake)
    server = TaskRunnerServer(adapter, host="0.0.0.0", port=port)
    logger.info("clankers-task-runner listening on :%d (%s)", port, "fake" if args.fake else "real")
    asyncio.run(server.serve_forever())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
