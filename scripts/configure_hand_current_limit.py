#!/usr/bin/env python3
"""One-time setup that makes the hand's current limit real.

Out of the box every XL330 is in position mode (Operating_Mode 3), which ignores
Current_Limit: a blocked finger draws full stall current (~1.47 A) and a few at once sag the
5 V rail until servos latch input-voltage faults. This writes robots.yaml's
`hand.operating_mode` (5, current-based position) and `hand.current_limit_ma` into each
servo's EEPROM. The gateway's `enable` refuses until they match.

EEPROM survives reboots and power loss, so this runs once per servo -- again only if a servo
is swapped. Servos that already match are not rewritten. Torque is turned OFF on all 16
first (the servo locks its EEPROM while powered): support the hand, it goes limp.

Usage (the gateway must be running -- it owns the serial port):
    uv run python scripts/configure_hand_current_limit.py                  # on the Pi
    uv run python scripts/configure_hand_current_limit.py --url ws://pi:9003
    uv run python scripts/configure_hand_current_limit.py --check          # read-only
"""

import argparse
import asyncio
import json
import sys

import websockets
from clankers.config import load_robots


def _grouped(by_servo: dict[str, float]) -> str:
    """{"0": 3, "1": 3, "7": 5} -> "3 on servos [0, 1]; 5 on servos [7]"."""
    groups: dict[float, list[int]] = {}
    for sid, value in by_servo.items():
        groups.setdefault(round(value), []).append(int(sid))
    return "; ".join(f"{v} on servos {sorted(ids)}" for v, ids in groups.items())


def _describe(data: dict) -> list[str]:
    lines = []
    if data.get("mode_mismatched"):
        lines.append(f"  Operating_Mode: {_grouped(data['mode_mismatched'])}  "
                     f"(want {data['configured_operating_mode']})")
    if data.get("mismatched"):
        lines.append(f"  Current_Limit mA: {_grouped(data['mismatched'])}  "
                     f"(want {round(data['configured_ma'])})")
    return lines


async def main(argv: list[str]) -> int:
    cfg = load_robots()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=f"ws://127.0.0.1:{cfg.ports['hand_gateway_ws']}")
    parser.add_argument("--check", action="store_true", help="only report; write nothing")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv[1:])

    async with websockets.connect(args.url) as ws:
        req = {"n": 0}

        async def call(op: str, **payload):
            req["n"] += 1
            rid = req["n"]
            await ws.send(json.dumps({"op": op, "req_id": rid, **payload}))
            while True:  # state pushes interleave; match on req_id
                msg = json.loads(await ws.recv())
                if msg.get("req_id") == rid:
                    return msg

        check = await call("check_current_limit")
        if not check.get("ok") or not check["data"].get("checked"):
            print(f"could not read the servos: {check.get('error') or check.get('data')}",
                  file=sys.stderr)
            return 1
        wrong = _describe(check["data"])
        if not wrong:
            print("All 16 servos already match robots.yaml -- nothing to write.")
            return 0
        print("Servos that differ from robots.yaml:")
        print("\n".join(wrong))
        if args.check:
            return 1

        print("\nThis writes EEPROM on those servos and turns torque OFF on all 16 first.")
        print("Support the hand -- it goes limp.")
        if not args.yes and input("Continue? [y/N] ").strip().lower() != "y":
            print("Nothing written.")
            return 1

        off = await call("disable")
        if not off.get("ok"):
            print(f"disable failed: {off.get('error')}", file=sys.stderr)
            return 1

        resp = await call("configure_current_limiting", confirm=True)
        if not resp.get("ok"):
            print(f"configure_current_limiting failed: {resp.get('error')}", file=sys.stderr)
            return 1
        data = resp["data"]
        print(f"\nWrote servos {data['changed']}: operating mode {data['operating_mode']}, "
              f"Current_Limit {data['current_limit_ma']} mA.")

        after = await call("check_current_limit")
        remaining = _describe(after.get("data") or {})
        if remaining:
            print("Still differs after writing:\n" + "\n".join(remaining), file=sys.stderr)
            return 1
        print("\nVerified. Next: enable torque, gently hold one finger against its motion, and")
        print("watch that joint's current in the studio -- it should level off near "
              f"{round(check['data']['configured_ma'])} mA instead of climbing past 1 A.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
