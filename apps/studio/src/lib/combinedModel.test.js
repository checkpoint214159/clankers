import { describe, expect, it } from 'vitest';
import robotsConfig from '../generated/robotsConfig.json';
import {
  applyLiveHandPositions,
  buildArmJoints,
  buildCombinedModel,
  clampArmJoint,
  zeroTargets,
} from './combinedModel';

describe('combinedModel', () => {
  it('exposes the six revolute arm joints and excludes the gripper', () => {
    const arm = buildArmJoints(robotsConfig);
    expect(arm).toHaveLength(6);
    expect(arm.map((j) => j.name)).toEqual([
      'joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6',
    ]);
    expect(arm.some((j) => j.name === 'gripper')).toBe(false);
  });

  it('builds one tree covering arm and all sixteen hand joints', () => {
    const model = buildCombinedModel(robotsConfig);
    expect(model.arm).toHaveLength(6);
    expect(model.hand.joints).toHaveLength(16);
  });

  it('reports the adapter mount as uncalibrated while robots.yaml says so', () => {
    // The mount transform is a measured guess; the page must be able to say so.
    const model = buildCombinedModel(robotsConfig);
    expect(model.adapterCalibrated).toBe(robotsConfig.adapter.calibrated);
  });

  it('throws a pointed error when the generated config is stale', () => {
    expect(() => buildArmJoints({})).toThrow(/clankers-export-config/);
  });

  it('clamps arm targets into their robots.yaml limits', () => {
    const [j1] = buildArmJoints(robotsConfig);
    expect(clampArmJoint(j1, 99)).toBe(j1.limit.max);
    expect(clampArmJoint(j1, -99)).toBe(j1.limit.min);
    expect(clampArmJoint(j1, NaN)).toBe(0);
  });

  it('zeroes every joint in the combined tree', () => {
    const model = buildCombinedModel(robotsConfig);
    const t = zeroTargets(model);
    expect(Object.keys(t)).toHaveLength(22); // 6 arm + 16 hand
    for (const v of Object.values(t)) expect(Number.isFinite(v)).toBe(true);
  });

  it('mirrors live servo positions onto the matching named joints', () => {
    const model = buildCombinedModel(robotsConfig);
    const base = zeroTargets(model);
    const next = applyLiveHandPositions(base, model, [{ servo_id: 0, pos: 0.5 }]);
    const j0 = model.hand.byServoId.get(0);
    expect(next[j0.name]).toBeCloseTo(0.5);
  });

  it('leaves targets untouched when the gateway has sent nothing', () => {
    const model = buildCombinedModel(robotsConfig);
    const base = zeroTargets(model);
    expect(applyLiveHandPositions(base, model, [])).toBe(base);
  });

  it('ignores live readings for servos that are not in robots.yaml', () => {
    const model = buildCombinedModel(robotsConfig);
    const base = zeroTargets(model);
    const next = applyLiveHandPositions(base, model, [{ servo_id: 99, pos: 1 }]);
    expect(next).toEqual(base);
  });
});
