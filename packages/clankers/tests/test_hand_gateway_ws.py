"""Protocol round-trip tests: a real websockets client against HandGatewayServer + MockBus.

No hardware, no lerobot import — `clankers.hand_gateway.bus` only imports lerobot lazily
inside `LerobotDynamixelBus.connect()`, which these tests never call.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json

import pytest
import websockets
from clankers.config import HandConfig, load_robots
from clankers.hand_gateway.bus import MockBus
from clankers.hand_gateway.server import HandGatewayServer
from clankers.hand_gateway.service import GatewayService

CFG = load_robots()


@contextlib.asynccontextmanager
async def _gateway(*, allow_uncalibrated: bool = False, hand_cfg: HandConfig | None = None):
    cfg_hand = hand_cfg or CFG.hand
    bus = MockBus(cfg_hand)
    bus.connect()
    service = GatewayService(bus, cfg_hand, allow_uncalibrated=allow_uncalibrated)
    server = HandGatewayServer(service, host="127.0.0.1", port=0)
    await server.start()
    try:
        yield server, service, bus
    finally:
        await server.stop()
        bus.disconnect()


class _WsClient:
    """Thin req/resp helper: matches responses by req_id, skipping unrelated pushes."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self._req_id = 0

    async def call(self, op: str, **payload) -> dict:
        self._req_id += 1
        req_id = self._req_id
        await self.ws.send(json.dumps({"op": op, "req_id": req_id, **payload}))
        async with asyncio.timeout(5.0):
            while True:
                msg = json.loads(await self.ws.recv())
                if msg.get("req_id") == req_id:
                    return msg


async def _recv_push(ws, type_: str, timeout: float = 5.0) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == type_:
                return msg


async def test_capabilities_reports_api_version_and_ops() -> None:
    async with (
        _gateway() as (server, _service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        resp = await _WsClient(ws).call("capabilities")
        assert resp["ok"] is True
        assert resp["data"]["api_version"] == "clankers-hand-v1"
        assert resp["data"]["vendor"] == "dynamixel"
        assert {"scan", "enable", "pos", "jog", "heartbeat"} <= set(resp["data"]["ops"])


async def test_scan_and_enable_round_trip() -> None:
    async with (
        _gateway() as (server, _service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        client = _WsClient(ws)
        scan_resp = await client.call("scan")
        assert scan_resp["ok"] is True
        assert len(scan_resp["data"]["hits"]) == 16
        assert scan_resp["data"]["duplicates"] == []

        enable_resp = await client.call("enable")
        assert enable_resp["ok"] is True
        assert sorted(enable_resp["data"]["servo_ids"]) == list(range(16))


async def test_unknown_op_returns_error_envelope() -> None:
    async with (
        _gateway() as (server, _service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        resp = await _WsClient(ws).call("not_a_real_op")
        assert resp["ok"] is False
        assert "not_a_real_op" in resp["error"]


async def test_pos_clamps_target_past_the_joint_limit() -> None:
    async with (
        _gateway(allow_uncalibrated=True) as (server, service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        client = _WsClient(ws)
        joint = service.hand_cfg.joints[0]

        over = await client.call("pos", targets={joint.name: joint.limit.max + 5.0})
        assert over["ok"] is True
        # Fresh joint, no prior commanded target: only the limit clamp is in play, so the
        # result is exactly the limit (not just "somewhere under it").
        assert over["data"]["targets"][joint.name] == pytest.approx(joint.limit.max)

        under = await client.call("pos", targets={joint.name: joint.limit.min - 5.0})
        assert under["ok"] is True
        assert under["data"]["targets"][joint.name] >= joint.limit.min


async def test_pos_refused_while_uncalibrated_but_jog_still_works() -> None:
    async with (
        _gateway(allow_uncalibrated=False) as (server, service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        assert service.hand_cfg.calibrated is False  # robots.yaml: PROVISIONAL until bring-up
        client = _WsClient(ws)
        await client.call("enable")
        joint = service.hand_cfg.joints[2]

        pos_resp = await client.call("pos", targets={joint.name: 0.1})
        assert pos_resp["ok"] is False
        assert "calibrated" in pos_resp["error"]

        jog_resp = await client.call("jog", servo_id=joint.servo_id, delta_rad=0.05)
        assert jog_resp["ok"] is True
        assert jog_resp["data"]["target"] == pytest.approx(0.05)


async def test_jog_clips_to_max_step_and_joint_limit() -> None:
    async with (
        _gateway() as (server, service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        client = _WsClient(ws)
        joint = service.hand_cfg.joints[0]
        max_step = service.hand_cfg.safety["max_step_rad"]

        resp = await client.call("jog", servo_id=joint.servo_id, delta_rad=999.0)
        assert resp["ok"] is True
        assert resp["data"]["target"] == pytest.approx(min(max_step, joint.limit.max))


async def test_error_status_decodes_injected_fault_bits() -> None:
    async with (
        _gateway() as (server, _service, bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        client = _WsClient(ws)
        bus.inject_fault(5, (1 << 0) | (1 << 5))  # undervoltage + overload, addr-70 bits

        resp = await client.call("error_status")
        assert resp["ok"] is True
        faults = resp["data"]["faults"]
        assert set(faults["5"]) == {"undervoltage", "overload"}
        assert faults["0"] == []


async def test_set_current_limit_hard_refuses_above_550ma() -> None:
    async with (
        _gateway() as (server, _service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        resp = await _WsClient(ws).call("set_current_limit", ma=600)
        assert resp["ok"] is False
        assert "550" in resp["error"]


async def test_set_current_limit_warns_above_configured_but_under_hard_cap() -> None:
    async with (
        _gateway() as (server, service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        resp = await _WsClient(ws).call("set_current_limit", ma=400)
        assert resp["ok"] is True
        assert resp["data"]["current_limit_ma"] == 400.0
        assert "warning" in resp["data"]
        assert service.hand_cfg.current_limit_ma < 400


async def test_heartbeat_feeds_watchdog_without_tripping() -> None:
    async with (
        _gateway() as (server, _service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        client = _WsClient(ws)
        await client.call("enable")
        resp = await client.call("heartbeat")
        assert resp["ok"] is True
        assert resp["data"]["tripped"] is False


async def test_watchdog_trip_on_missed_heartbeat_disables_torque_and_pushes_event() -> None:
    fast_hand = dataclasses.replace(
        CFG.hand,
        safety={**CFG.hand.safety, "heartbeat_interval_s": 0.02, "watchdog_multiplier": 2},
    )
    async with (
        _gateway(allow_uncalibrated=True, hand_cfg=fast_hand) as (server, _service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        client = _WsClient(ws)
        await client.call("enable")  # arms the watchdog
        await client.call("state_stream", enabled=True)  # starts the loop that checks it

        event = await _recv_push(ws, "event", timeout=5.0)
        assert event["data"]["event"] == "watchdog_trip"

        state_resp = await client.call("state_once")
        assert state_resp["ok"] is True
        # Torque is off on every servo post-trip: MockBus reports zero current when disabled.
        assert all(j["current_ma"] == 0.0 for j in state_resp["data"]["joints"])


async def test_watchdog_trips_even_without_any_state_stream_subscriber() -> None:
    """ADR-0004: a client that enables torque and dies without ever subscribing to
    state_stream must still trip the watchdog and lose torque."""
    fast_hand = dataclasses.replace(
        CFG.hand,
        safety={**CFG.hand.safety, "heartbeat_interval_s": 0.02, "watchdog_multiplier": 2},
    )
    async with (
        _gateway(allow_uncalibrated=True, hand_cfg=fast_hand) as (server, service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        client = _WsClient(ws)
        await client.call("enable")  # arms the watchdog; NO state_stream subscription

        event = await _recv_push(ws, "event", timeout=5.0)
        assert event["data"]["event"] == "watchdog_trip"
        assert service.watchdog.tripped is True


async def test_disable_all_disarms_watchdog_no_spurious_trip() -> None:
    fast_hand = dataclasses.replace(
        CFG.hand,
        safety={**CFG.hand.safety, "heartbeat_interval_s": 0.02, "watchdog_multiplier": 2},
    )
    async with (
        _gateway(allow_uncalibrated=True, hand_cfg=fast_hand) as (server, service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        client = _WsClient(ws)
        await client.call("enable")
        await client.call("disable")  # operator intent: everything off
        assert service.watchdog.armed is False
        await asyncio.sleep(0.2)  # several timeout periods
        assert service.watchdog.tripped is False


async def test_state_stream_pushes_joints_and_health() -> None:
    async with (
        _gateway() as (server, service, _bus),
        websockets.connect(f"ws://127.0.0.1:{server.port}") as ws,
    ):
        client = _WsClient(ws)
        await client.call("enable")
        await client.call("state_stream", enabled=True)

        push = await _recv_push(ws, "state", timeout=5.0)
        data = push["data"]
        assert isinstance(data["ts"], float)
        assert len(data["joints"]) == 16
        assert len(data["health"]) == 16
        joint0 = next(j for j in data["joints"] if j["servo_id"] == 0)
        assert joint0["name"] == service.hand_cfg.joints[0].name
        assert {"pos", "vel", "current_ma"} <= joint0.keys()
        health0 = next(h for h in data["health"] if h["servo_id"] == 0)
        assert {"temp_c", "voltage_v", "faults"} <= health0.keys()


async def test_multiple_clients_only_subscribers_get_pushes() -> None:
    async with _gateway() as (server, _service, _bus):
        uri = f"ws://127.0.0.1:{server.port}"
        async with websockets.connect(uri) as subscriber, websockets.connect(uri) as silent:
            sub_client = _WsClient(subscriber)
            await sub_client.call("enable")
            await sub_client.call("state_stream", enabled=True)

            push = await _recv_push(subscriber, "state", timeout=5.0)
            assert push["type"] == "state"

            # The non-subscribing connection must not receive a state push in the meantime.
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2):
                    await silent.recv()
