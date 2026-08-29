import { describe, expect, it } from 'vitest';
import robotsConfig from '../generated/robotsConfig.json';
import {
  FINGER_ORDER,
  buildHandJoints,
  buildHandModel,
  buildPresetTargets,
  clampToLimit,
  degToRad,
  findDuplicateServoIds,
  formatCurrentMa,
  formatJointPosition,
  formatTemperatureC,
  formatVelocity,
  formatVoltage,
  groupJointsByFinger,
  jointFaultChips,
  jointLimit,
  radToDeg,
  temperatureSeverity,
} from './leapHand';

describe('buildHandModel', () => {
  const model = buildHandModel(robotsConfig);

  it('builds all 16 canonical joints', () => {
    expect(model.joints).toHaveLength(16);
    expect(model.joints.map((j) => j.canonical)).toEqual([...Array(16).keys()]);
  });

  it('groups into 4 fingers of 4 joints each, in canonical order', () => {
    expect(model.fingers).toEqual(FINGER_ORDER);
    for (const finger of FINGER_ORDER) {
      expect(model.byFinger[finger]).toHaveLength(4);
      const canonicals = model.byFinger[finger].map((j) => j.canonical);
      expect(canonicals).toEqual([...canonicals].sort((a, b) => a - b));
    }
    // Confirmed on hardware 2026-08-29 by jogging each group and watching which finger
    // moved. These four lines used to read index/thumb/middle/ring in this order, which was
    // the mislabelling, not the wiring.
    expect(model.byFinger.index.map((j) => j.canonical)).toEqual([0, 1, 2, 3]);
    expect(model.byFinger.middle.map((j) => j.canonical)).toEqual([4, 5, 6, 7]);
    expect(model.byFinger.ring.map((j) => j.canonical)).toEqual([8, 9, 10, 11]);
    expect(model.byFinger.thumb.map((j) => j.canonical)).toEqual([12, 13, 14, 15]);
  });

  it('indexes joints by servo id, name, and canonical index', () => {
    expect(model.byServoId.get(0).name).toBe('index_mcp_side');
    expect(model.byName.get('middle_dip').servoId).toBe(7);
    expect(model.byCanonical.get(15).name).toBe('thumb_dip');
  });

  it('surfaces the calibrated flag and safety config verbatim', () => {
    // "verbatim" is the contract: mirror robots.yaml rather than assert today's value, so
    // confirming (or un-confirming) the hand does not need a test edit.
    expect(model.calibrated).toBe(robotsConfig.hand.calibrated);
    expect(model.safety.max_step_rad).toBeCloseTo(0.15);
    expect(model.safety.watchdog_multiplier).toBe(4);
    expect(model.currentLimitMa).toBe(300);
    expect(model.temperatureLimitC).toBe(70);
  });

  it('throws a clear error when hand config is missing', () => {
    expect(() => buildHandModel({})).toThrow(/hand/i);
  });
});

describe('buildHandJoints / groupJointsByFinger', () => {
  it('sorts by canonical index even when the source is out of order', () => {
    const joints = buildHandJoints({
      joints: [
        { canonical: 1, servo_id: 1, name: 'b', finger: 'index', limit: { min: -1, max: 1 } },
        { canonical: 0, servo_id: 0, name: 'a', finger: 'index', limit: { min: -1, max: 1 } },
      ],
    });
    expect(joints.map((j) => j.name)).toEqual(['a', 'b']);
  });

  it('tolerates an unknown finger key by grouping it under its own key', () => {
    const joints = buildHandJoints({
      joints: [{ canonical: 0, servo_id: 0, name: 'x', finger: 'pinky', limit: { min: -1, max: 1 } }],
    });
    const grouped = groupJointsByFinger(joints);
    expect(grouped.pinky).toHaveLength(1);
    expect(grouped.index).toHaveLength(0);
  });
});

describe('limit clamp and unit conversions', () => {
  const joint = { limit: { min: -0.5, max: 1.0 } };

  it('clamps into [min, max]', () => {
    expect(clampToLimit(joint, 5)).toBe(1.0);
    expect(clampToLimit(joint, -5)).toBe(-0.5);
    expect(clampToLimit(joint, 0.2)).toBeCloseTo(0.2);
  });

  it('falls back to a wide default limit when missing', () => {
    const limit = jointLimit(undefined);
    expect(limit.min).toBeLessThan(0);
    expect(limit.max).toBeGreaterThan(0);
  });

  it('converts rad <-> deg', () => {
    expect(radToDeg(Math.PI)).toBeCloseTo(180);
    expect(degToRad(180)).toBeCloseTo(Math.PI);
    expect(radToDeg('nope')).toBeNull();
  });
});

describe('display formatting', () => {
  it('formats a joint position as deg + rad', () => {
    expect(formatJointPosition(Math.PI / 2)).toEqual({ deg: '90.0°', rad: '1.571 rad' });
  });

  it('uses an em dash placeholder for missing telemetry', () => {
    expect(formatJointPosition(null)).toEqual({ deg: '—', rad: '—' });
    expect(formatVelocity(undefined)).toBe('—');
    expect(formatCurrentMa(Number.NaN)).toBe('—');
    expect(formatVoltage(null)).toBe('—');
    expect(formatTemperatureC(undefined)).toBe('—');
  });

  it('formats current/voltage/temperature with units', () => {
    expect(formatCurrentMa(123.4)).toBe('123 mA');
    expect(formatVoltage(7.412)).toBe('7.41 V');
    expect(formatTemperatureC(42)).toBe('42.0°C');
  });
});

describe('temperatureSeverity', () => {
  it('is ok at or below the warn threshold', () => {
    expect(temperatureSeverity(55)).toBe('ok');
    expect(temperatureSeverity(30)).toBe('ok');
  });

  it('is warn above 55 and danger above 65', () => {
    expect(temperatureSeverity(55.1)).toBe('warn');
    expect(temperatureSeverity(65)).toBe('warn');
    expect(temperatureSeverity(65.1)).toBe('danger');
  });

  it('is unknown for non-finite input', () => {
    expect(temperatureSeverity(undefined)).toBe('unknown');
  });
});

describe('jointFaultChips', () => {
  it('maps raw fault strings to display chips', () => {
    const chips = jointFaultChips(['overtemp_motor', 'bus_off']);
    expect(chips).toEqual([
      { key: 'overtemp_motor', label: 'Motor Overtemp', severity: 'warn' },
      { key: 'bus_off', label: 'Bus Off', severity: 'danger' },
    ]);
  });

  it('returns an empty list for non-array input', () => {
    expect(jointFaultChips(undefined)).toEqual([]);
  });
});

describe('buildPresetTargets', () => {
  const model = buildHandModel(robotsConfig);

  it('drives curl axes to their max limit for the curl preset', () => {
    const targets = buildPresetTargets(model, 'curl');
    const flexJoint = model.byName.get('index_mcp_flex');
    expect(targets.index_mcp_flex).toBeCloseTo(flexJoint.limit.max);
    const dipJoint = model.byName.get('index_dip');
    expect(targets.index_dip).toBeCloseTo(dipJoint.limit.max);
  });

  it('drives curl axes to their min limit for the open preset', () => {
    const targets = buildPresetTargets(model, 'open');
    const flexJoint = model.byName.get('index_mcp_flex');
    expect(targets.index_mcp_flex).toBeCloseTo(flexJoint.limit.min);
  });

  it('keeps side/abduction axes neutral in both presets', () => {
    const open = buildPresetTargets(model, 'open');
    const curl = buildPresetTargets(model, 'curl');
    expect(open.index_mcp_side).toBeCloseTo(0);
    expect(curl.index_mcp_side).toBeCloseTo(0);
  });

  it('produces a target for every joint in the model', () => {
    const targets = buildPresetTargets(model, 'open');
    expect(Object.keys(targets)).toHaveLength(16);
  });
});

describe('findDuplicateServoIds', () => {
  it('finds duplicate servo ids across scan hits', () => {
    const hits = [{ servo_id: 3 }, { servo_id: 4 }, { servo_id: 3 }, { servo_id: 5 }, { servo_id: 4 }];
    expect(findDuplicateServoIds(hits)).toEqual([3, 4]);
  });

  it('returns an empty list when there are no duplicates', () => {
    expect(findDuplicateServoIds([{ servo_id: 1 }, { servo_id: 2 }])).toEqual([]);
  });
});
