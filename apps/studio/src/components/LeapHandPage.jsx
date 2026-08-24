import React from 'react';
import robotsConfig from '../generated/robotsConfig.json';
import { useHandGateway } from '../hooks/useHandGateway';
import {
  buildHandModel,
  buildPresetTargets,
  findDuplicateServoIds,
  formatCurrentMa,
  formatJointPosition,
  formatTemperatureC,
  formatVelocity,
  jointFaultChips,
  temperatureSeverity,
} from '../lib/leapHand';
import '../styles/leap-hand.css';

const HAND_MODEL = buildHandModel(robotsConfig);
const JOG_STEP_RAD = Number(HAND_MODEL.safety?.max_step_rad) || 0.05;

function statusLabel(status) {
  if (status === 'connected') return 'Connected';
  if (status === 'connecting') return 'Connecting…';
  return 'Disconnected';
}

function JointCard({ joint, liveJoint, liveHealth, busy, connected, onJog }) {
  const pos = formatJointPosition(liveJoint?.pos);
  const tempSeverity = temperatureSeverity(liveHealth?.temp_c);
  const faults = jointFaultChips(liveHealth?.faults);

  return (
    <div className="jointCard">
      <div className="jointCardTop">
        <span className="jointCardName">{joint.name}</span>
        <span className="jointCardServo">servo {joint.servoId}</span>
      </div>

      <div className="faultChipRow">
        {faults.map((f) => (
          <span key={f.key} className={`faultChip faultChip--${f.severity}`}>
            {f.label}
          </span>
        ))}
      </div>

      <div className="jointCardTelemetry">
        <div>
          <b>pos</b>
          <span>{pos.deg}</span> / <span>{pos.rad}</span>
        </div>
        <div>
          <b>vel</b>
          {formatVelocity(liveJoint?.vel)}
        </div>
        <div>
          <b>I</b>
          {formatCurrentMa(liveJoint?.current_ma)}
        </div>
        <div>
          <b>temp</b>
          <span className={`tempBadge ${tempSeverity}`}>{formatTemperatureC(liveHealth?.temp_c)}</span>
        </div>
      </div>

      <div className="jogRow">
        <button
          className="jogBtn ghostBtn"
          disabled={!connected || busy}
          title={`jog -${JOG_STEP_RAD.toFixed(3)} rad`}
          onClick={() => onJog(joint, -JOG_STEP_RAD)}
        >
          −
        </button>
        <span className="tip">step {JOG_STEP_RAD.toFixed(3)} rad</span>
        <button
          className="jogBtn ghostBtn"
          disabled={!connected || busy}
          title={`jog +${JOG_STEP_RAD.toFixed(3)} rad`}
          onClick={() => onJog(joint, JOG_STEP_RAD)}
        >
          +
        </button>
      </div>
    </div>
  );
}

export function LeapHandPage() {
  const hand = useHandGateway();
  const [scanResult, setScanResult] = React.useState(null);
  const [scanBusy, setScanBusy] = React.useState(false);
  const [busyServoIds, setBusyServoIds] = React.useState(() => new Set());
  const [log, setLog] = React.useState([]);

  const pushLog = React.useCallback((msg, level = 'info') => {
    setLog((prev) => [...prev, { t: new Date().toLocaleTimeString(), msg, level }].slice(-200));
  }, []);

  const jointByServoId = React.useMemo(() => {
    const map = new Map();
    (hand.joints || []).forEach((j) => map.set(Number(j.servo_id), j));
    return map;
  }, [hand.joints]);

  const healthByServoId = React.useMemo(() => {
    const map = new Map();
    (hand.health || []).forEach((h) => map.set(Number(h.servo_id), h));
    return map;
  }, [hand.health]);

  const runOp = React.useCallback(
    async (label, fn) => {
      try {
        const data = await fn();
        pushLog(`${label}: ok`, 'ok');
        return data;
      } catch (e) {
        pushLog(`${label}: ${e.message || e}`, 'err');
        throw e;
      }
    },
    [pushLog]
  );

  const handleScan = async () => {
    setScanBusy(true);
    try {
      const data = await runOp('scan', () => hand.ops.scan());
      setScanResult(data);
    } catch {
      // already logged by runOp
    } finally {
      setScanBusy(false);
    }
  };

  const servoIdsForFinger = (finger) => (HAND_MODEL.byFinger[finger] || []).map((j) => j.servoId);

  const handleEnable = (servoIds, label) => runOp(label, () => hand.ops.enable(servoIds)).catch(() => {});
  const handleDisable = (servoIds, label) => runOp(label, () => hand.ops.disable(servoIds)).catch(() => {});

  const handleJog = async (joint, delta) => {
    setBusyServoIds((prev) => new Set(prev).add(joint.servoId));
    try {
      await runOp(`jog ${joint.name}`, () => hand.ops.jog(joint.servoId, delta));
    } catch {
      // already logged by runOp
    } finally {
      setBusyServoIds((prev) => {
        const next = new Set(prev);
        next.delete(joint.servoId);
        return next;
      });
    }
  };

  const handlePreset = (preset) => {
    // Defense in depth: the buttons are already disabled when not calibrated, but never
    // trust UI-only gating (CLAUDE.md) — mirror the gateway's own provisional-map guard here.
    if (!HAND_MODEL.calibrated) return;
    const targets = buildPresetTargets(HAND_MODEL, preset);
    runOp(`pos ${preset}`, () => hand.ops.pos(targets)).catch(() => {});
  };

  const scanDuplicates =
    scanResult?.duplicates?.length ? scanResult.duplicates : findDuplicateServoIds(scanResult?.hits);

  return (
    <section className="card glass">
      <div className="sectionTitle">
        <h2>LEAP Hand</h2>
        <span className="tip">
          16x Dynamixel XC330 over U2D2 · gateway :{robotsConfig?.ports?.hand_gateway_ws ?? 9003}
        </span>
      </div>

      {!HAND_MODEL.calibrated && (
        <div className="provisionalBanner">
          Provisional joint map — hardware bring-up has not confirmed servo IDs/signs yet. Jog
          only; pose presets stay disabled until hand.calibrated is true in robots.yaml.
        </div>
      )}

      {hand.watchdogTrip && (
        <div className="watchdogTripBanner">
          Watchdog tripped on the hand bus — torque is off. Faults require an explicit reboot
          per joint before re-enabling (ADR-0004); auto-clearing is not offered here.
        </div>
      )}

      <div className="handConnCard">
        <span className={`handStatusDot ${hand.status}`} aria-hidden="true" />
        <span>{statusLabel(hand.status)}</span>
        <span className="handConnUrl">{hand.wsUrl}</span>
        <div className="row compactToolbar">
          <button className="primary" disabled={hand.status !== 'disconnected'} onClick={hand.connect}>
            Connect
          </button>
          <button disabled={hand.status === 'disconnected'} onClick={hand.disconnect}>
            Disconnect
          </button>
          <button className="ghostBtn" disabled={!hand.connected || scanBusy} onClick={handleScan}>
            {scanBusy ? 'Scanning…' : 'Scan Bus'}
          </button>
        </div>
      </div>

      {scanResult && (
        <div className="handScanResult">
          <div className="tip">{scanResult.hits?.length ?? 0} servo(s) responded</div>
          {scanDuplicates.length > 0 && (
            <div className="dupWarningBanner">
              Duplicate servo IDs on the bus: {scanDuplicates.join(', ')} — resolve before
              enabling torque.
            </div>
          )}
        </div>
      )}

      <div className="taskStopBar">
        <button
          className="dangerBtn taskStopBtn"
          disabled={!hand.connected}
          onClick={() => handleDisable(undefined, 'disable all')}
        >
          ■ Disable All
        </button>
        <button disabled={!hand.connected} onClick={() => handleEnable(undefined, 'enable all')}>
          Enable All
        </button>
      </div>

      <div className="presetGrid">
        <button
          disabled={!hand.connected || !HAND_MODEL.calibrated}
          title={HAND_MODEL.calibrated ? '' : 'disabled — provisional map, jog only'}
          onClick={() => handlePreset('open')}
        >
          Open Hand
        </button>
        <button
          disabled={!hand.connected || !HAND_MODEL.calibrated}
          title={HAND_MODEL.calibrated ? '' : 'disabled — provisional map, jog only'}
          onClick={() => handlePreset('curl')}
        >
          Curl Hand
        </button>
      </div>

      <div className="fingerGrid">
        {HAND_MODEL.fingers.map((finger) => (
          <div className="fingerSection" key={finger}>
            <div className="fingerSectionHead">
              <h3>{finger}</h3>
              <div className="row compactToolbar">
                <button
                  className="ghostBtn small"
                  disabled={!hand.connected}
                  onClick={() => handleEnable(servoIdsForFinger(finger), `enable ${finger}`)}
                >
                  Enable
                </button>
                <button
                  className="ghostBtn small"
                  disabled={!hand.connected}
                  onClick={() => handleDisable(servoIdsForFinger(finger), `disable ${finger}`)}
                >
                  Disable
                </button>
              </div>
            </div>
            <div className="jointCardStack">
              {(HAND_MODEL.byFinger[finger] || []).map((joint) => (
                <JointCard
                  key={joint.servoId}
                  joint={joint}
                  liveJoint={jointByServoId.get(joint.servoId)}
                  liveHealth={healthByServoId.get(joint.servoId)}
                  busy={busyServoIds.has(joint.servoId)}
                  connected={hand.connected}
                  onJog={handleJog}
                />
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className="box logs" style={{ marginTop: 12 }}>
        {log.length === 0 ? (
          <span className="tip">(no activity yet)</span>
        ) : (
          log.map((l, i) => (
            <div key={i} className={l.level === 'err' ? 'errText' : undefined}>
              [{l.t}] {l.msg}
            </div>
          ))
        )}
      </div>
    </section>
  );
}
