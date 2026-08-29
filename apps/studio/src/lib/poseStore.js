// Save/load poses for the whole 22-joint system (6 arm + 16 hand).
//
// A pose is stored as { name, savedAt, joints: { jointName: radians } } — named joints, not a
// positional array, so a pose survives someone reordering robots.yaml, and a pose saved before
// a joint existed still loads (the missing joint just holds its current value).
//
// Storage is localStorage, which is per-browser and can throw outright (private windows,
// blocked site data), so every access is guarded and failure degrades to "no saved poses"
// rather than taking the page down.

import { clampToLimit } from './leapHand';
import { clampArmJoint } from './combinedModel';

export const POSE_STORAGE_KEY = 'clankers.poses.v1';

function isFiniteNumber(v) {
  return Number.isFinite(Number(v));
}

/** Defensive read: anything that is not a well-formed pose list becomes an empty list. */
export function readPoses(storage) {
  try {
    const raw = storage?.getItem?.(POSE_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (p) => p && typeof p.name === 'string' && p.name.length > 0 && p.joints && typeof p.joints === 'object',
    );
  } catch {
    return [];
  }
}

export function writePoses(storage, poses) {
  try {
    storage?.setItem?.(POSE_STORAGE_KEY, JSON.stringify(poses));
    return true;
  } catch {
    return false;
  }
}

/** Capture the current targets for every joint the model knows about. */
export function poseFromTargets(name, targets, model) {
  const joints = {};
  for (const j of model.arm) {
    if (isFiniteNumber(targets?.[j.name])) joints[j.name] = Number(targets[j.name]);
  }
  for (const j of model.hand.joints) {
    if (isFiniteNumber(targets?.[j.name])) joints[j.name] = Number(targets[j.name]);
  }
  return { name: String(name).trim(), savedAt: new Date().toISOString(), joints };
}

/**
 * Turn a stored pose back into targets, clamped into today's limits.
 *
 * Clamping on load matters: a pose saved before a limit was tightened would otherwise
 * command past it, and the joint names are the only thing tying the two together.
 */
export function poseToTargets(pose, model, current = {}) {
  const out = { ...current };
  for (const j of model.arm) {
    const v = pose?.joints?.[j.name];
    if (isFiniteNumber(v)) out[j.name] = clampArmJoint(j, Number(v));
  }
  for (const j of model.hand.joints) {
    const v = pose?.joints?.[j.name];
    if (isFiniteNumber(v)) out[j.name] = clampToLimit(j, Number(v));
  }
  return out;
}

/** Replace by name (case-insensitive) so re-saving a pose updates it instead of duplicating. */
export function upsertPose(poses, pose) {
  const key = pose.name.toLowerCase();
  const next = poses.filter((p) => p.name.toLowerCase() !== key);
  next.push(pose);
  next.sort((a, b) => a.name.localeCompare(b.name));
  return next;
}

export function removePose(poses, name) {
  const key = String(name).toLowerCase();
  return poses.filter((p) => p.name.toLowerCase() !== key);
}

/** Which joints in a stored pose no longer exist — shown so a stale pose is not silently partial. */
export function unknownJoints(pose, model) {
  const known = new Set([...model.arm.map((j) => j.name), ...model.hand.joints.map((j) => j.name)]);
  return Object.keys(pose?.joints || {}).filter((n) => !known.has(n));
}
