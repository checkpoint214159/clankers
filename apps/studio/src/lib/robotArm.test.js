import { describe, expect, it } from 'vitest';
import {
  REBOT_ARM_JOINT_LIMITS,
  REBOT_ARM_ROBSTRIDE_JOINT_LIMITS,
  REBOT_ARM_ROBSTRIDE_DEFAULT_TEMPLATE,
  jointLimitsForProfile,
  normalizeRobotArmModel,
} from './robotArm';

describe('robotArm config', () => {
  it('normalizes aliases', () => {
    expect(normalizeRobotArmModel('reBot-Arm Lite')).toBe('rebot-arm-robstride');
    expect(normalizeRobotArmModel('7dof')).toBe('rebot-arm-damiao');
  });

  it('contains joint limits for all joints', () => {
    for (let joint = 1; joint <= 7; joint += 1) {
      expect(REBOT_ARM_JOINT_LIMITS[joint]).toBeTruthy();
      expect(REBOT_ARM_JOINT_LIMITS[joint].min).toBeLessThanOrEqual(
        REBOT_ARM_JOINT_LIMITS[joint].max
      );
    }
  });

  it('returns model-specific limits map', () => {
    expect(jointLimitsForProfile('rebot-arm-damiao')).toBe(REBOT_ARM_JOINT_LIMITS);
    expect(jointLimitsForProfile('rebot-arm-robstride')).toBe(REBOT_ARM_ROBSTRIDE_JOINT_LIMITS);
    expect(jointLimitsForProfile('reBot-Arm Lite')).toBe(REBOT_ARM_ROBSTRIDE_JOINT_LIMITS);
  });

  it('contains RobStride default template values for all joints', () => {
    expect(Object.keys(REBOT_ARM_ROBSTRIDE_DEFAULT_TEMPLATE)).toHaveLength(7);
    expect(REBOT_ARM_ROBSTRIDE_DEFAULT_TEMPLATE[1]).toEqual({
      locKp: '13',
      spdKp: '12.0',
      accRad: '12.0',
      velMax: '1',
    });
    expect(REBOT_ARM_ROBSTRIDE_DEFAULT_TEMPLATE[2]).toMatchObject({
      locKp: '17',
      spdKp: '13.5',
      accRad: '1.5',
      velMax: '0.4',
    });
    expect(REBOT_ARM_ROBSTRIDE_DEFAULT_TEMPLATE[7]).toMatchObject({
      locKp: '10',
      spdKp: '5.0',
      accRad: '20.0',
      velMax: '1',
    });
  });
});
