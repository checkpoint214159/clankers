"""WS round-trip tests for clankers.tasks.server, over an ephemeral localhost port."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
import websockets
from clankers.config import load_robots
from clankers.tasks.library import load_library
from clankers.tasks.robot_adapter import FakeRobot
from clankers.tasks.server import TaskRunnerServer

CFG = load_robots()
LIB = load_library(CFG)


async def _recv_matching(ws: Any, predicate: Any, timeout: float = 3.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("no matching message received before timeout")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        msg = json.loads(raw)
        if predicate(msg):
            return msg


async def test_pose_list_returns_starter_poses_and_skills() -> None:
    async with (
        TaskRunnerServer(FakeRobot(CFG), library=LIB, host="127.0.0.1", port=0) as server,
        websockets.connect(f"ws://127.0.0.1:{server.bound_port}") as client,
    ):
        await client.send(json.dumps({"op": "pose_list", "req_id": 1}))
        reply = json.loads(await client.recv())

    assert reply["ok"] is True
    assert reply["op"] == "pose_list"
    assert reply["req_id"] == 1
    pose_names = {p["name"] for p in reply["data"]["poses"]}
    skill_names = {s["name"] for s in reply["data"]["skills"]}
    assert {"home", "hand_open", "hand_curl"} <= pose_names
    assert {"wave_fingers", "demo_open_close"} <= skill_names


async def test_state_once_returns_current_positions() -> None:
    async with (
        TaskRunnerServer(FakeRobot(CFG), library=LIB, host="127.0.0.1", port=0) as server,
        websockets.connect(f"ws://127.0.0.1:{server.bound_port}") as client,
    ):
        await client.send(json.dumps({"op": "state_once", "req_id": 7}))
        reply = json.loads(await client.recv())

    assert reply["ok"] is True
    assert reply["data"]["positions"]["joint1"] == pytest.approx(0.0)
    assert set(reply["data"]["positions"]) >= {j.name for j in CFG.hand.joints}


async def test_pose_go_runs_in_background_and_pushes_task_lifecycle_events() -> None:
    async with (
        TaskRunnerServer(FakeRobot(CFG), library=LIB, host="127.0.0.1", port=0) as server,
        websockets.connect(f"ws://127.0.0.1:{server.bound_port}") as client,
    ):
        await client.send(
            json.dumps({"op": "pose_go", "name": "home", "duration_s": 0.05, "req_id": 2})
        )
        reply = await _recv_matching(client, lambda m: m.get("req_id") == 2)
        assert reply == {"ok": True, "op": "pose_go", "req_id": 2, "data": {"name": "home"}}

        seen: list[str] = []
        while not seen or seen[-1] not in ("done", "stopped", "error"):
            msg = await _recv_matching(client, lambda m: m.get("type") == "task")
            seen.append(msg["data"]["event"])

    assert seen[0] == "accepted"
    assert seen[-1] == "done"


async def test_stop_op_acks_even_with_no_task_running() -> None:
    async with (
        TaskRunnerServer(FakeRobot(CFG), library=LIB, host="127.0.0.1", port=0) as server,
        websockets.connect(f"ws://127.0.0.1:{server.bound_port}") as client,
    ):
        await client.send(json.dumps({"op": "stop", "req_id": 9}))
        reply = json.loads(await client.recv())

    assert reply == {"ok": True, "op": "stop", "req_id": 9, "data": {}}


async def test_unknown_op_returns_error_envelope() -> None:
    async with (
        TaskRunnerServer(FakeRobot(CFG), library=LIB, host="127.0.0.1", port=0) as server,
        websockets.connect(f"ws://127.0.0.1:{server.bound_port}") as client,
    ):
        await client.send(json.dumps({"op": "not_a_real_op", "req_id": 5}))
        reply = json.loads(await client.recv())

    assert reply["ok"] is False
    assert reply["req_id"] == 5
    assert "error" in reply
