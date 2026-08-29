"""Teleop demo CLI: webcam (or synthetic wave) -> retarget -> hand gateway `pos` at 20 Hz.

Commands flow through the gateway's full safety stack (joint limits, per-command step
clamp, watchdog) — this script never talks to the bus directly (ADR-0002/0004).

Usage:
    uv run clankers-teleop-demo --synthetic         # no camera: open<->curl wave
    uv run clankers-teleop-demo                     # webcam + preview window
      keys in the preview: c = capture neutral with an OPEN relaxed hand, q = quit

The gateway must allow `pos` — until bring-up sets hand.calibrated, start it with:
    uv run clankers-hand-gateway --mock --allow-uncalibrated
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from typing import Any

from websockets.sync.client import connect as ws_connect

from clankers.config import load_robots
from clankers.teleop.retarget import Retargeter

logger = logging.getLogger(__name__)

HEARTBEAT_EVERY_S = 0.4  # gateway watchdog: heartbeat_interval_s 0.5 x multiplier 4


class GatewayClient:
    """Minimal sync WS client for the gateway envelope; surfaces event pushes."""

    def __init__(self, url: str) -> None:
        self._ws = ws_connect(url, open_timeout=5)
        self._req_id = 0

    def call(self, op: str, **payload: Any) -> dict[str, Any]:
        self._req_id += 1
        self._ws.send(json.dumps({"op": op, "req_id": self._req_id, **payload}))
        while True:
            msg = json.loads(self._ws.recv(timeout=5))
            if msg.get("type") == "event":
                event = msg.get("data", {}).get("event")
                raise RuntimeError(f"gateway event: {event} (torque was cut; restart teleop)")
            if msg.get("req_id") == self._req_id:
                if not msg.get("ok"):
                    raise RuntimeError(f"{op} failed: {msg.get('error')}")
                return msg.get("data", {})

    def close(self) -> None:
        self._ws.close()


def _ema(prev: dict[str, float] | None, new: dict[str, float], alpha: float) -> dict[str, float]:
    if prev is None:
        return dict(new)
    return {k: alpha * v + (1.0 - alpha) * prev[k] for k, v in new.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clankers-teleop-demo", description=__doc__)
    parser.add_argument("--url", default=None, help="gateway WS url (default from robots.yaml)")
    parser.add_argument("--camera", type=int, default=0, help="webcam index")
    parser.add_argument("--synthetic", action="store_true", help="no camera: open<->curl wave")
    parser.add_argument("--hz", type=float, default=20.0, help="command rate")
    parser.add_argument("--alpha", type=float, default=0.4, help="EMA smoothing factor (0..1]")
    parser.add_argument("--no-preview", action="store_true", help="camera mode without a window")
    parser.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level)

    cfg = load_robots()
    url = args.url or f"ws://127.0.0.1:{cfg.ports['hand_gateway_ws']}"
    retarget = Retargeter(hand_cfg=cfg.hand)
    dt = 1.0 / args.hz

    tracker = None
    synth = None
    if args.synthetic:
        from clankers.teleop.synthetic import hand_landmarks, wave

        synth = wave(hz=args.hz)
        retarget.set_neutral(hand_landmarks(curl=0.0))
    else:
        from clankers.teleop.camera import CameraTracker

        tracker = CameraTracker(camera_index=args.camera)

    client = GatewayClient(url)
    logger.info("connected to %s", url)
    try:
        client.call("enable")
    except RuntimeError as exc:
        if "unacknowledged faults" in str(exc):
            logger.error("%s — clear faults from the studio (reboot op) first", exc)
            return 1
        raise
    logger.info("torque enabled on all 16; watchdog armed")

    smoothed: dict[str, float] | None = None
    last_heartbeat = 0.0
    frames = sent = 0
    start = time.monotonic()
    try:
        while True:
            tick = time.monotonic()
            if args.duration is not None and tick - start > args.duration:
                break
            if tick - last_heartbeat > HEARTBEAT_EVERY_S:
                client.call("heartbeat")
                last_heartbeat = tick

            if synth is not None:
                lm = next(synth)
                frame = None
            else:
                assert tracker is not None
                lm, frame = tracker.read()

            if lm is not None:
                frames += 1
                if retarget.has_neutral:
                    smoothed = _ema(smoothed, retarget(lm), args.alpha)
                    try:
                        client.call("pos", targets=smoothed)
                        sent += 1
                    except RuntimeError as exc:
                        if "hand.calibrated is false" in str(exc):
                            logger.error(
                                "gateway refuses `pos` pre-calibration. Restart it with:\n"
                                "  uv run clankers-hand-gateway --mock --allow-uncalibrated"
                            )
                            return 1
                        raise

            if tracker is not None and not args.no_preview:
                hud = (
                    "teleoping - q quits"
                    if retarget.has_neutral
                    else "open hand flat, press c to capture neutral"
                )
                key = tracker.show(frame, hud)
                if key == ord("q") or key == 27:
                    break
                if key == ord("c") and lm is not None:
                    retarget.set_neutral(lm)
                    logger.info("neutral captured — teleop live")
            elif tracker is not None and not retarget.has_neutral and frames >= int(args.hz):
                # headless camera mode: auto-capture after ~1s of stable detection
                retarget.set_neutral(lm)
                logger.info("neutral auto-captured — teleop live")

            time.sleep(max(0.0, dt - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            client.call("disable")
            logger.info("torque disabled")
        except Exception:
            logger.exception("could not disable torque on exit — check the studio")
        client.close()
        if tracker is not None:
            tracker.close()
    logger.info("frames with a detected hand: %d, commands sent: %d", frames, sent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
