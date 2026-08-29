"""`clankers-detect`: find and identify the robot's serial adapters (bring-up step 0).

Enumerates serial devices, classifies which is the Damiao arm bridge vs the U2D2, and
(unless --no-probe) broadcast-pings the Dynamixel bus across common baud rates to report
which servo IDs answer and what model they are. The ping is read-only — no torque, no
register writes — so it is safe with the hand powered or unpowered (unpowered simply
answers nothing).

The arm side is NOT actively probed here: Damiao scanning goes through the motorbridge
CLI (a different stack, one-process-per-bus — ADR-0002); this tool prints the exact
command instead.
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
from dataclasses import dataclass

from clankers.config import load_robots

logger = logging.getLogger(__name__)

# Most-likely-first: LEAP builds default to 4M; factory-fresh Dynamixels ship at 57600.
PROBE_BAUDS = (4_000_000, 1_000_000, 57_600, 2_000_000, 3_000_000, 115_200)

_PATTERNS = (
    "/dev/cu.usbmodem*",  # macOS CDC-ACM: Damiao serial bridge
    "/dev/cu.usbserial*",  # macOS FTDI: U2D2
    "/dev/ttyACM*",  # Linux equivalents
    "/dev/ttyUSB*",
)


@dataclass(frozen=True)
class SerialCandidate:
    device: str
    kind: str  # "damiao-bridge" | "u2d2" | "unknown"

    @property
    def label(self) -> str:
        return {
            "damiao-bridge": "Damiao serial bridge (arm)",
            "u2d2": "U2D2 / FTDI (LEAP hand)",
        }.get(self.kind, "unknown serial device")


def classify_device(device: str) -> SerialCandidate:
    name = device.rsplit("/", 1)[-1].lower()
    if "usbmodem" in name or name.startswith("ttyacm"):
        return SerialCandidate(device, "damiao-bridge")
    if "usbserial" in name or name.startswith("ttyusb"):
        return SerialCandidate(device, "u2d2")
    return SerialCandidate(device, "unknown")


def list_serial_candidates() -> list[SerialCandidate]:
    devices: list[str] = []
    for pattern in _PATTERNS:
        devices.extend(sorted(glob.glob(pattern)))
    return [classify_device(d) for d in devices]


def _model_names() -> dict[int, str]:
    """Model-number -> name, from lerobot's table plus our local xc330-m288 entry."""
    try:
        from lerobot.motors.dynamixel import tables as dxl_tables

        from clankers.hand_gateway.dynamixel_compat import XC330_M288_MODEL_NUMBER

        names = {num: name for name, num in dxl_tables.MODEL_NUMBER_TABLE.items()}
        names.setdefault(XC330_M288_MODEL_NUMBER, "xc330-m288")
        return names
    except ImportError:
        return {}


def probe_dynamixel(
    device: str, bauds: tuple[int, ...] = PROBE_BAUDS, *, all_bauds: bool = False
) -> dict[int, dict[int, str]]:
    """Broadcast-ping `device` at each baud. Returns {baud: {servo_id: model_name}}.

    Stops at the first baud with responders unless all_bauds (mixed-baud buses are a
    misconfiguration this flag helps diagnose).
    """
    try:
        from dynamixel_sdk import COMM_SUCCESS, PacketHandler, PortHandler
    except ImportError as exc:
        raise ImportError(
            "probing needs dynamixel-sdk. Run `uv sync --extra hardware`."
        ) from exc

    names = _model_names()
    port = PortHandler(device)
    if not port.openPort():
        raise RuntimeError(
            f"cannot open {device} — is another process (a gateway?) holding it? "
            "One process per bus (ADR-0002)."
        )
    results: dict[int, dict[int, str]] = {}
    try:
        packet = PacketHandler(2.0)  # protocol 2.0 (all X-series)
        for baud in bauds:
            if not port.setBaudRate(baud):
                logger.warning("host adapter refused baud %d; skipping", baud)
                continue
            found, comm = packet.broadcastPing(port)
            if comm == COMM_SUCCESS and found:
                results[baud] = {
                    sid: names.get(model_nb, f"model#{model_nb}")
                    for sid, (model_nb, _fw) in sorted(found.items())
                }
                if not all_bauds:
                    break
    finally:
        port.closePort()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clankers-detect", description=__doc__)
    parser.add_argument("--no-probe", action="store_true", help="enumerate only, no bus ping")
    parser.add_argument("--port", default=None, help="probe this device instead of auto-picking")
    parser.add_argument(
        "--all-bauds", action="store_true", help="keep probing every baud after a hit"
    )
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level)

    cfg = load_robots()
    candidates = list_serial_candidates()
    if not candidates:
        print("No serial adapters found. Check cables/hub, then re-run.")
        return 1

    print("Serial adapters:")
    for c in candidates:
        print(f"  {c.device:<38} -> {c.label}")

    arm = next((c for c in candidates if c.kind == "damiao-bridge"), None)
    hand = next((c for c in candidates if c.kind == "u2d2"), None)
    if args.port:
        hand = classify_device(args.port)

    expected = {j.servo_id for j in cfg.hand.joints}
    status = 0

    if hand is None:
        print("\nHand: no U2D2-like device found.")
        status = 1
    elif args.no_probe:
        print(f"\nHand: would probe {hand.device} (skipped: --no-probe)")
    else:
        print(f"\nHand: broadcast-pinging {hand.device} (read-only) ...")
        results = probe_dynamixel(hand.device, all_bauds=args.all_bauds)
        if not results:
            print(
                "  no servos answered at any baud. Checklist: hand 5V PSU on? TTL cable "
                "seated? (an unpowered bus is silent — this is not a wiring verdict)"
            )
            status = 1
        for baud, servos in results.items():
            ids = set(servos)
            print(f"  baud {baud}: {len(servos)} servo(s): "
                  + ", ".join(f"id {sid}={name}" for sid, name in servos.items()))
            missing = sorted(expected - ids)
            extra = sorted(ids - expected)
            if not missing and not extra:
                print("  all 16 expected servo IDs present — matches robots.yaml")
            else:
                if missing:
                    print(f"  MISSING expected IDs: {missing}")
                if extra:
                    print(f"  UNEXPECTED IDs (not in robots.yaml): {extra}")

    if arm is not None:
        max_id = max(j.motor_id for j in cfg.arm.joints)
        print(
            f"\nArm: found {arm.device}. Scan it with motorbridge (its venv, not this one):\n"
            f"  motorbridge scan --vendor damiao --transport dm-serial \\\n"
            f"    --serial-port {arm.device} --serial-baud {cfg.arm.serial_baud} "
            f"--start-id 1 --end-id {max_id}"
        )
    else:
        print("\nArm: no Damiao-bridge-like device found.")

    return status


if __name__ == "__main__":
    sys.exit(main())
