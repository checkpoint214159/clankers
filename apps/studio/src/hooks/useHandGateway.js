import { useEffect, useRef, useState } from 'react';
import { WsGatewayClient } from '../wsGatewayClient';
import robotsConfig from '../generated/robotsConfig.json';

const DEFAULT_HAND_WS_URL = `ws://127.0.0.1:${robotsConfig?.ports?.hand_gateway_ws ?? 9003}`;
const HEARTBEAT_MS = 500;

// Same exponential-backoff-with-jitter shape as useGatewayBridge, generalized: no channel/
// query-token handshake (the hand gateway doesn't need one), and no i18n coupling since this
// is a second, independent WS connection with its own status text.
function reconnectDelayMs(attempt) {
  const base = 600;
  const max = 5000;
  const exp = Math.min(max, base * 2 ** Math.max(0, attempt));
  return exp + Math.floor(Math.random() * 300);
}

/** Second WS connection to the hand gateway (ADR-0002), reusing WsGatewayClient verbatim.
 * Tracks live joint/health state pushes, runs the required heartbeat while any servo is
 * enabled, and surfaces watchdog_trip events for the UI to display (never auto-clears one —
 * ADR-0004 requires an explicit operator ack via the reboot op). */
export function useHandGateway({ wsUrl = DEFAULT_HAND_WS_URL, heartbeatMs = HEARTBEAT_MS } = {}) {
  const [status, setStatus] = useState('disconnected'); // disconnected | connecting | connected
  const [lastError, setLastError] = useState('');
  const [joints, setJoints] = useState([]);
  const [health, setHealth] = useState([]);
  const [lastStateTs, setLastStateTs] = useState(null);
  const [watchdogTrip, setWatchdogTrip] = useState(null); // { at, detail } | null

  const clientRef = useRef(null);
  const shouldAutoReconnectRef = useRef(false);
  const connectingRef = useRef(false);
  const reconnectTimerRef = useRef(null);
  const reconnectAttemptRef = useRef(0);
  const enabledIdsRef = useRef(new Set());
  const enabledAllRef = useRef(false);
  const heartbeatTimerRef = useRef(null);
  const wsUrlRef = useRef(wsUrl);
  wsUrlRef.current = wsUrl;

  const clearReconnectTimer = () => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  };

  const clearHeartbeat = () => {
    if (heartbeatTimerRef.current) {
      clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = null;
    }
  };

  const anyEnabled = () => enabledAllRef.current || enabledIdsRef.current.size > 0;

  const startHeartbeatIfNeeded = () => {
    if (heartbeatTimerRef.current) return;
    if (!anyEnabled()) return;
    heartbeatTimerRef.current = setInterval(() => {
      clientRef.current?.send('heartbeat', {}, 2000).catch(() => {
        // A dropped heartbeat is not fatal by itself — the gateway's own watchdog will trip
        // and push a watchdog_trip event; nothing to reconcile locally here.
      });
    }, heartbeatMs);
  };

  const stopHeartbeatIfIdle = () => {
    if (!anyEnabled()) clearHeartbeat();
  };

  const ensureClient = () => {
    if (clientRef.current) return clientRef.current;
    clientRef.current = new WsGatewayClient({
      onOpen: () => {
        clearReconnectTimer();
        reconnectAttemptRef.current = 0;
        connectingRef.current = false;
        setStatus('connected');
        clientRef.current?.send('state_stream', { enabled: true }, 3000).catch(() => {});
      },
      onClose: () => {
        connectingRef.current = false;
        setStatus('disconnected');
        clearHeartbeat();
        if (!shouldAutoReconnectRef.current) return;
        clearReconnectTimer();
        const attempt = reconnectAttemptRef.current + 1;
        reconnectAttemptRef.current = attempt;
        const delay = reconnectDelayMs(attempt - 1);
        setStatus('connecting');
        reconnectTimerRef.current = setTimeout(() => {
          reconnectTimerRef.current = null;
          connectNow(true);
        }, delay);
      },
      onError: () => {
        connectingRef.current = false;
        setLastError('ws error');
      },
      onState: (data) => {
        setJoints(Array.isArray(data?.joints) ? data.joints : []);
        setHealth(Array.isArray(data?.health) ? data.health : []);
        setLastStateTs(data?.ts ?? Date.now());
      },
      onMessage: (msg) => {
        if (msg?.type === 'event' && msg?.data?.event === 'watchdog_trip') {
          setWatchdogTrip({ at: Date.now(), detail: msg.data });
          // A tripped watchdog means the gateway has torqued off; local enabled-tracking is
          // stale until the operator re-enables, so stop heartbeating rather than spin.
          enabledAllRef.current = false;
          enabledIdsRef.current.clear();
          clearHeartbeat();
        }
      },
    });
    return clientRef.current;
  };

  const connectNow = (isReconnect = false) => {
    if (connectingRef.current) return;
    const client = ensureClient();
    if (client.isConnected() && !isReconnect) return;
    connectingRef.current = true;
    setStatus('connecting');
    setLastError('');
    try {
      client.connect(wsUrlRef.current);
    } catch (e) {
      connectingRef.current = false;
      setStatus('disconnected');
      setLastError(e?.message || String(e));
    }
  };

  const connect = () => {
    shouldAutoReconnectRef.current = true;
    clearReconnectTimer();
    connectNow(false);
  };

  const disconnect = () => {
    shouldAutoReconnectRef.current = false;
    reconnectAttemptRef.current = 0;
    clearReconnectTimer();
    clearHeartbeat();
    connectingRef.current = false;
    enabledAllRef.current = false;
    enabledIdsRef.current.clear();
    setStatus('disconnected');
    ensureClient().disconnect();
  };

  useEffect(() => () => {
    clearReconnectTimer();
    clearHeartbeat();
    clientRef.current?.disconnect();
  }, []);

  const sendOp = async (op, payload = {}, timeoutMs = 8000) => {
    const client = ensureClient();
    const ret = await client.send(op, payload, timeoutMs);
    if (!ret?.ok) throw new Error(ret?.error || `${op} failed`);
    return ret.data;
  };

  const scan = (baud) => sendOp('scan', baud != null ? { baud } : {});

  const enable = async (servoIds) => {
    const data = await sendOp('enable', servoIds ? { servo_ids: servoIds } : {});
    if (servoIds) servoIds.forEach((id) => enabledIdsRef.current.add(id));
    else enabledAllRef.current = true;
    startHeartbeatIfNeeded();
    return data;
  };

  const disable = async (servoIds) => {
    const data = await sendOp('disable', servoIds ? { servo_ids: servoIds } : {});
    if (servoIds) servoIds.forEach((id) => enabledIdsRef.current.delete(id));
    else {
      enabledAllRef.current = false;
      enabledIdsRef.current.clear();
    }
    stopHeartbeatIfIdle();
    return data;
  };

  const jog = (servoId, deltaRad) => sendOp('jog', { servo_id: servoId, delta_rad: deltaRad });

  const pos = (targets) => sendOp('pos', { targets });

  const errorStatus = () => sendOp('error_status', {});

  // EEPROM writes: the gateway refuses both unless confirm is set, and refuses while torque
  // is on (the servo locks its EEPROM when powered).
  const setMechanicalZero = (payload) => sendOp('set_mechanical_zero', payload || {}, 20000);

  const restoreHomingOffsets = (payload) =>
    sendOp('restore_homing_offsets', payload || {}, 20000);

  const reboot = (servoId) => sendOp('reboot', { servo_id: servoId });

  const clearWatchdogTrip = () => setWatchdogTrip(null);

  return {
    status,
    connected: status === 'connected',
    lastError,
    joints,
    health,
    lastStateTs,
    watchdogTrip,
    clearWatchdogTrip,
    connect,
    disconnect,
    wsUrl,
    ops: {
      scan,
      enable,
      disable,
      jog,
      pos,
      errorStatus,
      reboot,
      setMechanicalZero,
      restoreHomingOffsets,
    },
  };
}
