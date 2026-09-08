#!/usr/bin/env python3
"""Live camera preview + property tuning. No mediapipe, no gateway -- just cv2.

On macOS there are no /dev/video* nodes: cameras go through AVFoundation and are addressed
by integer index. Index order is not stable across replug or reboot, and two identical
cameras are indistinguishable by name, so `--list` exists to let you look and decide.

Many properties are silently ignored by the AVFoundation backend. Every set here is read
back and reported, so you can tell "I set it" from "it took effect".

    uv run python scripts/camera_preview.py --list
    uv run python scripts/camera_preview.py --camera 1
    uv run python scripts/camera_preview.py --camera 1 --camera 2      # side by side
    uv run python scripts/camera_preview.py --camera 1 --width 1280 --height 720 --fps 30

macOS prompts for camera access on first use; if the window stays black, grant it under
System Settings -> Privacy & Security -> Camera and restart the terminal.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

try:
    import cv2
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    raise SystemExit(
        "opencv is not importable. opencv-python, opencv-contrib-python and "
        "opencv-python-headless all install the same `cv2` package and overwrite each "
        "other; keep exactly one:\n"
        "  uv pip uninstall opencv-python opencv-python-headless opencv-contrib-python\n"
        "  uv pip install --reinstall opencv-contrib-python"
    ) from exc

PROPS: dict[str, int] = {
    "width": cv2.CAP_PROP_FRAME_WIDTH,
    "height": cv2.CAP_PROP_FRAME_HEIGHT,
    "fps": cv2.CAP_PROP_FPS,
    "brightness": cv2.CAP_PROP_BRIGHTNESS,
    "contrast": cv2.CAP_PROP_CONTRAST,
    "saturation": cv2.CAP_PROP_SATURATION,
    "gain": cv2.CAP_PROP_GAIN,
    "exposure": cv2.CAP_PROP_EXPOSURE,
    "autofocus": cv2.CAP_PROP_AUTOFOCUS,
    "focus": cv2.CAP_PROP_FOCUS,
}

# key -> (property, delta). Uppercase decreases.
ADJUST: dict[str, tuple[str, float]] = {
    "b": ("brightness", +5.0), "B": ("brightness", -5.0),
    "c": ("contrast", +5.0), "C": ("contrast", -5.0),
    "s": ("saturation", +5.0), "S": ("saturation", -5.0),
    "g": ("gain", +5.0), "G": ("gain", -5.0),
    "e": ("exposure", +1.0), "E": ("exposure", -1.0),
    "f": ("focus", +5.0), "F": ("focus", -5.0),
}

RESOLUTIONS = [(640, 480), (800, 600), (1280, 720), (1920, 1080)]

HELP = """keys:  q quit   h help   p print props   w write snapshot
       r cycle resolution   a toggle autofocus
       b/B brightness  c/C contrast  s/S saturation
       g/G gain        e/E exposure  f/F focus     (uppercase decreases)"""


def open_camera(index: int, width=None, height=None, fps=None):
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        return None
    if width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
    if height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
    if fps:
        cap.set(cv2.CAP_PROP_FPS, float(fps))
    return cap


def read_props(cap) -> dict[str, float]:
    return {name: cap.get(prop) for name, prop in PROPS.items()}


def set_prop(cap, name: str, value: float) -> tuple[bool, float]:
    """Set a property and read it back. The backend may accept, clamp, or ignore it."""
    cap.set(PROPS[name], float(value))
    got = cap.get(PROPS[name])
    return abs(got - value) < 1e-6, got


def probe(max_index: int, width, height, fps) -> int:
    print(f"Probing camera indices 0..{max_index - 1} (AVFoundation)...\n")
    found = 0
    for i in range(max_index):
        cap = open_camera(i, width, height, fps)
        if cap is None:
            continue
        ok, frame = cap.read()
        if ok and frame is not None:
            found += 1
            p = read_props(cap)
            h, w = frame.shape[:2]
            print(f"  index {i}: OPEN   delivered {w}x{h}   reports "
                  f"{int(p['width'])}x{int(p['height'])} @ {p['fps']:.0f}fps")
        else:
            print(f"  index {i}: opens but delivers no frame (held by another process?)")
        cap.release()
    if not found:
        print("\nNo camera delivered a frame. On macOS that is usually permissions: "
              "System Settings -> Privacy & Security -> Camera.")
    else:
        print(f"\n{found} camera(s) usable. Identical models cannot be told apart by name "
              "-- open each and look at the picture.")
    return found


def draw_overlay(frame, index: int, props: dict[str, float], measured_fps: float):
    lines = [
        (
            f"cam {index}   {int(props['width'])}x{int(props['height'])}   "
            f"set {props['fps']:.0f} / actual {measured_fps:.1f} fps"
        ),
        (
            f"bright {props['brightness']:.0f}   contrast {props['contrast']:.0f}   "
            f"sat {props['saturation']:.0f}"
        ),
        (
            f"gain {props['gain']:.0f}   exposure {props['exposure']:.0f}   "
            f"focus {props['focus']:.0f}  (autofocus {props['autofocus']:.0f})"
        ),
    ]
    for n, text in enumerate(lines):
        y = 24 + n * 22
        # Draw twice: dark stroke under light fill stays readable on any scene.
        cv2.putText(frame, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3,
                    cv2.LINE_AA)
        cv2.putText(frame, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 1,
                    cv2.LINE_AA)
    return frame


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="camera_preview", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--camera", type=int, action="append", default=None,
                    help="camera index; repeat the flag for several windows")
    ap.add_argument("--list", action="store_true", help="probe indices and exit")
    ap.add_argument("--max-index", type=int, default=4,
                    help="highest index to probe; OpenCV prints its own noise past the last camera")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--fps", type=float, default=None)
    ap.add_argument("--snapshot-dir", default=".", help="where 'w' writes stills")
    args = ap.parse_args(argv)

    if args.list or not args.camera:
        return 0 if probe(args.max_index, args.width, args.height, args.fps) else 1

    caps: dict[int, object] = {}
    for index in args.camera:
        cap = open_camera(index, args.width, args.height, args.fps)
        if cap is None:
            print(f"camera {index}: could not open (try --list)")
            continue
        caps[index] = cap
        cv2.namedWindow(f"cam {index}", cv2.WINDOW_NORMAL)
    if not caps:
        return 1

    print(HELP)
    res_idx = 0
    frames = 0
    measured = 0.0
    t0 = time.monotonic()
    snapshot_dir = Path(args.snapshot_dir)

    try:
        while True:
            for index, cap in list(caps.items()):
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                props = read_props(cap)
                draw_overlay(frame, index, props, measured)
                cv2.imshow(f"cam {index}", frame)

            frames += 1
            dt = time.monotonic() - t0
            if dt >= 0.5:
                measured = frames / dt
                frames, t0 = 0, time.monotonic()

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == 255:
                continue
            ch = chr(key)

            if ch == "h":
                print(HELP)
            elif ch == "p":
                for index, cap in caps.items():
                    print(f"cam {index}: " + "  ".join(
                        f"{k}={v:g}" for k, v in read_props(cap).items()))
            elif ch == "w":
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                for index, cap in caps.items():
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        path = snapshot_dir / f"cam{index}-{stamp}.png"
                        cv2.imwrite(str(path), frame)
                        print(f"wrote {path}")
            elif ch == "r":
                res_idx = (res_idx + 1) % len(RESOLUTIONS)
                w, h = RESOLUTIONS[res_idx]
                for index, cap in caps.items():
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(w))
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(h))
                    got = read_props(cap)
                    note = "" if (int(got["width"]), int(got["height"])) == (w, h) else \
                        "  <- backend refused, kept its own"
                    print(f"cam {index}: asked {w}x{h}, got "
                          f"{int(got['width'])}x{int(got['height'])}{note}")
            elif ch == "a":
                for index, cap in caps.items():
                    now = cap.get(cv2.CAP_PROP_AUTOFOCUS)
                    ok, got = set_prop(cap, "autofocus", 0.0 if now else 1.0)
                    print(f"cam {index}: autofocus -> {got:g}"
                          f"{'' if ok else '  <- backend ignored it'}")
            elif ch in ADJUST:
                name, delta = ADJUST[ch]
                for index, cap in caps.items():
                    target = cap.get(PROPS[name]) + delta
                    ok, got = set_prop(cap, name, target)
                    print(f"cam {index}: {name} -> {got:g}"
                          f"{'' if ok else '  <- backend ignored/clamped it'}")
    finally:
        for cap in caps.values():
            cap.release()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
