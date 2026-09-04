"""Op handlers for the hand gateway (ADR-0002/0004), independent of the WS transport.

`GatewayService` owns the `HandBus`, the `SafetyClamp`, and the `Watchdog`; every op that can
move a servo runs through the clamp, and the watchdog is fed only by the explicit `heartbeat`
op. `server.py` maps each WS `op` to one of these methods and wraps the envelope.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from clankers.config import HandConfig, load_robots
from clankers.safety import SafetyClamp, Watchdog, clamp_step, decode_dynamixel_error

from .bus import HandBus

logger = logging.getLogger(__name__)

API_VERSION = "clankers-hand-v1"

OP_NAMES = [
    "capabilities",
    "scan",
    "enable",
    "disable",
    "pos",
    "jog",
    "state_once",
    "state_stream",
    "error_status",
    "reboot",
    "set_current_limit",
    "set_mechanical_zero",
    "restore_homing_offsets",
    "heartbeat",
]

# ADR-0004: LEAP V1 default is 300 mA, raisable toward ~550 on V1 only (V2 forbids raising).
# This is a hard backstop independent of `hand.current_limit_ma` in robots.yaml.
MAX_CURRENT_LIMIT_MA = 550.0


class GatewayError(Exception):
    """An op-level error to report over the WS envelope (`{"ok": false, "error": str}`)."""


class GatewayService:
    def __init__(
        self,
        bus: HandBus,
        hand_cfg: HandConfig | None = None,
        *,
        allow_uncalibrated: bool = False,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.bus = bus
        self.hand_cfg = hand_cfg or load_robots().hand
        self.allow_uncalibrated = allow_uncalibrated
        self.on_event = on_event

        self.clamp = SafetyClamp.for_hand(self.hand_cfg)
        self.watchdog = Watchdog(
            interval_s=float(self.hand_cfg.safety["heartbeat_interval_s"]),
            multiplier=float(self.hand_cfg.safety["watchdog_multiplier"]),
            on_trip=self._on_watchdog_trip,
            clock=clock,
        )

        self._servo_ids = [j.servo_id for j in self.hand_cfg.joints]
        self._joint_by_name = {j.name: j for j in self.hand_cfg.joints}
        self._joint_by_servo_id = {j.servo_id: j for j in self.hand_cfg.joints}
        self._enabled_ids: set[int] = set()

    # -- ops ---------------------------------------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        return {"api_version": API_VERSION, "vendor": "dynamixel", "ops": list(OP_NAMES)}

    def scan(self, baud: int | None = None) -> dict[str, Any]:
        hits = self.bus.scan(baud)
        seen: set[int] = set()
        duplicates: list[int] = []
        for h in hits:
            sid = int(h["servo_id"])
            if sid in seen and sid not in duplicates:
                duplicates.append(sid)
            seen.add(sid)
        return {"hits": hits, "duplicates": duplicates}

    def enable(self, servo_ids: list[int] | None = None) -> dict[str, Any]:
        ids = list(servo_ids) if servo_ids is not None else list(self._servo_ids)
        self._check_ids(ids)
        # ADR-0004: a latched hardware fault requires explicit acknowledgment (`reboot`);
        # refusing here prevents an enable/retry loop from papering over a real fault.
        faulted = {
            sid: [f.value for f in decode_dynamixel_error(int(h["error_bits"]))]
            for sid, h in self.bus.read_health().items()
            if sid in ids and int(h.get("error_bits", 0)) != 0
        }
        if faulted:
            raise GatewayError(
                f"refusing to enable servos with unacknowledged faults {faulted}; "
                "clear each with the `reboot` op first (ADR-0004)"
            )
        self.bus.enable_torque(ids)
        # Reseed the clamp from live positions on EVERY enable: while torque was off the
        # hand is back-drivable, so any stale last-target would let the next `pos` command
        # a far jump that still looks like one small step.
        positions = {
            self._joint_by_servo_id[sid].name: s["pos"]
            for sid, s in self.bus.read_state().items()
            if sid in self._joint_by_servo_id
        }
        self.clamp.reset(positions)
        # (Re-)arm the watchdog: a prior trip leaves torque off until an operator re-enables.
        self._enabled_ids.update(ids)
        self.watchdog.start()
        return {"servo_ids": ids}

    def disable(self, servo_ids: list[int] | None = None) -> dict[str, Any]:
        ids = list(servo_ids) if servo_ids is not None else list(self._servo_ids)
        self._check_ids(ids)
        self.bus.disable_torque(ids)
        self._enabled_ids.difference_update(ids)
        if not self._enabled_ids:
            # All torque intentionally off: stop supervising so an idle bench session
            # doesn't later fire a spurious watchdog_trip.
            self.watchdog.disarm()
        return {"servo_ids": ids}

    def pos(self, targets: dict[str, float]) -> dict[str, Any]:
        if not self.hand_cfg.calibrated and not self.allow_uncalibrated:
            raise GatewayError(
                "hand.calibrated is false in robots.yaml; `pos` is refused until bring-up "
                "confirms the joint map (docs/plans/bringup.md) — use `jog` for single-servo "
                "moves, or start the gateway with --allow-uncalibrated"
            )
        # With a firmware motion profile the servo bounds its own speed, so slicing a pose
        # into max_step_rad pieces only means the operator has to press "go to zero" four
        # times to actually get there. Joint limits still apply either way; `jog` keeps the
        # step clamp, since it is a nudge primitive rather than a pose command.
        stepped = bool(self.hand_cfg.safety.get("step_clamp_pos", True))
        try:
            clamped = self.clamp.apply(targets) if stepped else self.clamp.apply_limits(targets)
        except KeyError as exc:
            raise GatewayError(str(exc)) from exc
        by_servo_id = {self._joint_by_name[name].servo_id: rad for name, rad in clamped.items()}
        self.bus.write_positions(by_servo_id)
        return {"targets": clamped}

    def jog(self, servo_id: int, delta_rad: float) -> dict[str, Any]:
        """Single-servo relative move, allowed even before `hand.calibrated` (bring-up path)."""
        joint = self._joint_by_servo_id.get(servo_id)
        if joint is None:
            raise GatewayError(f"unknown servo_id {servo_id}")
        state = self.bus.read_state()
        if servo_id not in state:
            raise GatewayError(f"no state for servo_id {servo_id}")
        current = state[servo_id]["pos"]
        max_step = float(self.hand_cfg.safety["max_step_rad"])
        try:
            step = clamp_step(0.0, float(delta_rad), max_step)
        except ValueError as exc:
            raise GatewayError(str(exc)) from exc
        target = joint.limit.clamp(current + step)
        self.bus.write_positions({servo_id: target})
        # Keep the shared clamp's last-target in sync so a later `pos` step-limits from
        # where the joint actually is, not from a pre-jog stale value.
        self.clamp.note(joint.name, target)
        return {"servo_id": servo_id, "target": target}

    def set_mechanical_zero(
        self, servo_ids: list[int] | None = None, *, confirm: bool = False
    ) -> dict[str, Any]:
        """Make the hand's current physical pose read as zero (Homing_Offset, EEPROM).

        Three guards, in order of how much they hurt to get wrong:

        - `confirm` must be set. This is the hand's counterpart to the arm's
          set_zero_position, and the arm's is what an operator can destroy by reflex.
        - Torque must be OFF on every targeted servo. The servo locks its EEPROM while
          torque is on, so a partial write would leave half the hand re-zeroed.
        - Faulted servos are excluded from the reading they would poison (ADR-0004).

        Unlike the arm's, this IS undoable: the previous offsets come back in the result,
        and `restore_homing_offsets` writes them again.
        """
        ids = list(servo_ids) if servo_ids is not None else list(self._servo_ids)
        self._check_ids(ids)
        if not confirm:
            raise GatewayError(
                "set_mechanical_zero rewrites Homing_Offset in EEPROM (wear-limited) and "
                "changes what 0 rad means for the hand; re-send with confirm=true"
            )
        live = self.bus.read_torque_enabled(ids)
        powered = sorted(sid for sid, on in live.items() if on)
        if powered:
            raise GatewayError(
                f"refusing to re-zero while torque is on for {powered}: the servo locks its "
                "EEPROM when powered, so the write would land on only some joints. Disable "
                "torque first, hold the hand in the pose you want to call zero, then retry"
            )
        result = self.bus.set_mechanical_zero(ids)
        # Positions now mean something different, so any remembered target is stale.
        positions = {
            self._joint_by_servo_id[sid].name: s["pos"]
            for sid, s in self.bus.read_state().items()
            if sid in self._joint_by_servo_id
        }
        self.clamp.reset(positions)
        return {
            "servo_ids": ids,
            "previous_offsets": {str(k): v for k, v in result["previous"].items()},
            "new_offsets": {str(k): v for k, v in result["new"].items()},
        }

    def restore_homing_offsets(
        self, offsets: dict[str, int], *, confirm: bool = False
    ) -> dict[str, Any]:
        """Undo a re-zero by writing back the offsets `set_mechanical_zero` returned."""
        if not confirm:
            raise GatewayError("restore_homing_offsets writes EEPROM; re-send with confirm=true")
        try:
            parsed = {int(k): int(v) for k, v in (offsets or {}).items()}
        except (TypeError, ValueError) as exc:
            raise GatewayError(f"offsets must be {{servo_id: ticks}}: {exc}") from exc
        if not parsed:
            raise GatewayError("no offsets given")
        ids = sorted(parsed)
        self._check_ids(ids)
        powered = sorted(sid for sid, on in self.bus.read_torque_enabled(ids).items() if on)
        if powered:
            raise GatewayError(f"refusing to write Homing_Offset while torque is on for {powered}")
        self.bus.write_homing_offsets(parsed)
        positions = {
            self._joint_by_servo_id[sid].name: s["pos"]
            for sid, s in self.bus.read_state().items()
            if sid in self._joint_by_servo_id
        }
        self.clamp.reset(positions)
        return {"servo_ids": ids}

    def state_once(self) -> dict[str, Any]:
        return {"joints": self.joints_payload(self.bus.read_state())}

    def state_stream(self, enabled: bool) -> dict[str, Any]:
        """Per-connection opt-in; `server.py` tracks the flag and runs the push loop."""
        return {"enabled": bool(enabled)}

    def error_status(self) -> dict[str, Any]:
        health = self.health_payload(self.bus.read_health())
        return {"faults": {str(h["servo_id"]): h["faults"] for h in health}}

    def reboot(self, servo_id: int) -> dict[str, Any]:
        if servo_id not in self._joint_by_servo_id:
            raise GatewayError(f"unknown servo_id {servo_id}")
        self.bus.reboot(servo_id)
        return {"servo_id": servo_id}

    def set_current_limit(self, ma: float) -> dict[str, Any]:
        ma = float(ma)
        if ma > MAX_CURRENT_LIMIT_MA:
            raise GatewayError(
                f"current limit {ma} mA exceeds the hard cap {MAX_CURRENT_LIMIT_MA} mA (ADR-0004)"
            )
        self.bus.set_current_limit(ma)
        data: dict[str, Any] = {"current_limit_ma": ma}
        if ma > self.hand_cfg.current_limit_ma:
            data["warning"] = (
                f"{ma} mA exceeds robots.yaml hand.current_limit_ma "
                f"({self.hand_cfg.current_limit_ma}); confirm hand.revision before sustained use"
            )
        return data

    def heartbeat(self) -> dict[str, Any]:
        self.watchdog.feed()
        return {"tripped": self.watchdog.tripped}

    # -- payload shaping, shared with server.py's periodic push --------------------------------

    def joints_payload(self, state: dict[int, dict[str, float]]) -> list[dict[str, Any]]:
        out = []
        for sid in sorted(state):
            joint = self._joint_by_servo_id.get(sid)
            s = state[sid]
            out.append(
                {
                    "servo_id": sid,
                    "name": joint.name if joint else None,
                    "pos": s["pos"],
                    "vel": s["vel"],
                    "current_ma": s["current_ma"],
                    # Clients need this to know whether EEPROM-writing ops are available at
                    # all: the servo locks its EEPROM while torque is on.
                    "torque_enabled": sid in self._enabled_ids,
                }
            )
        return out

    def health_payload(self, health: dict[int, dict[str, float]]) -> list[dict[str, Any]]:
        out = []
        for sid in sorted(health):
            h = health[sid]
            faults = [f.value for f in decode_dynamixel_error(int(h["error_bits"]))]
            out.append(
                {
                    "servo_id": sid,
                    "temp_c": h["temp_c"],
                    "voltage_v": h["voltage_v"],
                    "faults": faults,
                }
            )
        return out

    # -- internal ------------------------------------------------------------------------------

    def _check_ids(self, ids: list[int]) -> None:
        unknown = [sid for sid in ids if sid not in self._joint_by_servo_id]
        if unknown:
            raise GatewayError(f"unknown servo_id(s): {unknown}")

    def _on_watchdog_trip(self) -> None:
        gap = self.watchdog.trip_gap_s
        logger.warning(
            "hand watchdog tripped: no heartbeat for %.1fs (timeout %.1fs), disabling torque "
            "on all servos. Just over the timeout usually means the gateway itself stalled: "
            "bus I/O is synchronous on the event loop, so a slow or retrying serial read "
            "blocks heartbeat handling. Far over (5s+) points at the client instead.",
            gap if gap is not None else float("nan"),
            self.watchdog.timeout_s,
        )
        self.bus.disable_torque(list(self._servo_ids))
        self._enabled_ids.clear()
        if self.on_event is not None:
            self.on_event(
                {
                    "event": "watchdog_trip",
                    "gap_s": self.watchdog.trip_gap_s,
                    "timeout_s": self.watchdog.timeout_s,
                }
            )
