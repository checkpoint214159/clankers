import { useEffect, useRef, useState } from 'react';
import { WsGatewayClient } from '../wsGatewayClient';
import robotsConfig from '../generated/robotsConfig.json';

const DEFAULT_TASK_RUNNER_WS_URL = `ws://127.0.0.1:${robotsConfig?.ports?.task_runner_ws ?? 9010}`;

// Same exponential-backoff-with-jitter reconnect shape as useGatewayBridge/useHandGateway.
function reconnectDelayMs(attempt) {
  const base = 600;
  const max = 5000;
  const exp = Math.min(max, base * 2 ** Math.max(0, attempt));
  return exp + Math.floor(Math.random() * 300);
}

/** WS connection to the task runner (:9010, ADR-0002). Lists poses/skills, runs them, tracks
 * task events for progress, and mirrors the live joint state push so the Tasks page stays
 * informative even when the debug gateways are idle. */
export function useTaskRunner({ wsUrl = DEFAULT_TASK_RUNNER_WS_URL } = {}) {
  const [status, setStatus] = useState('disconnected'); // disconnected | connecting | connected
  const [lastError, setLastError] = useState('');
  const [poses, setPoses] = useState([]);
  const [skills, setSkills] = useState([]);
  const [positions, setPositions] = useState(null);
  const [lastStateTs, setLastStateTs] = useState(null);
  const [running, setRunning] = useState(null); // { kind: 'pose'|'skill', name } | null
  const [lastEvent, setLastEvent] = useState(null); // { event, name, at }

  const clientRef = useRef(null);
  const shouldAutoReconnectRef = useRef(false);
  const connectingRef = useRef(false);
  const reconnectTimerRef = useRef(null);
  const reconnectAttemptRef = useRef(0);
  const wsUrlRef = useRef(wsUrl);
  wsUrlRef.current = wsUrl;

  const clearReconnectTimer = () => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  };

  const refreshPoseList = async () => {
    try {
      const ret = await clientRef.current?.send('pose_list', {}, 5000);
      if (!ret?.ok) return;
      setPoses(Array.isArray(ret.data?.poses) ? ret.data.poses : []);
      setSkills(Array.isArray(ret.data?.skills) ? ret.data.skills : []);
    } catch (e) {
      setLastError(e?.message || String(e));
    }
  };

  const ensureClient = () => {
    if (clientRef.current) return clientRef.current;
    clientRef.current = new WsGatewayClient({
      onOpen: () => {
        clearReconnectTimer();
        reconnectAttemptRef.current = 0;
        connectingRef.current = false;
        setStatus('connected');
        refreshPoseList();
        clientRef.current?.send('state_once', {}, 3000).catch(() => {});
      },
      onClose: () => {
        connectingRef.current = false;
        setStatus('disconnected');
        setRunning(null);
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
        setPositions(data?.positions ?? null);
        setLastStateTs(data?.ts ?? Date.now());
      },
      onMessage: (msg) => {
        if (msg?.type !== 'task') return;
        const { event, name } = msg.data || {};
        setLastEvent({ event, name, at: Date.now() });
        if (event === 'accepted') setRunning({ kind: msg.data?.kind || 'task', name });
        if (event === 'done' || event === 'stopped' || event === 'error') setRunning(null);
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
    connectingRef.current = false;
    setStatus('disconnected');
    setRunning(null);
    ensureClient().disconnect();
  };

  useEffect(() => () => {
    clearReconnectTimer();
    clientRef.current?.disconnect();
  }, []);

  const sendOp = async (op, payload = {}, timeoutMs = 8000) => {
    const client = ensureClient();
    const ret = await client.send(op, payload, timeoutMs);
    if (!ret?.ok) throw new Error(ret?.error || `${op} failed`);
    return ret.data;
  };

  const poseGo = async (name, durationS) => {
    setRunning({ kind: 'pose', name });
    try {
      return await sendOp('pose_go', durationS != null ? { name, duration_s: durationS } : { name });
    } catch (e) {
      setRunning(null);
      throw e;
    }
  };

  const skillRun = async (name) => {
    setRunning({ kind: 'skill', name });
    try {
      return await sendOp('skill_run', { name });
    } catch (e) {
      setRunning(null);
      throw e;
    }
  };

  const stop = async () => {
    try {
      return await sendOp('stop', {});
    } finally {
      setRunning(null);
    }
  };

  return {
    status,
    connected: status === 'connected',
    lastError,
    poses,
    skills,
    positions,
    lastStateTs,
    running,
    lastEvent,
    connect,
    disconnect,
    refreshPoseList,
    wsUrl,
    ops: {
      poseGo,
      skillRun,
      stop,
    },
  };
}
