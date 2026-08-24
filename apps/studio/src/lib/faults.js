// Normalized fault taxonomy (ADR-0004), mirrored from clankers.safety.faults.Fault.
// Faults require explicit operator acknowledgment (reboot op) — this module only formats
// them for display, it never clears or acknowledges anything.

export const FAULT_KEYS = [
  'undervoltage',
  'overvoltage',
  'overcurrent',
  'overtemp_mosfet',
  'overtemp_motor',
  'overload',
  'comms_timeout',
  'encoder_fault',
  'electrical_shock',
  'bus_off',
];

// severity is a token, not a literal color, so components pick the palette (see
// .faultChip--danger / .faultChip--warn in styles/leap-hand.css).
export const FAULT_TAXONOMY = Object.freeze({
  undervoltage: { label: 'Undervoltage', severity: 'danger' },
  overvoltage: { label: 'Overvoltage', severity: 'danger' },
  overcurrent: { label: 'Overcurrent', severity: 'danger' },
  overtemp_mosfet: { label: 'MOSFET Overtemp', severity: 'warn' },
  overtemp_motor: { label: 'Motor Overtemp', severity: 'warn' },
  overload: { label: 'Overload', severity: 'danger' },
  comms_timeout: { label: 'Comms Timeout', severity: 'warn' },
  encoder_fault: { label: 'Encoder Fault', severity: 'danger' },
  electrical_shock: { label: 'Electrical Shock', severity: 'danger' },
  bus_off: { label: 'Bus Off', severity: 'danger' },
});

export function normalizeFaultKey(raw) {
  return String(raw ?? '')
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, '_');
}

/** Map a raw fault string (any casing/spacing) to {key, label, severity}. Unknown faults
 * still render — with the normalized key as the label and 'warn' severity — rather than
 * being dropped, since a display gap here would hide a real safety condition. */
export function faultDisplay(raw) {
  const key = normalizeFaultKey(raw);
  const known = FAULT_TAXONOMY[key];
  if (known) return { key, ...known };
  return { key: key || 'unknown', label: key || 'Unknown Fault', severity: 'warn' };
}

export function faultListDisplay(list) {
  if (!Array.isArray(list)) return [];
  return list.map(faultDisplay);
}

export function isKnownFault(raw) {
  return normalizeFaultKey(raw) in FAULT_TAXONOMY;
}
