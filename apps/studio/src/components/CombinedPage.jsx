import React from 'react';
import robotsConfig from '../generated/robotsConfig.json';
import { useHandGatewayContext } from '../hooks/useHandGatewayContext';
import {
  useConnectionContext,
  useControlContext,
  useRobotArmContext,
} from '../hooks/useMotorStudioContext';
import { useCoalescedSender } from '../hooks/useCoalescedSender';
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
const ARM_NAMES = new Set(MODEL.arm.map((j) => j.name));

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
  const { controlMotor, patchControl } = useControlContext();

  const [targets, setTargets] = React.useState(() => zeroTargets(MODEL));
  const [mirrorLive, setMirrorLive] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [note, setNote] = React.useState('');
  const [armOpen, setArmOpen] = React.useState(false);
  const [handOpen, setHandOpen] = React.useState(false);
  const [armLive, setArmLive] = React.useState(false);
  const [handLive, setHandLive] = React.useState(false);

  // Mirror of `targets` for event handlers, which need the value they are about to set
  // without waiting for a re-render to queue it onto the bus.
  const targetsRef = React.useRef(targets);
  targetsRef.current = targets;
  const lastArmSentRef = React.useRef({});

  const armReady = Boolean(armConn?.connected) && !arm?.armBulkBusy;
  const handReady = hand.connected && !busy;
  const canPoseHand = handReady && MODEL.hand.calibrated;

  // Live driving and live mirroring pull the same sliders in opposite directions -- the
  // operator drags, the hand reports where it actually got to, the slider jumps back. While
  // the hand is being driven live, measurement yields to intent.
  React.useEffect(() => {
    if (!mirrorLive || handLive || !hand.connected) return;
    setTargets((prev) => applyLiveHandPositions(prev, MODEL, hand.joints));
  }, [mirrorLive, handLive, hand.connected, hand.joints]);

  const handTargetsFrom = React.useCallback((source) => {
    const out = {};
    for (const j of MODEL.hand.joints) out[j.name] = clampToLimit(j, source?.[j.name] ?? 0);
    return out;
  }, []);

  const armRowByName = React.useMemo(() => {
    const out = {};
    for (const row of arm?.robotArmJointRows || []) {
      const joint = MODEL.arm.find((a) => a.joint === Number(row?.joint));
      if (joint) out[joint.name] = row;
    }
    return out;
  }, [arm?.robotArmJointRows]);

  /**
   * Command the six arm joints. Unchanged joints are skipped unless `force`, so a live drag
   * of one slider does not re-send the other five on every tick of a shared serial bus.
   */
  const sendArmTargets = React.useCallback(
    async (next, { force = false } = {}) => {
      for (const joint of MODEL.arm) {
        const row = armRowByName[joint.name];
        if (!row?.hit) continue;
        // MIT mode takes torque/impedance commands, not a position target.
        if (String(row?.control?.mode) === 'mit') continue;
        const target = clampArmJoint(joint, next[joint.name]);
        const last = lastArmSentRef.current[joint.name];
        if (!force && Number.isFinite(last) && Math.abs(last - target) < 1e-4) continue;
        lastArmSentRef.current[joint.name] = target;
        patchControl?.(row.key, { target });
        await controlMotor(row.hit, 'move', { target });
      }
    },
    [armRowByName, controlMotor, patchControl],
  );

  const armSender = useCoalescedSender(
    React.useCallback((next) => sendArmTargets(next), [sendArmTargets]),
  );
  const handSender = useCoalescedSender(
    React.useCallback((next) => hand.ops.pos(handTargetsFrom(next)), [hand.ops, handTargetsFrom]),
  );

  const setJoint = React.useCallback(
    (name, value) => {
      const next = { ...targetsRef.current, [name]: value };
      targetsRef.current = next;
      setTargets(next);
      if (ARM_NAMES.has(name)) {
        if (armLive && armReady) armSender.queue(next);
      } else if (handLive && canPoseHand) {
        handSender.queue(next);
      }
    },
    [armLive, armReady, handLive, canPoseHand, armSender, handSender],
  );

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
        // Send the pose that was asked for. This used to call resetPoseRobotArm(), which
        // always drives the arm to zero -- fine for "go to zero pose", wrong for every
        // saved pose, which would have silently zeroed the arm instead.
        if (armReady) await sendArmTargets(next, { force: true });
        if (canPoseHand) await hand.ops.pos(handTargetsFrom(next));
      });
      if (skipped.length) setNote((n) => `${n} — skipped ${skipped.join(', ')}`);
    },
    [armReady, canPoseHand, sendArmTargets, hand.ops, hand.connected, handTargetsFrom, run],
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
          <label title="Send each slider change to the arm as you drag">
            <input
              type="checkbox"
              checked={armLive}
              disabled={!armReady}
              onChange={(e) => {
                setArmLive(e.target.checked);
                if (!e.target.checked) armSender.stop();
              }}
              aria-label="live arm"
            />{' '}
            live
          </label>
          <button
            onClick={() => run('send arm pose', () => sendArmTargets(targetsRef.current, { force: true }))}
            disabled={!armReady || busy}
          >
            Send arm pose
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
          {armLive
            ? 'Live: each slider change is sent to the arm as you drag, newest target wins.'
            : 'Manual: sliders pose the model only until you press Send arm pose.'}{' '}
          Looking for <strong>Set Mechanical Zero</strong> (rewrites the encoder reference — not
          the same as Go to zero pose)?{' '}
          <button className="ghostBtn small" onClick={() => setArmOpen(true)}>
            Open arm controls
          </button>
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
          <label title="Send the whole hand pose as you drag">
            <input
              type="checkbox"
              checked={handLive}
              disabled={!canPoseHand}
              onChange={(e) => {
                setHandLive(e.target.checked);
                if (!e.target.checked) handSender.stop();
              }}
              aria-label="live hand"
            />{' '}
            live
          </label>
          <button onClick={sendHandPose} disabled={!canPoseHand}>
            Send hand pose
          </button>
        </div>
        {handLive && mirrorLive && (
          <p className="muted">
            Live driving pauses position mirroring — otherwise the measured position fights the
            slider you are dragging.
          </p>
        )}

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
