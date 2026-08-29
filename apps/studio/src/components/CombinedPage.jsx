import React from 'react';
import robotsConfig from '../generated/robotsConfig.json';
import { useHandGateway } from '../hooks/useHandGateway';
import { useConnectionContext, useRobotArmContext } from '../hooks/useMotorStudioContext';
import { ArmUrdfViewer } from './ArmUrdfViewer';
import {
  applyLiveHandPositions,
  buildCombinedModel,
  clampArmJoint,
  zeroTargets,
} from '../lib/combinedModel';
import { buildPresetTargets, clampToLimit, radToDeg } from '../lib/leapHand';
import '../styles/combined.css';

const MODEL = buildCombinedModel(robotsConfig);
const JOG_STEP_RAD = Number(MODEL.hand.safety?.max_step_rad) || 0.05;

function JointSlider({ joint, value, onChange, disabled, live }) {
  return (
    <div className="combinedJointRow">
      <label className="combinedJointName" htmlFor={`j-${joint.name}`}>
        {joint.name}
      </label>
      <input
        id={`j-${joint.name}`}
        type="range"
        min={joint.limit.min}
        max={joint.limit.max}
        step={0.005}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
        aria-label={joint.name}
      />
      <span className="combinedJointValue">{radToDeg(value)}</span>
      {live !== undefined && live !== null && (
        <span className="combinedJointLive" title="live position from the hand gateway">
          live {radToDeg(live)}
        </span>
      )}
    </div>
  );
}

export function CombinedPage() {
  const hand = useHandGateway();
  // The arm is driven through the same shared studio session the Robot Arm page uses, not a
  // second connection: one process per bus (ADR-0002), and two owners of the Damiao serial
  // link would fight. That is why these are context reads rather than a new gateway hook.
  const arm = useRobotArmContext();
  const armConn = useConnectionContext();
  const [targets, setTargets] = React.useState(() => zeroTargets(MODEL));
  const [mirrorLive, setMirrorLive] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [note, setNote] = React.useState('');

  const armReady = Boolean(armConn?.connected) && !arm?.armBulkBusy;
  const handReady = hand.connected && !busy;
  const canPoseHand = handReady && MODEL.hand.calibrated;

  const run = React.useCallback(async (label, fn) => {
    setBusy(true);
    setNote('');
    try {
      await fn();
      setNote(`${label}: ok`);
    } catch (err) {
      setNote(`${label}: ${err?.message || err}`);
    } finally {
      setBusy(false);
    }
  }, []);

  const handHomeTargets = React.useCallback(() => {
    const out = {};
    for (const j of MODEL.hand.joints) out[j.name] = clampToLimit(j, 0);
    return out;
  }, []);

  // While the gateway is streaming and mirroring is on, the hand half of the model shows
  // measured positions instead of whatever the sliders last asked for.
  React.useEffect(() => {
    if (!mirrorLive || !hand.connected) return;
    setTargets((prev) => applyLiveHandPositions(prev, MODEL, hand.joints));
  }, [mirrorLive, hand.connected, hand.joints]);

  const setJoint = React.useCallback((name, value) => {
    setTargets((prev) => ({ ...prev, [name]: value }));
  }, []);

  const liveByName = React.useMemo(() => {
    const out = {};
    for (const live of hand.joints || []) {
      const j = MODEL.hand.byServoId.get(Number(live?.servo_id));
      if (j && Number.isFinite(Number(live?.pos))) out[j.name] = Number(live.pos);
    }
    return out;
  }, [hand.joints]);

  const applyPreset = React.useCallback((preset) => {
    setTargets((prev) => ({ ...prev, ...buildPresetTargets(MODEL.hand, preset) }));
  }, []);

  const sendHandPose = React.useCallback(() => {
    const payload = {};
    for (const j of MODEL.hand.joints) payload[j.name] = targets[j.name];
    return run('send hand pose', () => hand.ops.pos(payload));
  }, [hand.ops, targets, run]);

  const homeHand = React.useCallback(() => {
    const home = handHomeTargets();
    setTargets((prev) => ({ ...prev, ...home }));
    return run('home hand', () => hand.ops.pos(home));
  }, [hand.ops, handHomeTargets, run]);

  // The headline action: put the whole machine back to a known pose in one press. The arm
  // half goes through the shared session's reset-pose (all joints to 0 rad); the hand half
  // is a normal clamped `pos`. Either side is skipped if it is not ready, and the note says
  // which ones actually ran rather than implying both did.
  const resetAllPoses = React.useCallback(async () => {
    const ran = [];
    const skipped = [];
    if (armReady) ran.push('arm');
    else skipped.push('arm (not connected)');
    if (canPoseHand) ran.push('hand');
    else skipped.push(hand.connected ? 'hand (map unconfirmed)' : 'hand (gateway offline)');

    // No empty-run branch: the button is disabled unless at least one side is ready.
    await run(`reset all poses (${ran.join(' + ')})`, async () => {
      if (armReady) await arm.resetPoseRobotArm();
      if (canPoseHand) {
        const home = handHomeTargets();
        setTargets((prev) => ({ ...prev, ...home }));
        await hand.ops.pos(home);
      }
    });
    if (skipped.length) setNote((n) => `${n} — skipped ${skipped.join(', ')}`);
  }, [armReady, canPoseHand, arm, hand.ops, hand.connected, handHomeTargets, run]);

  const jog = React.useCallback(
    async (joint, delta) => {
      setBusy(true);
      setNote('');
      try {
        await hand.ops.jog(joint.servoId, delta);
      } catch (err) {
        setNote(String(err?.message || err));
      } finally {
        setBusy(false);
      }
    },
    [hand.ops],
  );

  return (
    <div className="combinedPage">
      <section className="card glass">
        <div className="row toolbar compactToolbar">
          <strong>Combined model</strong>
          <span className="muted">arm + adapter + LEAP hand</span>
          <button
            className="primary"
            onClick={resetAllPoses}
            disabled={busy || (!armReady && !canPoseHand)}
            title="Arm to 0 rad and hand to home, on the hardware"
          >
            Reset all poses
          </button>
          <span className="muted">|</span>
          <span className="muted">model only:</span>
          <button onClick={() => setTargets(zeroTargets(MODEL))}>Zero sliders</button>
          <button onClick={() => applyPreset('open')}>Open hand</button>
          <button onClick={() => applyPreset('curl')}>Curl hand</button>
        </div>

        {!MODEL.adapterCalibrated && (
          <p className="warnBanner" role="status">
            Adapter mount is <strong>provisional</strong>. The flange side is measured from the
            STL, but where the palm sits on the plate (and its clocking about the 6-hole ring)
            is an assumption — tune <code>adapter.hand_mount</code> in robots.yaml and re-run{' '}
            <code>clankers-build-urdf</code>. This view is a drawing, not a measurement.
          </p>
        )}
        {!MODEL.hand.calibrated && (
          <p className="warnBanner" role="status">
            <code>hand.calibrated</code> is false, so the servo&nbsp;↔&nbsp;joint map is still a
            hypothesis: the gateway refuses whole-hand <code>pos</code> commands. Per-joint jog
            works and is the bring-up path.
          </p>
        )}
      </section>

      <section className="card glass combinedViewerCard">
        <ArmUrdfViewer jointTargets={targets} profile="clankers" />
      </section>

      <section className="card glass">
        <div className="row toolbar compactToolbar">
          <h3>Arm</h3>
          <span className={armConn?.connected ? 'okChip' : 'muted'}>
            {armConn?.connected ? 'gateway connected' : 'gateway disconnected'}
          </span>
          <button onClick={() => run('arm enable all', arm.enableAllRobotArm)} disabled={!armReady || busy}>
            Enable all
          </button>
          <button onClick={() => run('arm disable all', arm.disableAllRobotArm)} disabled={!armReady || busy}>
            Disable all
          </button>
          <button onClick={() => run('arm reset pose', arm.resetPoseRobotArm)} disabled={!armReady || busy}>
            Reset pose
          </button>
        </div>
        <p className="muted">
          These buttons drive the real arm through the same session as the Robot Arm page —
          connect there. The sliders below pose the model only; per-joint live motion stays on
          that page, which owns the Damiao bus.
        </p>
        {MODEL.arm.map((j) => (
          <JointSlider
            key={j.name}
            joint={j}
            value={targets[j.name] ?? 0}
            onChange={(v) => setJoint(j.name, clampArmJoint(j, v))}
          />
        ))}
      </section>

      <section className="card glass">
        <div className="row toolbar compactToolbar">
          <h3>Hand</h3>
          <span className={hand.connected ? 'okChip' : 'muted'}>
            {hand.connected ? 'gateway connected' : 'gateway disconnected'}
          </span>
          <label>
            <input
              type="checkbox"
              checked={mirrorLive}
              onChange={(e) => setMirrorLive(e.target.checked)}
            />{' '}
            mirror live positions
          </label>
          <button
            onClick={() => run('hand enable all', () => hand.ops.enable())}
            disabled={!handReady}
          >
            Enable all
          </button>
          <button
            onClick={() => run('hand disable all', () => hand.ops.disable())}
            disabled={!handReady}
          >
            Disable all
          </button>
          <button
            onClick={sendHandPose}
            disabled={!canPoseHand}
            title={
              MODEL.hand.calibrated
                ? 'Send every hand joint to its slider value'
                : 'Blocked until hand.calibrated is true'
            }
          >
            Send hand pose
          </button>
          <button onClick={homeHand} disabled={!canPoseHand} title="All hand joints to 0 rad">
            Home hand
          </button>
        </div>
        {note && <p className="warnBanner">{note}</p>}

        {MODEL.hand.fingers.map((finger) => (
          <div key={finger} className="combinedFingerGroup">
            <h4>{finger}</h4>
            {MODEL.hand.byFinger[finger].map((j) => (
              <div key={j.name} className="combinedJointWithJog">
                <JointSlider
                  joint={j}
                  value={targets[j.name] ?? 0}
                  live={liveByName[j.name]}
                  onChange={(v) => setJoint(j.name, clampToLimit(j, v))}
                />
                <button
                  disabled={!hand.connected || busy}
                  onClick={() => jog(j, -JOG_STEP_RAD)}
                  aria-label={`jog ${j.name} negative`}
                >
                  −
                </button>
                <button
                  disabled={!hand.connected || busy}
                  onClick={() => jog(j, JOG_STEP_RAD)}
                  aria-label={`jog ${j.name} positive`}
                >
                  +
                </button>
              </div>
            ))}
          </div>
        ))}
      </section>
    </div>
  );
}
