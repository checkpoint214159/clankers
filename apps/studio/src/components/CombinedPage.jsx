import React from 'react';
import robotsConfig from '../generated/robotsConfig.json';
import { useHandGatewayContext } from '../hooks/useHandGatewayContext';
import { useConnectionContext, useRobotArmContext } from '../hooks/useMotorStudioContext';
import { ArmUrdfViewer } from './ArmUrdfViewer';
import { CollapsibleSection } from './CollapsibleSection';
import { GatewayConnections } from './GatewayConnections';
import { PoseLibrary } from './PoseLibrary';
import { RobotArmPage } from './RobotArmPage';
import { LeapHandPage } from './LeapHandPage';
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
  const hand = useHandGatewayContext();
  // The arm runs through the shared studio session rather than its own connection: one
  // process per bus (ADR-0002), and two owners of the Damiao link would fight.
  const arm = useRobotArmContext();
  const armConn = useConnectionContext();

  const [targets, setTargets] = React.useState(() => zeroTargets(MODEL));
  const [mirrorLive, setMirrorLive] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [note, setNote] = React.useState('');
  const [armOpen, setArmOpen] = React.useState(false);
  const [handOpen, setHandOpen] = React.useState(false);

  const armReady = Boolean(armConn?.connected) && !arm?.armBulkBusy;
  const handReady = hand.connected && !busy;
  const canPoseHand = handReady && MODEL.hand.calibrated;

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

  const applyPreset = React.useCallback((preset) => {
    setTargets((prev) => ({ ...prev, ...buildPresetTargets(MODEL.hand, preset) }));
  }, []);

  const handTargetsFrom = React.useCallback((source) => {
    const out = {};
    for (const j of MODEL.hand.joints) out[j.name] = clampToLimit(j, source?.[j.name] ?? 0);
    return out;
  }, []);

  /**
   * Send a full-system target map to the hardware.
   *
   * The arm and the hand are commanded through completely different stacks, so this reports
   * which halves actually went out instead of implying both did.
   */
  const sendToRobot = React.useCallback(
    async (next, label) => {
      const ran = [];
      const skipped = [];
      if (armReady) ran.push('arm');
      else skipped.push('arm (gateway offline)');
      if (canPoseHand) ran.push('hand');
      else skipped.push(hand.connected ? 'hand (map unconfirmed)' : 'hand (gateway offline)');

      await run(`${label} (${ran.join(' + ') || 'nothing'})`, async () => {
        if (armReady) await arm.resetPoseRobotArm();
        if (canPoseHand) await hand.ops.pos(handTargetsFrom(next));
      });
      if (skipped.length) setNote((n) => `${n} — skipped ${skipped.join(', ')}`);
    },
    [armReady, canPoseHand, arm, hand.ops, hand.connected, handTargetsFrom, run],
  );

  // Zero all = go to the zero pose on the hardware. It does not touch any encoder zero
  // reference; that is `Set mechanical zero`, inside the arm section behind its confirm.
  const zeroAll = React.useCallback(() => {
    const next = zeroTargets(MODEL);
    setTargets(next);
    return sendToRobot(next, 'zero all');
  }, [sendToRobot]);

  const sendHandPose = React.useCallback(
    () => run('send hand pose', () => hand.ops.pos(handTargetsFrom(targets))),
    [hand.ops, handTargetsFrom, targets, run],
  );

  const jog = React.useCallback(
    (joint, delta) => run(`jog ${joint.name}`, () => hand.ops.jog(joint.servoId, delta)),
    [hand.ops, run],
  );

  const canSendPose = armReady || canPoseHand;

  return (
    <div className="combinedPage">
      <GatewayConnections />

      <section className="card glass">
        <div className="row toolbar compactToolbar">
          <strong>Whole system</strong>
          <span className="muted">6 arm + 16 hand joints</span>
          {/* Named for what it does, not "Zero all": the arm section has a *mechanical*
              zero that rewrites the encoder reference, and two buttons a letter apart doing
              opposite things is how a reference gets destroyed by accident. */}
          <button className="primary strong" onClick={zeroAll} disabled={busy || !canSendPose}>
            Go to zero pose
          </button>
          <span className="muted">|</span>
          <span className="muted">sliders only:</span>
          <button onClick={() => setTargets(zeroTargets(MODEL))}>Zero sliders</button>
          <button onClick={() => applyPreset('open')}>Open hand</button>
          <button onClick={() => applyPreset('curl')}>Curl hand</button>
        </div>

        {!MODEL.adapterCalibrated && (
          <p className="warnBanner" role="status">
            Adapter mount is <strong>provisional</strong>. The flange side is measured from the
            STL, but where the palm sits on the plate is an assumption — tune{' '}
            <code>adapter.hand_mount</code> in robots.yaml and re-run{' '}
            <code>clankers-build-urdf</code>.
          </p>
        )}
        {note && <p className="warnBanner">{note}</p>}
      </section>

      <section className="card glass combinedViewerCard">
        <ArmUrdfViewer jointTargets={targets} profile="clankers" />
      </section>

      <section className="card glass">
        <PoseLibrary
          model={MODEL}
          targets={targets}
          onLoad={setTargets}
          onSend={(next) => {
            setTargets(next);
            return sendToRobot(next, 'send pose');
          }}
          canSend={canSendPose && !busy}
        />
      </section>

      <section className="card glass">
        <div className="row toolbar compactToolbar">
          <h3>Arm</h3>
          <span className={armConn?.connected ? 'okChip' : 'muted'}>
            {armConn?.connected ? 'connected' : 'disconnected'}
          </span>
          <button onClick={() => run('arm enable all', arm.enableAllRobotArm)} disabled={!armReady || busy}>
            Enable all
          </button>
          <button onClick={() => run('arm disable all', arm.disableAllRobotArm)} disabled={!armReady || busy}>
            Disable all
          </button>
        </div>
        {MODEL.arm.map((j) => (
          <JointSlider
            key={j.name}
            joint={j}
            value={targets[j.name] ?? 0}
            onChange={(v) => setJoint(j.name, clampArmJoint(j, v))}
          />
        ))}
        <p className="muted">
          These sliders pose the model. For live per-joint motion, mechanical zeroing and motor
          parameters, open the arm controls below.
        </p>
      </section>

      <section className="card glass">
        <div className="row toolbar compactToolbar">
          <h3>Hand</h3>
          <span className={hand.connected ? 'okChip' : 'muted'}>
            {hand.connected ? 'connected' : 'disconnected'}
          </span>
          <label>
            <input
              type="checkbox"
              checked={mirrorLive}
              onChange={(e) => setMirrorLive(e.target.checked)}
            />{' '}
            mirror live positions
          </label>
          <button onClick={() => run('hand enable all', () => hand.ops.enable())} disabled={!handReady}>
            Enable all
          </button>
          <button onClick={() => run('hand disable all', () => hand.ops.disable())} disabled={!handReady}>
            Disable all
          </button>
          <button onClick={sendHandPose} disabled={!canPoseHand}>
            Send hand pose
          </button>
        </div>

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
                  disabled={!handReady}
                  onClick={() => jog(j, -JOG_STEP_RAD)}
                  aria-label={`jog ${j.name} negative`}
                >
                  −
                </button>
                <button
                  disabled={!handReady}
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

      <CollapsibleSection
        title="Arm controls"
        collapsed={!armOpen}
        onToggleCollapsed={() => setArmOpen((v) => !v)}
        collapsedHint="Live per-joint motion, motor parameters, self-check, and Set Mechanical Zero — which rewrites the encoder reference and is not the same as Go to zero pose."
      >
        <RobotArmPage showViewer={false} />
      </CollapsibleSection>

      <CollapsibleSection
        title="Hand diagnostics"
        collapsed={!handOpen}
        onToggleCollapsed={() => setHandOpen((v) => !v)}
        collapsedHint="Per-servo telemetry, temperature and current, fault decoding, bus scan."
      >
        <LeapHandPage />
      </CollapsibleSection>
    </div>
  );
}
