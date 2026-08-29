import { beforeEach, describe, expect, it } from 'vitest';
import robotsConfig from '../generated/robotsConfig.json';
import { buildCombinedModel } from './combinedModel';
import {
  POSE_STORAGE_KEY,
  poseFromTargets,
  poseToTargets,
  readPoses,
  removePose,
  unknownJoints,
  upsertPose,
  writePoses,
} from './poseStore';

const MODEL = buildCombinedModel(robotsConfig);

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return {
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = String(v);
    },
    _data: data,
  };
}

function throwingStorage() {
  return {
    getItem: () => {
      throw new Error('SecurityError: blocked');
    },
    setItem: () => {
      throw new Error('QuotaExceededError');
    },
  };
}

let targets;
beforeEach(() => {
  targets = {};
  for (const j of MODEL.arm) targets[j.name] = 0;
  for (const j of MODEL.hand.joints) targets[j.name] = 0;
});

describe('poseStore', () => {
  it('captures every arm and hand joint', () => {
    const pose = poseFromTargets('grip', targets, MODEL);
    expect(Object.keys(pose.joints)).toHaveLength(22);
    expect(pose.name).toBe('grip');
    expect(pose.savedAt).toBeTruthy();
  });

  it('round-trips through storage', () => {
    const storage = memoryStorage();
    const pose = poseFromTargets('grip', { ...targets, joint1: 0.4 }, MODEL);
    writePoses(storage, [pose]);
    const [back] = readPoses(storage);
    expect(back.joints.joint1).toBeCloseTo(0.4);
  });

  it('degrades to no saved poses when storage is unreadable', () => {
    // Private windows and blocked site data throw on access rather than returning null.
    expect(readPoses(throwingStorage())).toEqual([]);
    expect(writePoses(throwingStorage(), [])).toBe(false);
  });

  it('ignores corrupt or foreign storage contents', () => {
    expect(readPoses(memoryStorage({ [POSE_STORAGE_KEY]: 'not json' }))).toEqual([]);
    expect(readPoses(memoryStorage({ [POSE_STORAGE_KEY]: '{"a":1}' }))).toEqual([]);
    const partial = JSON.stringify([{ name: 'ok', joints: {} }, { name: '' }, { joints: {} }]);
    expect(readPoses(memoryStorage({ [POSE_STORAGE_KEY]: partial }))).toHaveLength(1);
  });

  it('clamps a loaded pose into current limits', () => {
    // A pose saved before a limit was tightened must not command past it.
    const j1 = MODEL.arm[0];
    const hand0 = MODEL.hand.joints[0];
    const pose = { name: 'wild', joints: { [j1.name]: 99, [hand0.name]: -99 } };
    const out = poseToTargets(pose, MODEL, targets);
    expect(out[j1.name]).toBeCloseTo(j1.limit.max);
    expect(out[hand0.name]).toBeCloseTo(hand0.limit.min);
  });

  it('leaves joints the pose does not mention at their current value', () => {
    const pose = { name: 'partial', joints: { joint1: 0.2 } };
    const out = poseToTargets(pose, MODEL, { ...targets, joint2: -0.5 });
    expect(out.joint1).toBeCloseTo(0.2);
    expect(out.joint2).toBeCloseTo(-0.5);
  });

  it('ignores joints that no longer exist rather than inventing them', () => {
    const pose = { name: 'old', joints: { ring_base_rot: 0.3, joint1: 0.1 } };
    const out = poseToTargets(pose, MODEL, targets);
    expect(out.ring_base_rot).toBeUndefined();
    expect(out.joint1).toBeCloseTo(0.1);
    expect(unknownJoints(pose, MODEL)).toEqual(['ring_base_rot']);
  });

  it('re-saving a name updates in place instead of duplicating', () => {
    let poses = upsertPose([], poseFromTargets('home', targets, MODEL));
    poses = upsertPose(poses, poseFromTargets('HOME', { ...targets, joint1: 1 }, MODEL));
    expect(poses).toHaveLength(1);
    expect(poses[0].joints.joint1).toBeCloseTo(1);
  });

  it('keeps poses sorted and removes by name case-insensitively', () => {
    let poses = upsertPose([], poseFromTargets('zeta', targets, MODEL));
    poses = upsertPose(poses, poseFromTargets('alpha', targets, MODEL));
    expect(poses.map((p) => p.name)).toEqual(['alpha', 'zeta']);
    expect(removePose(poses, 'ALPHA').map((p) => p.name)).toEqual(['zeta']);
  });
});
