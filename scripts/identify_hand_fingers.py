#!/usr/bin/env python3
"""Jog one finger group at a time and record which finger actually moves.

robots.yaml claims servos 0-3 are the index, 4-7 the middle, and so on. That claim is only
as good as the last time someone watched the hand move -- and it stops being true the moment
servos are swapped during a repair. This asks the hardware instead.

Torque-on, but only ever one servo at a time and only by `jog`, which the gateway clamps to
safety.max_step_rad. Nothing here trusts the joint map it is testing.

    uv run python scripts/identify_hand_fingers.py --url ws://192.168.2.2:9003
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import websockets
from clankers.config import load_robots

FINGERS = ("index", "middle", "ring", "thumb", "none / unsure")


async def main(argv: list[str] | None = None) -> int:
    cfg = load_robots()
    ap = argparse.ArgumentParser(prog="identify_hand_fingers", description=__doc__)
    ap.add_argument("--url", default=f"ws://127.0.0.1:{cfg.ports['hand_gateway_ws']}")
    ap.add_argument("--reps", type=int, default=6, help="jog steps out and back per group")
    args = ap.parse_args(argv)

    # One representative servo per labelled group: the flex joint, whose motion is unmistakable.
    groups: dict[str, int] = {}
    for j in cfg.hand.joints:
        if j.name.endswith("_mcp_flex") or j.name.endswith("_base_rot"):
            groups.setdefault(j.finger, j.servo_id)
    for j in cfg.hand.joints:                      # fall back to the first servo of a group
        groups.setdefault(j.finger, j.servo_id)

    async with websockets.connect(args.url) as ws:
        req = {"n": 0}

        async def call(op: str, **payload):
            req["n"] += 1
            rid = req["n"]
            await ws.send(json.dumps({"op": op, "req_id": rid, **payload}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("req_id") == rid:
                    return msg

        step = float(cfg.hand.safety["max_step_rad"])
        print("Enabling torque. Keep clear of the hand.\n")
        r = await call("enable")
        if not r.get("ok"):
            print(f"enable failed: {r.get('error')}", file=sys.stderr)
            return 1

        observed: dict[str, str] = {}
        try:
            for label, servo_id in sorted(groups.items(), key=lambda kv: kv[1]):
                input(f"About to jog servo {servo_id} (robots.yaml calls it '{label}'). "
                      "Watch the hand, then press Enter...")
                for _ in range(args.reps):        # out
                    await call("jog", servo_id=servo_id, delta_rad=step)
                    await asyncio.sleep(0.12)
                for _ in range(args.reps):        # and back
                    await call("jog", servo_id=servo_id, delta_rad=-step)
                    await asyncio.sleep(0.12)

                for i, name in enumerate(FINGERS):
                    print(f"   {i}) {name}")
                choice = input("Which finger moved? ").strip()
                observed[label] = FINGERS[int(choice)] if choice.isdigit() and \
                    int(choice) < len(FINGERS) else "unknown"
        finally:
            await call("disable")
            print("\nTorque off.")

        print("\n--- result ---")
        wrong = {k: v for k, v in observed.items() if v != k}
        for label, actual in observed.items():
            mark = "ok" if actual == label else f"MISLABELLED -> actually {actual}"
            print(f"  robots.yaml '{label}' (servo {groups[label]}): {mark}")
        if wrong:
            print("\nrobots.yaml finger labels do not match the hardware. Fix the `finger:`"
                  "\nand `name:` fields for the affected groups -- keep canonical/servo_id/"
                  "\nlimit rows where they are, only the labels move. Then re-run"
                  "\n`uv run clankers-export-config`.")
        else:
            print("\nAll groups match robots.yaml. If teleop still swaps fingers, the fault"
                  "\nis in clankers.teleop.retarget.FINGER_MAP, not the joint map.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
