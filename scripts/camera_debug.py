#!/usr/bin/env python3
"""Look at and tune the cameras on a Linux/V4L2 box. The Pi-side companion to
camera_preview.py, which was written against macOS and is wrong here in two ways:

  * AVFoundation implements no image controls, so camera_preview.py brightens pixels in
    software and calls that a preview. V4L2 implements the real ones -- brightness,
    contrast, gamma, gain, sharpness, white balance, exposure -- so this script drives
    the camera itself and software adjustment never enters into it.
  * Camera indices on macOS are unstable and two identical cameras are indistinguishable.
    Here every camera has a USB bus path that survives replug and reboot, so `--list`
    tells the two "USB HD Camera"s apart and `--camera 1.4` names one for good.

There is no window to draw into over ssh or VS Code, so the default view is an MJPEG
stream over HTTP: VS Code forwards the port to your laptop like any other dev server,
and the page carries a slider per control. `--view window` draws on the Pi's own monitor
(DISPLAY=:0 via Xwayland) if you are sitting in front of it; `--view clip` records an mp4
to scp; `--view snapshot` writes one frame and exits.

    ./scripts/camera_debug.py --list
    ./scripts/camera_debug.py                       # both cameras, browser view on :8088
    ./scripts/camera_debug.py --camera 1.4 --width 1280 --height 720
    ./scripts/camera_debug.py --view window
    ./scripts/camera_debug.py --view clip --seconds 10 --out /tmp/cam.mp4
    ./scripts/camera_debug.py --view snapshot --out /tmp/cam.png

Why a setting "doesn't stick": V4L2 marks a manual control `inactive` while its auto
counterpart owns it, and writing to it then fails with a misleading "Permission denied"
-- exposure_time_absolute is locked while auto_exposure is in Aperture Priority, and
white_balance_temperature is locked while white_balance_automatic is on. This script
turns the gate off for you and says so, rather than reporting a silent no-op.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

try:
    import cv2
    import numpy as np
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    raise SystemExit(
        "opencv is not importable. Install the camera extra:\n"
        "  uv sync --extra camera\n"
        "(the `teleop` extra pulls mediapipe, which publishes no aarch64 wheel and so\n"
        "cannot be installed on this Pi at all -- `camera` exists to avoid it.)\n"
        "\n"
        "If cv2 imports but is an empty namespace, opencv-python, opencv-contrib-python\n"
        "and opencv-python-headless have overwritten each other; keep exactly one:\n"
        "  uv pip uninstall opencv-python opencv-python-headless opencv-contrib-python\n"
        "  uv pip install --reinstall opencv-contrib-python"
    ) from exc

# Drivers that expose a Video Capture node without being a camera: the Pi's ISP, its
# H.264/HEVC/JPEG codecs, and the video decoder. Real cameras are uvcvideo (USB) or the
# CSI receivers (rp1-cfe, unicam).
NON_CAMERA_DRIVERS = {"bcm2835-isp", "bcm2835-codec", "rpi-hevc-dec", "rpivid", "pisp-be"}

# Manual control -> the auto control that locks it, and the value that hands control back.
# Names differ across kernel versions, so each manual control lists every gate it might
# have; only the one this camera actually exposes is used.
AUTO_GATES: dict[str, tuple[tuple[str, int], ...]] = {
    "exposure_time_absolute": (("auto_exposure", 1), ("exposure_auto", 1)),
    "exposure_absolute": (("auto_exposure", 1), ("exposure_auto", 1)),
    "white_balance_temperature": (
        ("white_balance_automatic", 0),
        ("white_balance_temperature_auto", 0),
    ),
    "focus_absolute": (("focus_automatic_continuous", 0), ("focus_auto", 0)),
    "gain": (("gain_automatic", 0), ("autogain", 0)),
}

# Shown first in the UI: the controls that actually explain a picture looking wrong.
CONTROL_ORDER = [
    "auto_exposure",
    "exposure_time_absolute",
    "exposure_dynamic_framerate",
    "white_balance_automatic",
    "white_balance_temperature",
    "brightness",
    "contrast",
    "saturation",
    "gamma",
    "gain",
    "hue",
    "sharpness",
    "backlight_compensation",
    "power_line_frequency",
]


# --- v4l2 ---------------------------------------------------------------------------


def _v4l2ctl() -> str:
    path = shutil.which("v4l2-ctl")
    if not path:
        raise SystemExit(
            "v4l2-ctl not found. It is what reads a control's real range and its "
            "active/inactive state -- OpenCV reports neither.\n"
            "  sudo apt install v4l-utils"
        )
    return path


def v4l2(device: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_v4l2ctl(), "-d", device, *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,  # every caller inspects returncode itself; a raise would lose the stderr
    )


@dataclass
class Control:
    """One V4L2 control, with the range and flags OpenCV's `cap.get()` cannot tell you."""

    name: str
    kind: str  # int | bool | menu | intmenu | button | str
    value: int
    default: int | None = None
    minimum: int | None = None
    maximum: int | None = None
    step: int | None = None
    flags: set[str] = field(default_factory=set)
    menu: dict[int, str] = field(default_factory=dict)

    @property
    def inactive(self) -> bool:
        """True when an auto control currently owns this one; writes will be refused."""
        return "inactive" in self.flags

    @property
    def read_only(self) -> bool:
        return "read-only" in self.flags

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "value": self.value,
            "default": self.default,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "inactive": self.inactive,
            "read_only": self.read_only,
            "menu": {str(k): v for k, v in self.menu.items()},
            "gate": gate_for(self.name),
        }


_CTRL_RE = re.compile(r"^\s*(\w+)\s+0x[0-9a-fA-F]+\s+\((\w+)\)\s*:\s*(.*)$")
_MENU_RE = re.compile(r"^\s+(\d+):\s*(.*)$")


def gate_for(name: str) -> str | None:
    gates = AUTO_GATES.get(name)
    return gates[0][0] if gates else None


def _int(fields: dict[str, str], key: str) -> int | None:
    try:
        return int(fields[key])
    except (KeyError, ValueError):
        return None


def read_controls(device: str) -> dict[str, Control]:
    out = v4l2(device, "--list-ctrls-menus").stdout
    controls: dict[str, Control] = {}
    current: Control | None = None
    for line in out.splitlines():
        match = _CTRL_RE.match(line)
        if match:
            name, kind, rest = match.groups()
            fields: dict[str, str] = {}
            for pair in re.finditer(r"(\w+)=([^\s]+)", rest):
                fields[pair.group(1)] = pair.group(2)
            flags = set()
            if "flags" in rest:
                flags = {
                    f.strip()
                    for f in rest.split("flags=", 1)[1].split("(")[0].split(",")
                    if f.strip()
                }

            current = Control(
                name=name,
                kind=kind,
                value=_int(fields, "value") or 0,
                default=_int(fields, "default"),
                minimum=_int(fields, "min"),
                maximum=_int(fields, "max"),
                step=_int(fields, "step"),
                flags=flags,
            )
            controls[name] = current
            continue
        menu = _MENU_RE.match(line)
        if menu and current is not None:
            current.menu[int(menu.group(1))] = menu.group(2).strip()
    return controls


def set_control(device: str, name: str, value: int) -> tuple[bool, str]:
    """Set one control, opening its auto gate first if that is what is blocking it.

    Returns (ok, human-readable explanation). The explanation is the point: a V4L2 write
    to an inactive control fails as EACCES "Permission denied", which reads like a udev
    problem and is really "auto_exposure owns this". Reporting the readback rather than
    the exit code also catches the other case -- a driver that accepts the ioctl and
    quietly clamps the value to something else.
    """
    controls = read_controls(device)
    control = controls.get(name)
    if control is None:
        return False, f"{name}: no such control on {device}"
    if control.read_only:
        return False, f"{name} is read-only"

    notes: list[str] = []
    if control.inactive:
        for gate_name, manual_value in AUTO_GATES.get(name, ()):
            gate = controls.get(gate_name)
            if gate is None or gate.value == manual_value:
                continue
            result = v4l2(device, "--set-ctrl", f"{gate_name}={manual_value}")
            if result.returncode == 0:
                label = gate.menu.get(manual_value, str(manual_value))
                notes.append(f"set {gate_name}={label} first (it was locking {name})")
            break
        else:
            if name in AUTO_GATES:
                notes.append(f"{name} is inactive and its auto gate is not exposed here")

    result = v4l2(device, "--set-ctrl", f"{name}={value}")
    after = read_controls(device).get(name)
    got = after.value if after else None

    if got == value:
        return True, "; ".join([*notes, f"{name} = {value}"])
    if result.returncode != 0:
        reason = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        detail = reason[0]
        if "Permission denied" in detail:
            detail = "refused: the control is inactive (an auto control owns it)"
        return False, "; ".join([*notes, f"{name}: {detail}"])
    return False, "; ".join([*notes, f"{name}: asked {value}, camera kept {got}"])


def reset_controls(device: str) -> list[str]:
    """Put every writable control back to its driver default.

    Three passes, because a manual control cannot be written while its auto counterpart
    owns it: unlock every gate, restore the manual values, then put the gates themselves
    back. Restoring the autos first -- the obvious order -- re-locks exposure and white
    balance before their turn comes, so those two silently keep whatever they were set to
    and "reset to defaults" quietly isn't one.
    """
    controls = read_controls(device)
    manual_of = {g: v for gates in AUTO_GATES.values() for g, v in gates if g in controls}

    for gate, manual_value in manual_of.items():
        v4l2(device, "--set-ctrl", f"{gate}={manual_value}")

    def restore(name: str) -> str | None:
        control = controls[name]
        if control.default is None or control.read_only or control.kind == "button":
            return None
        if v4l2(device, "--set-ctrl", f"{name}={control.default}").returncode != 0:
            return f"{name}: could not restore default {control.default}"
        return None

    problems = [restore(n) for n in controls if n not in manual_of]
    problems += [restore(n) for n in manual_of]
    return [p for p in problems if p]


@dataclass
class DeviceInfo:
    path: str
    index: int
    card: str
    driver: str
    bus: str
    by_path: str | None

    @property
    def usb_port(self) -> str:
        """The physical port, e.g. "1.4" -- what actually distinguishes identical cameras.

        Unlike the index this survives replug and reboot, and unlike /dev/v4l/by-id it is
        unique: two cameras of the same model with no serial number collide on by-id.
        """
        match = re.search(r"usb-[^-]*-([\d.]+)$", self.bus)
        return match.group(1) if match else self.bus

    @property
    def label(self) -> str:
        return f"{self.card} @ usb {self.usb_port}"


def _info_field(info: str, key: str) -> str:
    match = re.search(rf"{key}\s*:\s*(.+)", info)
    return match.group(1).strip() if match else ""


def enumerate_devices(include_all: bool = False) -> list[DeviceInfo]:
    by_path: dict[str, str] = {}
    for link in glob.glob("/dev/v4l/by-path/*"):
        try:
            by_path.setdefault(os.path.realpath(link), link)
        except OSError:
            pass

    devices: list[DeviceInfo] = []
    for path in sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p))):
        info = v4l2(path, "--info").stdout
        if not info:
            continue
        caps = info.split("Device Caps", 1)[-1]
        # "Metadata Capture" is the UVC sidecar node that sits next to every camera; it
        # opens and delivers nothing an image pipeline can use.
        if "Video Capture" not in caps or "Video Capture" not in caps.replace(
            "Metadata Capture", ""
        ):
            continue

        driver = _info_field(info, "Driver name")
        if not include_all and driver in NON_CAMERA_DRIVERS:
            continue
        devices.append(
            DeviceInfo(
                path=path,
                index=int(re.sub(r"\D", "", path)),
                card=_info_field(info, "Card type").split(":")[0].strip(),
                driver=driver,
                bus=_info_field(info, "Bus info"),
                by_path=by_path.get(path),
            )
        )
    return devices


def read_formats(device: str) -> list[tuple[str, list[tuple[int, int, float]]]]:
    """[(fourcc, [(w, h, max_fps), ...]), ...] as the camera reports it."""
    out = v4l2(device, "--list-formats-ext").stdout
    formats: list[tuple[str, list[tuple[int, int, float]]]] = []
    sizes: list[tuple[int, int, float]] = []
    size: tuple[int, int] | None = None
    for line in out.splitlines():
        fmt = re.search(r"\[\d+\]:\s*'(\w+)'", line)
        if fmt:
            sizes = []
            formats.append((fmt.group(1), sizes))
            continue
        dim = re.search(r"Size: Discrete (\d+)x(\d+)", line)
        if dim:
            size = (int(dim.group(1)), int(dim.group(2)))
            continue
        fps = re.search(r"\(([\d.]+) fps\)", line)
        if fps and size:
            rate = float(fps.group(1))
            for i, (w, h, existing) in enumerate(sizes):
                if (w, h) == size:
                    sizes[i] = (w, h, max(existing, rate))
                    break
            else:
                sizes.append((*size, rate))
    return formats


def resolve_camera(token: str, devices: list[DeviceInfo]) -> DeviceInfo | None:
    """Accept an index (0), a node (/dev/video0), or a USB port ("1.4") / name substring."""
    for device in devices:
        if token == device.path or token == str(device.index) or token == device.usb_port:
            return device
    matches = [d for d in devices if token.lower() in d.label.lower() or token in d.bus]
    return matches[0] if len(matches) == 1 else None


# --- capture ------------------------------------------------------------------------


def frame_stats(frame) -> dict:
    """Numbers that separate the three ways a picture goes wrong.

    Exposure and white balance look equally "wrong" on a screen but have different fixes,
    and neither is fixable by the software brightness slider camera_preview.py offers.
    """
    b, g, r = (float(x) for x in cv2.mean(frame)[:3])
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    total = gray.size
    dark = float((gray < 16).sum()) / total
    blown = float((gray > 249).sum()) / total
    mean = float(gray.mean())

    notes = []
    if mean < 45:
        notes.append(f"underexposed (mean {mean:.0f}, {dark:.0%} near black)")
    elif mean > 205:
        notes.append(f"overexposed (mean {mean:.0f})")
    if blown > 0.05:
        notes.append(f"{blown:.0%} of pixels blown out")
    if b > 1 and r / max(b, 1e-6) > 1.35:
        notes.append(f"warm/red cast (R:B {r / max(b, 1e-6):.2f})")
    elif r > 1 and b / max(r, 1e-6) > 1.35:
        notes.append(f"cool/blue cast (B:R {b / max(r, 1e-6):.2f})")
    if mean > 0 and float(gray.std()) < 12:
        notes.append("very flat (lens cap? no light?)")

    return {
        "mean": round(mean, 1),
        "std": round(float(gray.std()), 1),
        "r": round(r, 1),
        "g": round(g, 1),
        "b": round(b, 1),
        "dark_frac": round(dark, 4),
        "blown_frac": round(blown, 4),
        "notes": notes,
    }


def exposure_hint(device: str, stats: dict) -> str | None:
    """When a picture is dark or blown, say whether the camera's own AE is the cause.

    Worth the extra v4l2 round-trip only in that case, and worth it because the two
    explanations look identical on screen and have opposite fixes: a dark room needs
    more light, whereas an auto-exposure that has latched at its minimum needs to be
    taken out of auto altogether. One of the two cameras here does exactly that -- in
    Aperture Priority it delivers a mean of 4, and at the very same exposure value set
    manually it delivers 102.
    """
    if not stats or not (stats["mean"] < 45 or stats["mean"] > 205):
        return None
    controls = read_controls(device)
    auto = controls.get("auto_exposure") or controls.get("exposure_auto")
    if auto is None:
        return None
    mode = auto.menu.get(auto.value, str(auto.value))
    manual = controls.get("exposure_time_absolute") or controls.get("exposure_absolute")
    if "Manual" in mode:
        if manual is None:
            return None
        return f"exposure is manual at {manual.value} (range {manual.minimum}-{manual.maximum})"
    verb = "dark" if stats["mean"] < 45 else "blown out"
    return (
        f"auto_exposure is '{mode}' and the picture is still {verb} -- this camera's AE "
        f"is not converging. Set auto_exposure to Manual Mode and adjust "
        f"exposure_time_absolute by hand."
    )


class Camera:
    """One camera: a capture thread publishing the latest frame, plus its V4L2 controls.

    The thread exists so a slow HTTP client cannot stall capture and so several viewers
    share one stream -- a UVC camera allows only a single opener, so the alternative is
    a second viewer failing to open the device at all.
    """

    def __init__(self, device: DeviceInfo, width=None, height=None, fps=None, fourcc="MJPG"):
        self.device = device
        self.width, self.height, self.fps, self.fourcc = width, height, fps, fourcc
        self.cap: cv2.VideoCapture | None = None
        self.frame = None
        self.stats: dict = {}
        self.swap_rb = False
        self.measured_fps = 0.0
        self.error: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def open(self) -> bool:
        cap = cv2.VideoCapture(self.device.path, cv2.CAP_V4L2)
        if not cap.isOpened():
            self.error = f"could not open {self.device.path} (already in use?)"
            return False
        # Order matters: FOURCC before the size. MJPG is not cosmetic here -- these
        # cameras offer 1280x720 at 30 fps compressed but only 10 fps as raw YUYV, and
        # OpenCV's default pick is YUYV.
        if self.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
        if self.width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.width))
        if self.height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.height))
        if self.fps:
            cap.set(cv2.CAP_PROP_FPS, float(self.fps))
        # A short queue keeps the preview honest -- with the driver default of 4 you tune
        # against a picture several frames old and every change looks laggy. Not 1,
        # though: with a single buffer this camera delivers 15 fps instead of 30, because
        # there is nothing to fill while the one buffer is being re-queued. 2 measures the
        # same 30 fps as the default and still holds only one frame of lag, which the
        # capture thread drains continuously anyway.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
        ok, _ = cap.read()
        if not ok:
            cap.release()
            self.error = f"{self.device.path} opened but delivered no frame"
            return False
        self.cap = cap
        self.error = None
        return True

    def actual(self) -> tuple[int, int, float, str]:
        assert self.cap is not None
        code = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        fourcc = "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4)) if code else "?"
        return (
            int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            self.cap.get(cv2.CAP_PROP_FPS),
            fourcc,
        )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        count, t0 = 0, time.monotonic()
        while not self._stop.is_set():
            assert self.cap is not None
            ok, frame = self.cap.read()
            if not ok or frame is None:
                self.error = "camera stopped delivering frames (unplugged?)"
                time.sleep(0.2)
                continue
            self.error = None
            if self.swap_rb:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            count += 1
            dt = time.monotonic() - t0
            with self._lock:
                self.frame = frame
                if dt >= 0.5:
                    self.measured_fps = count / dt
                    self.stats = frame_stats(frame)
                    count, t0 = 0, time.monotonic()

    def latest(self):
        with self._lock:
            return None if self.frame is None else self.frame.copy()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self.cap:
            self.cap.release()

    def state(self) -> dict:
        width, height, fps, fourcc = self.actual() if self.cap else (0, 0, 0, "?")
        controls = read_controls(self.device.path)
        ordered = [n for n in CONTROL_ORDER if n in controls]
        ordered += [n for n in sorted(controls) if n not in ordered]
        with self._lock:
            stats = dict(self.stats)
        hint = exposure_hint(self.device.path, stats)
        if hint:
            stats["notes"] = [*stats.get("notes", []), hint]
        return {
            "key": self.device.usb_port,
            "path": self.device.path,
            "label": self.device.label,
            "width": width,
            "height": height,
            "fps_set": round(fps, 1),
            "fps_actual": round(self.measured_fps, 1),
            "fourcc": fourcc,
            "swap_rb": self.swap_rb,
            "error": self.error,
            "stats": stats,
            "controls": [controls[n].as_dict() for n in ordered],
        }


def overlay(frame, camera: Camera):
    width, height, _, fourcc = camera.actual()
    stats = camera.stats
    lines = [
        f"{camera.device.label}   {width}x{height} {fourcc}   {camera.measured_fps:.1f} fps",
    ]
    if stats:
        lines.append(
            f"mean {stats['mean']:.0f}  RGB {stats['r']:.0f}/{stats['g']:.0f}/{stats['b']:.0f}"
            f"  blown {stats['blown_frac']:.0%}"
        )
        lines.extend(stats["notes"][:2])
    if camera.swap_rb:
        lines.append("R/B swapped")
    if not lines:
        return frame

    # Text sits on a darkened band rather than on a heavy dark stroke of itself. The
    # stroke-under-fill idiom camera_preview.py uses is broken as of OpenCV 5.0: glyph
    # advance now grows with thickness (getTextSize of one string is 387 px at thickness
    # 1 and 409 px at 3), so the thick dark copy drifts right of the thin bright one and
    # every line comes out visibly doubled.
    font, scale, thickness, pad, step = cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1, 8, 22
    widths = [cv2.getTextSize(text, font, scale, thickness)[0][0] for text in lines]
    box_w = min(frame.shape[1], max(widths) + pad * 2)
    box_h = min(frame.shape[0], step * len(lines) + pad)
    band = frame[:box_h, :box_w]
    band[:] = (band * 0.35).astype(np.uint8)
    for n, text in enumerate(lines):
        cv2.putText(
            frame, text, (pad, 18 + n * step), font, scale, (0, 255, 0), thickness, cv2.LINE_AA
        )
    return frame


# --- browser view -------------------------------------------------------------------

PAGE = """<!doctype html>
<title>camera debug</title>
<style>
  :root { color-scheme: dark; --bg:#14161a; --panel:#1d2027; --line:#2e323b; --fg:#e6e8ec;
          --dim:#9aa1ad; --accent:#6ea8fe; --warn:#ffb35c; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  header { padding:10px 16px; border-bottom:1px solid var(--line); display:flex; gap:16px;
           align-items:baseline; flex-wrap:wrap; }
  h1 { font-size:14px; margin:0; font-weight:600; }
  .cams { display:flex; flex-wrap:wrap; gap:16px; padding:16px; align-items:flex-start; }
  .cam { background:var(--panel); border:1px solid var(--line); border-radius:8px;
         padding:12px; flex:1 1 460px; min-width:min(100%, 360px); }
  .cam h2 { font-size:13px; margin:0 0 8px; font-weight:600; }
  .cam img { width:100%; display:block; border-radius:4px; background:#000; }
  .meta { color:var(--dim); margin:8px 0; font-size:12px; }
  .notes { color:var(--warn); min-height:1.5em; }
  .ctrl { display:grid; grid-template-columns:1fr 108px 60px; gap:8px; align-items:center;
          padding:3px 0; }
  .ctrl label { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ctrl.inactive label { color:var(--dim); }
  .ctrl .val { text-align:right; color:var(--dim); }
  input[type=range] { width:100%; accent-color:var(--accent); }
  select, button { background:#262a33; color:var(--fg); border:1px solid var(--line);
                   border-radius:4px; padding:3px 8px; font:inherit; }
  button { cursor:pointer; }
  button:hover { border-color:var(--accent); }
  .row { display:flex; gap:8px; margin:10px 0; flex-wrap:wrap; }
  .lock { color:var(--warn); font-size:11px; }
  #log { padding:6px 16px; color:var(--dim); border-top:1px solid var(--line);
         white-space:pre-wrap; min-height:2.5em; }
</style>
<header>
  <h1>camera debug</h1>
  <span class="meta" id="hint">drag a slider — it writes straight to the camera over V4L2</span>
</header>
<div class="cams" id="cams"></div>
<div id="log"></div>
<script>
const cams = document.getElementById('cams');
const log = document.getElementById('log');
let built = false;

function say(msg) { log.textContent = msg; }

async function post(path, body) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                              body: JSON.stringify(body)});
  const j = await r.json();
  say(j.message || '');
  refresh();
  return j;
}

function build(state) {
  cams.innerHTML = '';
  for (const cam of state.cameras) {
    const el = document.createElement('div');
    el.className = 'cam';
    el.innerHTML = `
      <h2>${cam.label} <span class="meta">${cam.path}</span></h2>
      <img src="/stream.mjpg?cam=${cam.key}" alt="${cam.label}">
      <div class="meta" id="meta-${cam.key}"></div>
      <div class="notes" id="notes-${cam.key}"></div>
      <div class="row">
        <button data-act="reset" data-cam="${cam.key}">reset to defaults</button>
        <button data-act="snapshot" data-cam="${cam.key}">save snapshot</button>
        <button data-act="swap" data-cam="${cam.key}">swap R/B</button>
      </div>
      <div id="ctrls-${cam.key}"></div>`;
    cams.appendChild(el);

    const host = el.querySelector(`#ctrls-${CSS.escape(cam.key)}`);
    for (const c of cam.controls) {
      if (c.read_only || c.kind === 'button') continue;
      const row = document.createElement('div');
      row.className = 'ctrl' + (c.inactive ? ' inactive' : '');
      const id = `${cam.key}-${c.name}`;
      let input;
      if (c.kind === 'menu' || c.kind === 'intmenu') {
        input = `<select id="in-${id}">` + Object.entries(c.menu).map(([k,v]) =>
          `<option value="${k}" ${Number(k)===c.value?'selected':''}>${v}</option>`).join('') +
          `</select>`;
      } else if (c.kind === 'bool') {
        input = `<select id="in-${id}">
                   <option value="0" ${!c.value?'selected':''}>off</option>
                   <option value="1" ${c.value?'selected':''}>on</option></select>`;
      } else {
        input = `<input type="range" id="in-${id}" min="${c.min}" max="${c.max}"
                        step="${c.step||1}" value="${c.value}">`;
      }
      // The lock note is the whole reason this page exists: V4L2 refuses writes to a
      // manual control while its auto counterpart owns it, with an error that says
      // "Permission denied" and means nothing of the sort.
      const lock = c.inactive && c.gate ? `<span class="lock"> (${c.gate} owns it)</span>` : '';
      row.innerHTML = `<label for="in-${id}" title="${c.name}">${c.name}${lock}</label>
                       ${input}<span class="val" id="val-${id}">${c.value}</span>`;
      host.appendChild(row);

      const field = row.querySelector(`#in-${CSS.escape(id)}`);
      const send = () => post('/api/control',
        {cam: cam.key, name: c.name, value: Number(field.value)});
      field.addEventListener('change', send);
      if (field.type === 'range') {
        field.addEventListener('input', () => {
          document.getElementById(`val-${id}`).textContent = field.value;
        });
      }
    }
  }
  cams.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    const cam = btn.dataset.cam;
    if (btn.dataset.act === 'reset') post('/api/reset', {cam});
    if (btn.dataset.act === 'snapshot') post('/api/snapshot', {cam});
    if (btn.dataset.act === 'swap') post('/api/swap', {cam});
  }, {once: true});
  built = true;
}

function paint(state) {
  for (const cam of state.cameras) {
    const meta = document.getElementById(`meta-${cam.key}`);
    if (!meta) { built = false; return; }
    const s = cam.stats || {};
    meta.textContent = `${cam.width}x${cam.height} ${cam.fourcc}  `
      + `${cam.fps_actual} fps (set ${cam.fps_set})  `
      + (s.mean !== undefined ? `mean ${s.mean}  R${s.r} G${s.g} B${s.b}  `
         + `blown ${(s.blown_frac*100).toFixed(1)}%` : '')
      + (cam.swap_rb ? '  [R/B swapped]' : '');
    document.getElementById(`notes-${cam.key}`).textContent =
      cam.error ? cam.error : (s.notes || []).join(' · ');
    for (const c of cam.controls) {
      const id = `${cam.key}-${c.name}`;
      const field = document.getElementById(`in-${id}`);
      const val = document.getElementById(`val-${id}`);
      if (field && document.activeElement !== field) field.value = c.value;
      if (val) val.textContent = c.value;
      const row = field && field.closest('.ctrl');
      if (row) row.classList.toggle('inactive', c.inactive);
    }
  }
}

async function refresh() {
  const state = await (await fetch('/api/state')).json();
  // Controls appear and disappear as autos are toggled, so the panel is rebuilt only
  // when the shape changes -- rebuilding every second would fight the slider you hold.
  const shape = state.cameras.map(c => c.key + ':' + c.controls.length).join('|');
  if (!built || shape !== refresh.shape) { refresh.shape = shape; build(state); }
  paint(state);
}
refresh();
setInterval(refresh, 1000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    # Class-level because http.server instantiates a handler per request; `serve()`
    # fills these in once before the server starts.
    cameras: ClassVar[dict[str, Camera]] = {}
    snapshot_dir: ClassVar[Path] = Path(".")
    quality: ClassVar[int] = 80

    def log_message(self, *args):  # quiet: one line per frame is not useful
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _camera(self, key: str | None) -> Camera | None:
        if key in self.cameras:
            return self.cameras[key]
        return next(iter(self.cameras.values()), None)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        cam = self._camera((query.get("cam") or [None])[0])

        if url.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/state":
            self._json({"cameras": [c.state() for c in self.cameras.values()]})
        elif url.path == "/snapshot.jpg" and cam:
            frame = cam.latest()
            if frame is None:
                self._json({"error": "no frame yet"}, 503)
                return
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(buf)))
            self.end_headers()
            self.wfile.write(buf.tobytes())
        elif url.path == "/stream.mjpg" and cam:
            self._stream(cam)
        else:
            self._json({"error": "not found"}, 404)

    def _stream(self, cam: Camera) -> None:
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
        self.end_headers()
        last = None
        try:
            while True:
                frame = cam.latest()
                if frame is None or frame is last:
                    time.sleep(0.005)
                    continue
                last = frame
                overlay(frame, cam)
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                if not ok:
                    continue
                self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(buf)}\r\n\r\n".encode())
                self.wfile.write(buf.tobytes())
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # viewer closed the tab

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "message": "bad JSON"}, 400)
            return
        cam = self._camera(body.get("cam"))
        if cam is None:
            self._json({"ok": False, "message": "no such camera"}, 404)
            return
        path = urlparse(self.path).path

        if path == "/api/control":
            ok, message = set_control(cam.device.path, body["name"], int(body["value"]))
            self._json({"ok": ok, "message": message})
        elif path == "/api/reset":
            problems = reset_controls(cam.device.path)
            self._json(
                {"ok": not problems, "message": "; ".join(problems) or "controls reset to defaults"}
            )
        elif path == "/api/swap":
            cam.swap_rb = not cam.swap_rb
            self._json({"ok": True, "message": f"swap R/B {'on' if cam.swap_rb else 'off'}"})
        elif path == "/api/snapshot":
            frame = cam.latest()
            if frame is None:
                self._json({"ok": False, "message": "no frame yet"}, 503)
                return
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            out = self.snapshot_dir / f"cam{cam.device.usb_port}-{time.strftime('%Y%m%d-%H%M%S')}.png"
            cv2.imwrite(str(out), frame)
            self._json({"ok": True, "message": f"wrote {out}"})
        else:
            self._json({"ok": False, "message": "not found"}, 404)


def lan_address() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 1))  # TEST-NET-1: routed nowhere, just names the interface
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def serve(cameras: list[Camera], port: int, snapshot_dir: Path, quality: int) -> int:
    Handler.cameras = {c.device.usb_port: c for c in cameras}
    Handler.snapshot_dir = snapshot_dir
    Handler.quality = quality
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"\n  http://localhost:{port}      (VS Code forwards this port to your laptop)")
    print(f"  http://{lan_address()}:{port}   (same network, no forwarding needed)\n")
    print("Ctrl-C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        server.shutdown()
    return 0


# --- other views --------------------------------------------------------------------


def show_windows(cameras: list[Camera]) -> int:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        # The Pi runs labwc with Xwayland, so :0 exists even when this shell has no
        # DISPLAY -- an ssh/VS Code session simply is not the desktop session.
        if Path("/tmp/.X11-unix/X0").exists():
            os.environ["DISPLAY"] = ":0"
            os.environ.setdefault("XAUTHORITY", str(Path.home() / ".Xauthority"))
            print("DISPLAY was unset; using :0 — the window opens on the Pi's own monitor.")
        else:
            print("No display found. Use the default browser view instead.", file=sys.stderr)
            return 1
    for cam in cameras:
        cv2.namedWindow(cam.device.label, cv2.WINDOW_NORMAL)
    print("keys: q quit   w snapshot   x swap R/B   0 reset controls")
    try:
        while True:
            for cam in cameras:
                frame = cam.latest()
                if frame is not None:
                    cv2.imshow(cam.device.label, overlay(frame, cam))
            key = cv2.waitKey(20) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord("x"):
                for cam in cameras:
                    cam.swap_rb = not cam.swap_rb
            elif key == ord("0"):
                for cam in cameras:
                    print("\n".join(reset_controls(cam.device.path)) or "controls reset")
            elif key == ord("w"):
                stamp = time.strftime("%Y%m%d-%H%M%S")
                for cam in cameras:
                    frame = cam.latest()
                    if frame is not None:
                        out = f"cam{cam.device.usb_port}-{stamp}.png"
                        cv2.imwrite(out, frame)
                        print(f"wrote {out}")
    finally:
        cv2.destroyAllWindows()
    return 0


def record_clip(cameras: list[Camera], seconds: float, out: Path, overlay_on: bool) -> int:
    """Record each camera to an mp4 -- the fallback when there is no display and no browser."""
    writers = {}
    for cam in cameras:
        width, height, fps, _ = cam.actual()
        rate = cam.measured_fps if cam.measured_fps > 1 else (fps if fps > 1 else 30.0)
        path = out if len(cameras) == 1 else out.with_name(f"{out.stem}-{cam.device.usb_port}{out.suffix}")
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), rate, (width, height))
        if not writer.isOpened():
            print(f"could not open {path} for writing", file=sys.stderr)
            return 1
        writers[cam.device.usb_port] = (writer, path, rate)
        print(f"recording {cam.device.label} -> {path} ({width}x{height} @ {rate:.1f} fps)")

    deadline = time.monotonic() + seconds
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        for cam in cameras:
            frame = cam.latest()
            if frame is None or frame is last.get(cam.device.usb_port):
                continue
            last[cam.device.usb_port] = frame
            writers[cam.device.usb_port][0].write(overlay(frame, cam) if overlay_on else frame)
        time.sleep(0.005)
    for writer, path, _ in writers.values():
        writer.release()
        print(f"wrote {path}  ({path.stat().st_size / 1e6:.1f} MB)")
    return 0


def write_snapshot(cameras: list[Camera], out: Path, overlay_on: bool) -> int:
    for cam in cameras:
        # Wait for the first stats window rather than the first frame. A UVC camera opens
        # into whatever exposure and white balance it last had, and takes a second or so
        # to converge -- grabbing frame 1 photographs the transient, not the camera.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not cam.stats:
            time.sleep(0.02)
        frame = cam.latest()
        if frame is None:
            print(f"{cam.device.label}: no frame", file=sys.stderr)
            continue
        path = out if len(cameras) == 1 else out.with_name(f"{out.stem}-{cam.device.usb_port}{out.suffix}")
        cv2.imwrite(str(path), overlay(frame, cam) if overlay_on else frame)
        stats = frame_stats(frame)
        print(f"wrote {path}   mean {stats['mean']}  R{stats['r']} G{stats['g']} B{stats['b']}")
        hint = exposure_hint(cam.device.path, stats)
        for note in [*stats["notes"], *([hint] if hint else [])]:
            print(f"    {note}")
    return 0


def print_listing(include_all: bool) -> int:
    devices = enumerate_devices(include_all)
    if not devices:
        print("No cameras found. Check `ls /dev/video*` and that you are in the `video` group.")
        return 1
    for device in devices:
        print(f"\n{device.path}   {device.card}   driver {device.driver}")
        print(f"    usb port {device.usb_port}   ({device.bus})")
        print(f"    --camera {device.usb_port}   <- stable across replug and reboot")
        for fourcc, sizes in read_formats(device.path):
            top = sorted(sizes, key=lambda s: (s[0] * s[1], s[2]))[-4:]
            summary = ", ".join(f"{w}x{h}@{fps:g}" for w, h, fps in reversed(top))
            print(f"    {fourcc}: {summary}")
        controls = read_controls(device.path)
        locked = [f"{n} (locked by {gate_for(n)})" for n, c in controls.items() if c.inactive]
        print(f"    {len(controls)} controls" + (f"; inactive: {', '.join(locked)}" if locked else ""))
    if len({d.card for d in devices}) < len(devices):
        print(
            "\nTwo cameras report the same name, so /dev/v4l/by-id collides for them and "
            "\nindices can swap on reboot. The usb port above is what tells them apart."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="camera_debug",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--camera",
        action="append",
        default=None,
        metavar="ID",
        help="index, /dev/videoN, usb port (e.g. 1.4), or name substring; repeatable. "
        "Default: every camera found.",
    )
    parser.add_argument("--list", action="store_true", help="show cameras, formats, controls; exit")
    parser.add_argument("--all-devices", action="store_true", help="--list also shows ISP/codec nodes")
    parser.add_argument("--view", choices=("browser", "window", "clip", "snapshot"), default="browser")
    parser.add_argument("--port", type=int, default=8088, help="browser view port (default 8088)")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=float, default=30)
    parser.add_argument(
        "--fourcc",
        default="MJPG",
        help="pixel format (default MJPG: these cameras do 720p30 compressed but only "
        "720p10 as raw YUYV, and OpenCV picks YUYV by itself)",
    )
    parser.add_argument("--seconds", type=float, default=10.0, help="--view clip duration")
    parser.add_argument("--out", type=Path, default=None, help="--view clip/snapshot output path")
    parser.add_argument("--snapshot-dir", type=Path, default=Path("."), help="where the browser saves stills")
    parser.add_argument("--quality", type=int, default=80, help="MJPEG stream JPEG quality")
    parser.add_argument("--no-overlay", action="store_true", help="omit the text overlay")
    parser.add_argument("--reset", action="store_true", help="restore driver defaults before starting")
    args = parser.parse_args(argv)

    if args.list:
        return print_listing(args.all_devices)

    devices = enumerate_devices()
    if not devices:
        print("No cameras found (see --list).", file=sys.stderr)
        return 1
    if args.camera:
        selected = []
        for token in args.camera:
            device = resolve_camera(token, devices)
            if device is None:
                print(f"--camera {token}: no unique match (see --list)", file=sys.stderr)
                return 1
            selected.append(device)
    else:
        selected = devices

    cameras: list[Camera] = []
    for device in selected:
        if args.reset:
            reset_controls(device.path)
        camera = Camera(device, args.width, args.height, args.fps, args.fourcc)
        if not camera.open():
            print(f"{device.label}: {camera.error}", file=sys.stderr)
            continue
        width, height, fps, fourcc = camera.actual()
        asked = f"{args.width}x{args.height} {args.fourcc}"
        got = f"{width}x{height} {fourcc}"
        note = "" if asked == got else f"   <- asked {asked}, camera chose this instead"
        print(f"{device.label}: {got} @ {fps:g} fps{note}")
        camera.start()
        cameras.append(camera)
    if not cameras:
        return 1

    try:
        if args.view == "browser":
            return serve(cameras, args.port, args.snapshot_dir, args.quality)
        if args.view == "window":
            return show_windows(cameras)
        if args.view == "clip":
            return record_clip(cameras, args.seconds, args.out or Path("camera.mp4"), not args.no_overlay)
        return write_snapshot(cameras, args.out or Path("camera.png"), not args.no_overlay)
    finally:
        for camera in cameras:
            camera.stop()


if __name__ == "__main__":
    raise SystemExit(main())
