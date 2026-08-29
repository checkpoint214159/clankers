import React from 'react';
import robotsConfig from '../generated/robotsConfig.json';
import { useConnectionContext, useRobotArmContext } from '../hooks/useMotorStudioContext';
import { useHandGatewayContext } from '../hooks/useHandGatewayContext';
import '../styles/combined.css';

const PORTS = robotsConfig.ports || {};

function StatusDot({ state }) {
  return <span className={`connDot connDot--${state}`} aria-hidden="true" />;
}

function GatewayRow({ label, hint, url, onUrlChange, state, statusText, onConnect, onDisconnect, busy }) {
  const connected = state === 'connected';
  return (
    <div className="gatewayRow">
      <div className="gatewayLabel">
        <StatusDot state={state} />
        <strong>{label}</strong>
        <span className="muted">{hint}</span>
      </div>
      <input
        aria-label={`${label} url`}
        value={url}
        onChange={(e) => onUrlChange(e.target.value)}
        disabled={connected}
        spellCheck={false}
      />
      <span className={connected ? 'okChip' : 'muted'}>{statusText}</span>
      <button className="primary" onClick={onConnect} disabled={connected || busy}>
        Connect
      </button>
      <button onClick={onDisconnect} disabled={!connected || busy}>
        Disconnect
      </button>
    </div>
  );
}

/** Both gateways in one place: the Damiao arm bridge (:9002) and the LEAP hand (:9003). */
export function GatewayConnections() {
  const conn = useConnectionContext();
  const arm = useRobotArmContext();
  const hand = useHandGatewayContext();

  const armState = conn?.connected ? 'connected' : 'disconnected';
  const handState = hand.status === 'connected' ? 'connected'
    : hand.status === 'connecting' ? 'connecting' : 'disconnected';

  // The arm page used to be what created the joint cards; with one page it has to happen on
  // connect, or the arm controls come up with nothing to drive.
  const connectArm = React.useCallback(() => {
    arm?.ensureRobotArmCards?.();
    conn?.connectWs?.();
  }, [arm, conn]);

  const connectBoth = React.useCallback(() => {
    if (!conn?.connected) connectArm();
    if (hand.status !== 'connected') hand.connect();
  }, [conn, connectArm, hand]);

  const disconnectBoth = React.useCallback(() => {
    if (conn?.connected) conn.disconnectWs();
    if (hand.status !== 'disconnected') hand.disconnect();
  }, [conn, hand]);

  const bothConnected = conn?.connected && hand.status === 'connected';
  const noneConnected = !conn?.connected && hand.status === 'disconnected';

  return (
    <section className="card glass">
      <div className="row toolbar compactToolbar">
        <strong>Gateways</strong>
        <span className="muted">one process per bus — see docs/glossary.md</span>
        <button className="primary strong" onClick={connectBoth} disabled={bothConnected}>
          Connect both
        </button>
        <button onClick={disconnectBoth} disabled={noneConnected}>
          Disconnect both
        </button>
      </div>

      <GatewayRow
        label="Arm"
        hint={`motorbridge · Damiao serial · :${PORTS.arm_gateway_ws ?? 9002}`}
        url={conn?.wsUrl ?? ''}
        onUrlChange={(v) => conn?.setWsUrl?.(v)}
        state={armState}
        statusText={conn?.connText || armState}
        onConnect={connectArm}
        onDisconnect={() => conn?.disconnectWs?.()}
        busy={Boolean(arm?.armBulkBusy)}
      />

      <GatewayRow
        label="Hand"
        hint={`LEAP · Dynamixel U2D2 · :${PORTS.hand_gateway_ws ?? 9003}`}
        url={hand.wsUrl}
        onUrlChange={() => {}}
        state={handState}
        statusText={handState}
        onConnect={hand.connect}
        onDisconnect={hand.disconnect}
      />

      {hand.lastError && <p className="warnBanner">hand gateway: {hand.lastError}</p>}
      {hand.watchdogTrip && (
        <p className="warnBanner" role="status">
          Hand watchdog tripped — torque was cut. Re-enable when you are ready.{' '}
          <button className="ghostBtn small" onClick={hand.clearWatchdogTrip}>
            Dismiss
          </button>
        </p>
      )}
    </section>
  );
}
