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
from .dynamixel_compat import (
    OPERATING_MODE_CURRENT_BASED_POSITION,
    OPERATING_MODE_NAMES,
    profile_register_values,
)

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
    "check_current_limit",
    "configure_current_limiting",
    "apply_motion_profile",
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
        faulted = self._faulted(ids)
        if faulted:
            raise GatewayError(
                f"refusing to enable servos with unacknowledged faults {faulted}; "
                "clear each with the `reboot` op first (ADR-0004)"
            )
        # EEPROM first: if the servo is not even in the mode that enforces a current cap,
        # restoring the RAM-side cap below would be meaningless.
        unconfigured = self._eeprom_mismatches(ids)
        if unconfigured:
            raise GatewayError(
                f"refusing to enable: {self._summarize_eeprom(unconfigured)}, but robots.yaml "
                f"wants mode {self._mode_name(self.hand_cfg.operating_mode)} and "
                f"Current_Limit {self.hand_cfg.current_limit_ma} mA. Until they match, nothing "
                "caps motor current: a blocked finger draws full stall current and can brown "
                "out the whole hand. With torque off, run "
                "scripts/configure_hand_current_limit.py once (the `configure_current_limiting` "
                "op, confirm=true); it writes EEPROM, so it survives reboots"
            )
        restored = self._ensure_ram_config(ids)
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
        return {"servo_ids": ids, "ram_config_restored": restored}

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

    def apply_motion_profile(self) -> dict[str, Any]:
        """Re-push Profile_Velocity/Acceleration to every servo.

        These are RAM registers, so unlike Current_Limit they do NOT survive a reboot or a
        power cycle: a servo that was rebooted to clear a fault, unplugged, browned out, or
        hot-swapped comes back with the factory default of 0, which does not mean "slow" but
        "no profile at all" -- it drives at full speed toward every goal while its
        neighbours still ramp. `enable` now checks and restores the profile itself, so this
        op is for pushing it by hand, not the only line of defence.
        """
        applied = self.bus.apply_motion_profile()
        logger.info("motion profile re-applied: %s", applied)
        return applied

    def check_current_limit(self) -> dict[str, Any]:
        """Is motor current really capped at `hand.current_limit_ma`? Complain loudly if not.

        That takes two EEPROM settings, not one. Current_Limit is the obvious one, but the
        factory Operating_Mode (3, position) ignores it entirely: the position loop drives
        PWM -- a voltage -- straight from the position error, so a stalled servo draws full
        stall current (~1.47 A) whatever the limit says, and several stalled at once sag the
        rail far enough to latch input-voltage faults across the chain. Only mode 5 puts a
        current loop under the position loop and caps it.

        Read-only: EEPROM is wear-limited and CLAUDE.md wants its writes batched and
        confirmed, which is `configure_current_limiting`.
        """
        want_ma = float(self.hand_cfg.current_limit_ma)
        want_mode = self.hand_cfg.operating_mode
        try:
            wrong = self._eeprom_mismatches(list(self._servo_ids))
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("could not read Current_Limit/Operating_Mode: %s", exc)
            return {"checked": False, "configured_ma": want_ma, "configured_operating_mode": want_mode}
        if wrong:
            logger.error(
                "the servos are not configured as robots.yaml says (operating mode %s, "
                "Current_Limit %.0f mA): %s. robots.yaml alone does not reach the hardware, and "
                "until it does nothing caps motor current. `enable` will refuse. With torque "
                "off, run scripts/configure_hand_current_limit.py once.",
                self._mode_name(want_mode), want_ma, self._summarize_eeprom(wrong),
            )
        return {
            "checked": True,
            "configured_ma": want_ma,
            "configured_operating_mode": want_mode,
            "mismatched": {
                str(sid): f["current_limit_ma"]
                for sid, f in sorted(wrong.items()) if "current_limit_ma" in f
            },
            "mode_mismatched": {
                str(sid): f["operating_mode"]
                for sid, f in sorted(wrong.items()) if "operating_mode" in f
            },
        }

    def configure_current_limiting(self, *, confirm: bool = False) -> dict[str, Any]:
        """Write Operating_Mode and Current_Limit from robots.yaml into each servo's EEPROM.

        The one-time setup that makes `hand.current_limit_ma` real (see check_current_limit).
        Guarded like `set_mechanical_zero`:

        - `confirm` must be set: EEPROM is wear-limited and survives power-off.
        - Torque must be OFF on every servo: the servo locks its EEPROM while powered, so a
          partial batch would leave the hand in mixed modes.
        - No latched faults: a faulted servo answers every write with an error.

        Only registers that differ are written, and all of them are read back. To undo,
        change robots.yaml and run this again; `previous` in the result records what each
        servo held before.
        """
        ids = list(self._servo_ids)
        if not confirm:
            raise GatewayError(
                "configure_current_limiting rewrites Operating_Mode and Current_Limit in EEPROM "
                "(wear-limited, survives power-off) on every servo that differs from "
                "robots.yaml; re-send with confirm=true"
            )
        powered = sorted(sid for sid, on in self.bus.read_torque_enabled(ids).items() if on)
        if powered:
            raise GatewayError(
                f"refusing while torque is on for {powered}: the servo locks its EEPROM when "
                "powered, so the batch would land on only some joints. Disable torque first "
                "(support the hand -- it goes limp)"
            )
        faulted = self._faulted(ids)
        if faulted:
            raise GatewayError(
                f"refusing with unacknowledged faults {faulted}: a faulted servo rejects writes. "
                "Clear each with the `reboot` op first"
            )
        wrong = self._eeprom_mismatches(ids)
        if wrong:
            try:
                if any("current_limit_ma" in f for f in wrong.values()):
                    # Ceiling first: the servo refuses a Goal_Current above it.
                    self.bus.set_current_limit(float(self.hand_cfg.current_limit_ma))
                modes = {
                    sid: self.hand_cfg.operating_mode
                    for sid, f in wrong.items() if "operating_mode" in f
                }
                if modes:
                    self.bus.write_operating_modes(modes)
            except (ConnectionError, OSError, RuntimeError) as exc:
                still = self._eeprom_mismatches(ids)
                raise GatewayError(
                    f"EEPROM write failed part-way ({exc}); still wrong: "
                    f"{self._summarize_eeprom(still) or 'nothing'}. Re-run to finish -- only "
                    "servos that still differ are written"
                ) from exc
            still = self._eeprom_mismatches(ids)
            if still:
                raise GatewayError(
                    f"EEPROM write did not take: {self._summarize_eeprom(still)} after writing. "
                    "Check `error_status` and the cabling, then re-run"
                )
            logger.warning(
                "wrote EEPROM on servo(s) %s: operating mode %s, Current_Limit %d mA "
                "(previously %s)",
                sorted(wrong), self._mode_name(self.hand_cfg.operating_mode),
                self.hand_cfg.current_limit_ma, self._summarize_eeprom(wrong),
            )
        # Goal_Current only means something once the mode is 5; push the RAM side now so the
        # first `enable` afterwards has nothing to restore.
        self._apply_ram_config()
        return {
            "changed": sorted(wrong),
            "previous": {str(sid): f for sid, f in sorted(wrong.items())},
            "operating_mode": self.hand_cfg.operating_mode,
            "current_limit_ma": self.hand_cfg.current_limit_ma,
        }

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

    def _faulted(self, ids: list[int]) -> dict[int, list[str]]:
        return {
            sid: [f.value for f in decode_dynamixel_error(int(h["error_bits"]))]
            for sid, h in self.bus.read_health().items()
            if sid in ids and int(h.get("error_bits", 0)) != 0
        }

    @staticmethod
    def _mode_name(mode: int) -> str:
        return f"{mode} ({OPERATING_MODE_NAMES.get(int(mode), 'unknown')})"

    def _eeprom_mismatches(self, ids: list[int]) -> dict[int, dict[str, float]]:
        """`{servo_id: {setting: live value}}` for each EEPROM setting that differs from
        robots.yaml. These survive reboots, so a mismatch is never fixed behind anyone's
        back -- only by the confirmed `configure_current_limiting`."""
        want_ma = float(self.hand_cfg.current_limit_ma)
        out: dict[int, dict[str, float]] = {}
        for sid, mode in self.bus.read_operating_modes().items():
            if sid in ids and mode != self.hand_cfg.operating_mode:
                out.setdefault(sid, {})["operating_mode"] = mode
        for sid, ma in self.bus.read_current_limits().items():
            if sid in ids and abs(ma - want_ma) > 1.0:
                out.setdefault(sid, {})["current_limit_ma"] = ma
        return out

    def _summarize_eeprom(self, wrong: dict[int, dict[str, float]]) -> str:
        """Group by value, so sixteen factory-fresh servos read as one line, not sixteen."""
        by_mode: dict[int, list[int]] = {}
        by_limit: dict[float, list[int]] = {}
        for sid, f in sorted(wrong.items()):
            if "operating_mode" in f:
                by_mode.setdefault(int(f["operating_mode"]), []).append(sid)
            if "current_limit_ma" in f:
                by_limit.setdefault(f["current_limit_ma"], []).append(sid)
        parts = [f"servo(s) {ids} in mode {self._mode_name(m)}" for m, ids in by_mode.items()]
        parts += [f"servo(s) {ids} at Current_Limit {ma:.0f} mA" for ma, ids in by_limit.items()]
        return "; ".join(parts)

    def _apply_ram_config(self) -> None:
        self.bus.apply_motion_profile()
        if self.hand_cfg.operating_mode == OPERATING_MODE_CURRENT_BASED_POSITION:
            self.bus.write_goal_currents(float(self.hand_cfg.current_limit_ma))

    def _ram_mismatches(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        wanted = profile_register_values(self.hand_cfg.profile)
        for sid, live in self.bus.read_motion_profiles().items():
            if sid in ids and live != wanted:
                out.setdefault(sid, {})["profile"] = live
        if self.hand_cfg.operating_mode == OPERATING_MODE_CURRENT_BASED_POSITION:
            want_ma = float(self.hand_cfg.current_limit_ma)
            for sid, ma in self.bus.read_goal_currents().items():
                if sid in ids and abs(ma - want_ma) > 1.0:
                    out.setdefault(sid, {})["goal_current_ma"] = ma
        return out

    def _ensure_ram_config(self, ids: list[int]) -> list[int]:
        """Make sure every servo about to get torque has robots.yaml's RAM settings loaded:
        the motion profile, and in mode 5 the Goal_Current cap.

        RAM is written at connect and nowhere else, and anything that restarts a servo in
        between -- the `reboot` that clears a latched fault, a brownout, an unplugged joint
        -- silently resets it: a profile of 0 means full speed. Torque is RAM too and comes
        back OFF from every one of those, so `enable` is the one door every such servo must
        pass through before it can move again, and checking here covers all of them.
        Returns the servo ids that had to be restored.
        """
        stale = self._ram_mismatches(ids)
        if not stale:
            return []
        logger.warning(
            "servo(s) %s lost their RAM settings (read %s) -- a reboot, brownout or power loss "
            "resets them, and a profile of 0 means full speed. Re-applying before enabling.",
            sorted(stale), stale,
        )
        self._apply_ram_config()
        still = self._ram_mismatches(ids)
        if still:
            raise GatewayError(
                f"refusing to enable: servo(s) {sorted(still)} will not take the configured "
                f"motion profile / Goal_Current (read back {still}); without them a servo "
                "moves at full speed or pushes uncapped. Check `error_status` and "
                "`check_current_limit`, then retry"
            )
        return sorted(stale)

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
