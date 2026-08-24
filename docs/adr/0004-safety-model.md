# ADR-0004: Safety model

**Status:** Accepted · **Date:** 2026-08-24

## Context

A 22-DOF system driven by policies, tasks, sliders, and scripts. Any single software layer
can be bypassed or buggy. Research checklist: Dynamixel shutdown register (addr 63) +
hardware-error status (addr 70); Damiao PMAX/VMAX/TMAX + status nibble; watchdog convention
3–5× control interval; CAN bus-off must be surfaced, never silently auto-recovered; EEPROM
writes are wear-limited; LEAP V1 default current limit 300 mA (raisable to ~550 on V1 —
V2 forbids raising; revision unconfirmed).

## Decision

Three enforced layers, outermost first:

1. **Firmware limits on the motors** (backstop, survives any software bug): Damiao
   PMAX/VMAX/TMAX per joint; XC330 position/current/temperature limits + shutdown register
   left at defaults (voltage+overheat+shock+overload). Current limit 300 mA until the hand
   revision is confirmed. Temperature limit 70 °C is never raised.
2. **`clankers.safety` clamps in every software command path** — gateway ops AND
   `Robot.send_action` (task runner and policies flow through these), not just the studio UI:
   joint-limit clamp, max-step clamp (`max_step_rad`), zero-safe epsilon, gripper MIT clamp.
3. **Watchdog + operator controls:** heartbeat required from any commanding client; missing
   `watchdog_multiplier × interval` → torque off. Faults (Dynamixel error bits, Damiao status)
   are normalized to one taxonomy {undervoltage, overvoltage, overcurrent, overtemp_mosfet,
   overtemp_motor, overload, comms_timeout, encoder_fault, bus_off} and require explicit
   operator acknowledgment to clear — a tripped Dynamixel shutdown needs an explicit reboot
   op by design. Big red stop in every UI surface.

Physical e-stop in the motor power path is a hardware purchase, tracked in bring-up docs —
software stop is not an e-stop.

## Known limitations (accepted 2026-08-24 adversarial review)

- **Shared heartbeat across gateway clients:** the hand gateway's watchdog is fed by any
  connected client, so a visualization-only client heartbeating can mask a dead commanding
  client. Acceptable for the single-operator debug gateway; revisit (per-connection command
  ownership) before any unattended multi-client use.
- **Arm-side `zero_safe_eps_rad` / gripper MIT clamps** are enforced in the studio UI layer
  (inherited from motorbridge-studio's `motorStudioOps.js`) and by Damiao firmware limits —
  not yet in a Python path, because the arm's debug commands flow through the stock
  motorbridge gateway. Add Python-side enforcement when the arm joins `ClankerStation`
  (`hand_only=False`).
- **Task-runner layer 3** is `stop_safe()` on fault (torque off via disconnect) plus firmware
  limits; there is no independent process supervising a *hung* (not crashed) runner. The
  operator + physical e-stop cover that gap until the runner routes through a supervised
  gateway.

## Consequences

+ A runaway policy is bounded by clamps, then watchdog, then firmware.
− Clamps add latency-negligible checks to `send_action`; worth it.
− Firmware limit configuration is part of bring-up, not optional.
