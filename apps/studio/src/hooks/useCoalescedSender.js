import React from 'react';

export const DEFAULT_SEND_INTERVAL_MS = 80;

/**
 * Rate-limit a stream of pose updates onto a serial bus: keep only the newest target and
 * send it when the previous send finishes.
 *
 * Dragging a slider fires far faster than a motor bus can accept commands, and the stale
 * intermediate values are worthless — only the latest matters. Same shape as the arm page's
 * LiveMoveScheduler (80 ms, latest-wins, never two sends in flight), but for a whole pose
 * instead of one joint.
 *
 * `send` may be async; a rejection is swallowed here and left to the caller's own reporting,
 * because a live drag that hits one refusal should not tear the loop down.
 */
export function useCoalescedSender(send, intervalMs = DEFAULT_SEND_INTERVAL_MS) {
  const pendingRef = React.useRef(null);
  const timerRef = React.useRef(null);
  const inFlightRef = React.useRef(false);
  const sendRef = React.useRef(send);
  sendRef.current = send;

  const stop = React.useCallback(() => {
    pendingRef.current = null;
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const flush = React.useCallback(async () => {
    if (inFlightRef.current) return;
    const payload = pendingRef.current;
    if (payload === null) {
      // Nothing queued since the last send: idle out rather than tick forever.
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      return;
    }
    pendingRef.current = null;
    inFlightRef.current = true;
    try {
      await sendRef.current(payload);
    } catch {
      // reported by the caller
    } finally {
      inFlightRef.current = false;
    }
  }, []);

  const queue = React.useCallback(
    (payload) => {
      pendingRef.current = payload;
      if (!timerRef.current) {
        flush();
        timerRef.current = setInterval(flush, intervalMs);
      }
    },
    [flush, intervalMs],
  );

  React.useEffect(() => stop, [stop]);

  // Memoized: this object lands in effect dependency arrays, and a fresh identity every
  // render makes those effects re-run every render.
  return React.useMemo(() => ({ queue, stop }), [queue, stop]);
}
