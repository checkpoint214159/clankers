"""Tests for the hardware-free parts of clankers-detect (classification + wiring)."""

from __future__ import annotations

from clankers.detect import PROBE_BAUDS, BusProbe, classify_device


def test_classify_macos_damiao_bridge() -> None:
    c = classify_device("/dev/cu.usbmodem00000000050C1")
    assert c.kind == "damiao-bridge"
    assert "arm" in c.label


def test_classify_macos_u2d2() -> None:
    c = classify_device("/dev/cu.usbserial-FTBIN8NW")
    assert c.kind == "u2d2"
    assert "hand" in c.label


def test_classify_linux_equivalents() -> None:
    assert classify_device("/dev/ttyACM0").kind == "damiao-bridge"
    assert classify_device("/dev/ttyUSB0").kind == "u2d2"


def test_classify_unknown() -> None:
    assert classify_device("/dev/cu.Bluetooth-Incoming-Port").kind == "unknown"


def test_probe_bauds_start_with_leap_default() -> None:
    assert PROBE_BAUDS[0] == 4_000_000  # LEAP builds default to 4M
    assert 57_600 in PROBE_BAUDS  # factory-fresh servos


def test_busprobe_flags_intermittent_servos() -> None:
    """A servo that answers some rounds but not all is the failure a single ping hides."""
    probe = BusProbe(
        baud=4_000_000,
        rounds=10,
        models={0: "xl330-m288", 15: "xl330-m288"},
        answered={0: 10, 15: 3},
    )
    assert probe.flaky == {15: 3}


def test_busprobe_clean_bus_has_no_flaky_servos() -> None:
    probe = BusProbe(
        baud=4_000_000, rounds=10, models={0: "xl330-m288"}, answered={0: 10}
    )
    assert probe.flaky == {}
