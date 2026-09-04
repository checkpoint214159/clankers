#!/usr/bin/env python3
"""Clear latched Dynamixel hardware faults by rebooting servos.

A hardware error (undervoltage, overload, overheat) LATCHES: the servo blinks red and
refuses commands until it is rebooted or power-cycled. Rebooting drops torque and clears
Hardware_Error_Status; it does not touch EEPROM, so IDs, baud, homing offsets and current
limits all survive.

Usage (gateway must be running -- it owns the serial port):
    uv run python scripts/reboot_hand_servos.py            # reboot whatever reports a fault
    uv run python scripts/reboot_hand_servos.py 1 3 12 13  # reboot specific servo ids
"""

import asyncio
import json
import sys

import websockets
from clankers.config import load_robots


async def main(argv: list[str]) -> int:
    url = f"ws://127.0.0.1:{load_robots().ports['hand_gateway_ws']}"
    wanted = [int(a) for a in argv[1:]]

    async with websockets.connect(url) as ws:
        req = {"n": 0}

        async def call(op: str, **payload):
            req["n"] += 1
            rid = req["n"]
            await ws.send(json.dumps({"op": op, "req_id": rid, **payload}))
            while True:  # state pushes interleave; match on req_id
                msg = json.loads(await ws.recv())
                if msg.get("req_id") == rid:
                    return msg

        if not wanted:
            status = await call("error_status")
            if not status.get("ok"):
                print(f"error_status failed: {status.get('error')}", file=sys.stderr)
                return 1
            # error_status reports every servo, healthy ones with an empty fault list.
            faults = {
                k: v for k, v in (status["data"].get("faults") or {}).items() if v
            }
            wanted = sorted(int(k) for k in faults)
            if not wanted:
                print("No servo is reporting a fault.")
                return 0
            print(f"Faulted: {faults}")

        # Torque must be off to reboot safely: the hand goes limp as each one resets.
        await call("disable")
        print("Torque off. Support the hand if it is holding a pose.")

        failed = []
        for sid in wanted:
            resp = await call("reboot", servo_id=sid)
            ok = resp.get("ok")
            print(f"  reboot servo {sid}: {'ok' if ok else resp.get('error')}")
            if not ok:
                failed.append(sid)
            await asyncio.sleep(0.3)  # the servo needs a moment to come back on the bus

        after = await call("error_status")
        remaining = {
            k: v for k, v in ((after.get("data") or {}).get("faults") or {}).items() if v
        }
        print(f"\nRemaining faults: {remaining or 'none'}")
        if remaining:
            print("Still faulted: the cause is present, not just latched. Undervoltage that")
            print("returns immediately means the rail is sagging -- check power and the")
            print("current limit (`check_current_limit`) before enabling torque again.")
        return 1 if (failed or remaining) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
