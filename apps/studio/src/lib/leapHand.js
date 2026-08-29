// LEAP hand topology, built from the generated robots.yaml export (never hardcode topology —
// see CLAUDE.md). Parallel to lib/robotArm.js, but the hand's joint map is data-driven because
// it is PROVISIONAL until bring-up confirms servo IDs/signs (see hand.calibrated in the config).
import { faultDisplay } from './faults';

// Anatomical order. (It used to read index/thumb/middle/ring, which mirrored the old
// mislabelling of servos 4-15 rather than any display preference.)
export const FINGER_ORDER = ['index', 'middle', 'ring', 'thumb'];

export const TEMP_WARN_C = 55;
export const TEMP_DANGER_C = 65;

function toFiniteNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** Build the flat 16-joint list from config.hand.joints, one entry per canonical index. */
export function buildHandJoints(handConfig) {
  const rawJoints = Array.isArray(handConfig?.joints) ? handConfig.joints : [];
  return rawJoints
    .map((j) => ({
      canonical: Number(j.canonical),
      servoId: Number(j.servo_id),
      name: String(j.name),
      finger: String(j.finger),
      limit: {
        min: Number(j.limit?.min ?? -Math.PI),
        max: Number(j.limit?.max ?? Math.PI),
      },
    }))
    .sort((a, b) => a.canonical - b.canonical);
}

/** Group joints by finger, each finger's joints sorted by canonical index (MCP side -> DIP). */
export function groupJointsByFinger(joints) {
  const grouped = {};
  for (const finger of FINGER_ORDER) grouped[finger] = [];
  for (const j of joints) {
    if (!grouped[j.finger]) grouped[j.finger] = [];
    grouped[j.finger].push(j);
  }
  for (const key of Object.keys(grouped)) {
    grouped[key] = [...grouped[key]].sort((a, b) => a.canonical - b.canonical);
  }
  return grouped;
}

/** Build the full hand model consumed by useHandGateway/LeapHandPage from robotsConfig.json. */
export function buildHandModel(robotsConfig) {
  const hand = robotsConfig?.hand;
  if (!hand) throw new Error('robotsConfig.hand is missing — regenerate with clankers-export-config');

  const joints = buildHandJoints(hand);
  const byFinger = groupJointsByFinger(joints);
  const fingers = FINGER_ORDER.filter((f) => (byFinger[f] || []).length > 0);
  for (const f of Object.keys(byFinger)) {
    if (!fingers.includes(f) && byFinger[f].length > 0) fingers.push(f);
  }

  return {
    joints,
    fingers,
    byFinger,
    byServoId: new Map(joints.map((j) => [j.servoId, j])),
    byName: new Map(joints.map((j) => [j.name, j])),
    byCanonical: new Map(joints.map((j) => [j.canonical, j])),
    safety: hand.safety || {},
    poll: hand.poll || {},
    calibrated: Boolean(hand.calibrated),
    motorModel: hand.motor_model ?? null,
    currentLimitMa: toFiniteNumber(hand.current_limit_ma),
    temperatureLimitC: toFiniteNumber(hand.temperature_limit_c),
  };
}

export function jointLimit(joint) {
  return joint?.limit || { min: -Math.PI, max: Math.PI };
}

export function clampToLimit(joint, valueRad) {
  const { min, max } = jointLimit(joint);
  const v = toFiniteNumber(valueRad) ?? 0;
  return Math.min(max, Math.max(min, v));
}

export function radToDeg(rad) {
  const v = toFiniteNumber(rad);
  return v === null ? null : (v * 180) / Math.PI;
}

export function degToRad(deg) {
  const v = toFiniteNumber(deg);
  return v === null ? null : (v * Math.PI) / 180;
}

/** Display formatting: {deg, rad} strings for a joint position, or the placeholder for a
 * missing/offline reading — callers should never have to guess how "no data" renders. */
export function formatJointPosition(rad) {
  const value = toFiniteNumber(rad);
  if (value === null) return { deg: '—', rad: '—' };
  return {
    deg: `${radToDeg(value).toFixed(1)}°`,
    rad: `${value.toFixed(3)} rad`,
  };
}

export function formatVelocity(radPerS) {
  const value = toFiniteNumber(radPerS);
  return value === null ? '—' : `${value.toFixed(2)} rad/s`;
}

export function formatCurrentMa(ma) {
  const value = toFiniteNumber(ma);
  return value === null ? '—' : `${value.toFixed(0)} mA`;
}

export function formatVoltage(v) {
  const value = toFiniteNumber(v);
  return value === null ? '—' : `${value.toFixed(2)} V`;
}

export function formatTemperatureC(c) {
  const value = toFiniteNumber(c);
  return value === null ? '—' : `${value.toFixed(1)}°C`;
}

/** 'ok' | 'warn' | 'danger' | 'unknown' — thresholds match the LeapHandPage spec (>55 amber,
 * >65 red); the firmware shutdown default (70C, ADR-0004) stays a hard backstop above both. */
export function temperatureSeverity(c) {
  const value = toFiniteNumber(c);
  if (value === null) return 'unknown';
  if (value > TEMP_DANGER_C) return 'danger';
  if (value > TEMP_WARN_C) return 'warn';
  return 'ok';
}

/** Fault chip display data for one joint's raw fault-string list. */
export function jointFaultChips(rawFaults) {
  if (!Array.isArray(rawFaults)) return [];
  return rawFaults.map(faultDisplay);
}

export const HAND_PRESETS = ['open', 'curl'];

// Joint-name suffixes that move a finger's curl axis (MCP flex, PIP, DIP, or the ring's base
// rotation); "_side" joints are abduction and stay neutral in these synthetic presets.
const CURL_AXIS_SUFFIXES = ['_flex', '_pip', '_dip', '_base_rot'];

function isCurlAxis(name) {
  return CURL_AXIS_SUFFIXES.some((suffix) => String(name).endsWith(suffix));
}

/** A synthetic open/curl pose derived from joint limits and naming, NOT a calibrated pose —
 * curl axes go to their max (curl) or min (open) limit, side axes stay at 0. Callers must
 * gate this behind `model.calibrated`, mirroring the hand gateway's own guard (the joint map
 * is provisional until bring-up confirms servo IDs/signs). */
export function buildPresetTargets(model, preset) {
  const out = {};
  for (const joint of model.joints) {
    if (isCurlAxis(joint.name)) {
      out[joint.name] = preset === 'curl' ? joint.limit.max : joint.limit.min;
    } else {
      out[joint.name] = clampToLimit(joint, 0);
    }
  }
  return out;
}

/** Duplicate servo IDs found in a scan hits list — surfaced as a warning banner. */
export function findDuplicateServoIds(hits) {
  const seen = new Set();
  const dupes = new Set();
  for (const h of Array.isArray(hits) ? hits : []) {
    const id = Number(h?.servo_id);
    if (!Number.isFinite(id)) continue;
    if (seen.has(id)) dupes.add(id);
    seen.add(id);
  }
  return [...dupes].sort((a, b) => a - b);
}
