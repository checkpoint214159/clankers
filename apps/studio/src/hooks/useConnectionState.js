import { useState } from 'react';
import { APP_DEFAULTS } from '../lib/appConfig';
import { useGatewayBridge } from './useGatewayBridge';
import { usePersistedState } from './usePersistedState';

const LS_WS_URL_KEY = 'motorbridge_studio_ws_url_v1';
const LS_WS_TOKEN_ENABLED_KEY = 'motorbridge_studio_ws_token_enabled_v1';
const LS_WS_TOKEN_KEY = 'motorbridge_studio_ws_token_v1';

export function useConnectionState({ pushLog, setStateSnapshot, onGatewayState, onGatewayParams }) {
  const [wsUrl, setWsUrl] = usePersistedState(LS_WS_URL_KEY, APP_DEFAULTS.wsUrl, (cached, fallback) => {
    const value = String(cached ?? '').trim();
    return value || fallback;
  });
  const [channel, setChannel] = useState(APP_DEFAULTS.channel);
  const [scanTimeoutMs, setScanTimeoutMs] = useState(APP_DEFAULTS.scanTimeoutMs);
  // Default the toggle on when a token was supplied by the environment: someone who set
  // VITE_MOTORBRIDGE_WS_TOKEN plainly intends to use it, and a token that is present but
  // switched off fails as an opaque handshake rejection.
  const [wsTokenEnabled, setWsTokenEnabled] = usePersistedState(
    LS_WS_TOKEN_ENABLED_KEY,
    Boolean(APP_DEFAULTS.wsToken),
    (cached) => Boolean(cached)
  );
  const [wsToken, setWsToken] = usePersistedState(LS_WS_TOKEN_KEY, APP_DEFAULTS.wsToken, (cached, fallback) =>
    String(cached ?? '').trim() || fallback
  );

  const bridge = useGatewayBridge({
    wsUrl,
    channel,
    wsTokenEnabled,
    wsToken,
    pushLog,
    setStateSnapshot,
    onGatewayState,
    onGatewayParams,
  });

  return {
    wsUrl,
    setWsUrl,
    channel,
    setChannel,
    scanTimeoutMs,
    setScanTimeoutMs,
    wsTokenEnabled,
    setWsTokenEnabled,
    wsToken,
    setWsToken,
    ...bridge,
  };
}
