import { describe, expect, it } from 'vitest';
import {
  FAULT_KEYS,
  FAULT_TAXONOMY,
  faultDisplay,
  faultListDisplay,
  isKnownFault,
  normalizeFaultKey,
} from './faults';

describe('faults taxonomy', () => {
  it('covers the full ADR-0004 taxonomy with a label and severity token', () => {
    expect(FAULT_KEYS).toHaveLength(10);
    for (const key of FAULT_KEYS) {
      expect(FAULT_TAXONOMY[key]).toBeTruthy();
      expect(typeof FAULT_TAXONOMY[key].label).toBe('string');
      expect(['danger', 'warn']).toContain(FAULT_TAXONOMY[key].severity);
    }
  });

  it('normalizes casing and separators before lookup', () => {
    expect(normalizeFaultKey('Overtemp-Motor')).toBe('overtemp_motor');
    expect(normalizeFaultKey('  BUS_OFF ')).toBe('bus_off');
    expect(isKnownFault('Comms Timeout')).toBe(true);
  });

  it('maps a raw fault string to display data', () => {
    expect(faultDisplay('overcurrent')).toEqual({
      key: 'overcurrent',
      label: 'Overcurrent',
      severity: 'danger',
    });
  });

  it('still renders an unknown fault rather than dropping it', () => {
    const display = faultDisplay('some_new_fault');
    expect(display.key).toBe('some_new_fault');
    expect(display.severity).toBe('warn');
    expect(isKnownFault('some_new_fault')).toBe(false);
  });

  it('maps a list and tolerates non-array input', () => {
    expect(faultListDisplay(['undervoltage', 'bus_off']).map((f) => f.key)).toEqual([
      'undervoltage',
      'bus_off',
    ]);
    expect(faultListDisplay(null)).toEqual([]);
    expect(faultListDisplay(undefined)).toEqual([]);
  });
});
