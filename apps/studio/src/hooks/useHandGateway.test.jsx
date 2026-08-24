// @vitest-environment jsdom
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useHandGateway } from './useHandGateway';

class FakeWebSocket {
  static OPEN = 1;

  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.onopen = null;
    this.onclose = null;
    this.onerror = null;
    this.onmessage = null;
    this.sent = [];
    FakeWebSocket.instances.push(this);
  }

  close() {
    this.readyState = 2;
  }

  send(data) {
    this.sent.push(JSON.parse(data));
  }

  emitOpen() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  emitMessage(obj) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }

  /** Respond ok:true to the most recently sent request for `op` (defaults to the very last). */
  respondOk(op, data = {}) {
    const req = [...this.sent].reverse().find((m) => !op || m.op === op);
    if (!req) throw new Error(`no sent request for op=${op}`);
    this.emitMessage({ ok: true, op: req.op, req_id: req.req_id, data });
  }

  respondErr(op, error = 'failed') {
    const req = [...this.sent].reverse().find((m) => !op || m.op === op);
    if (!req) throw new Error(`no sent request for op=${op}`);
    this.emitMessage({ ok: false, op: req.op, req_id: req.req_id, error });
  }
}

FakeWebSocket.instances = [];

function Harness({ wsUrl, heartbeatMs }) {
  const hand = useHandGateway({ wsUrl, heartbeatMs });
  const [result, setResult] = React.useState('');

  const run = (label, fn) => async () => {
    try {
      const data = await fn();
      setResult(`${label}:ok:${JSON.stringify(data)}`);
    } catch (e) {
      setResult(`${label}:err:${e.message}`);
    }
  };

  return (
    <>
      <div data-testid="status">{hand.status}</div>
      <div data-testid="joints">{JSON.stringify(hand.joints)}</div>
      <div data-testid="health">{JSON.stringify(hand.health)}</div>
      <div data-testid="watchdog">{hand.watchdogTrip ? 'tripped' : 'ok'}</div>
      <div data-testid="result">{result}</div>
      <button onClick={hand.connect}>Connect</button>
      <button onClick={hand.disconnect}>Disconnect</button>
      <button onClick={run('scan', () => hand.ops.scan())}>Scan</button>
      <button onClick={run('enable', () => hand.ops.enable())}>EnableAll</button>
      <button onClick={run('enableOne', () => hand.ops.enable([3]))}>EnableOne</button>
      <button onClick={run('disable', () => hand.ops.disable())}>DisableAll</button>
      <button onClick={run('jog', () => hand.ops.jog(3, 0.05))}>Jog</button>
      <button onClick={run('pos', () => hand.ops.pos({ index_mcp_side: 0.1 }))}>Pos</button>
      <button onClick={run('errorStatus', () => hand.ops.errorStatus())}>ErrorStatus</button>
      <button onClick={run('reboot', () => hand.ops.reboot(3))}>Reboot</button>
    </>
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  FakeWebSocket.instances = [];
});

function connectAndOpen(url = 'ws://127.0.0.1:9003') {
  render(<Harness wsUrl={url} />);
  fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
  act(() => {
    FakeWebSocket.instances[0].emitOpen();
  });
  return FakeWebSocket.instances[0];
}

describe('useHandGateway connection', () => {
  it('connects to the given ws url and reports connected status', () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    connectAndOpen('ws://127.0.0.1:9003');
    expect(FakeWebSocket.instances[0].url).toBe('ws://127.0.0.1:9003');
    expect(screen.getByTestId('status').textContent).toBe('connected');
  });

  it('enables the state stream on open', () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    expect(ws.sent.some((m) => m.op === 'state_stream' && m.enabled === true)).toBe(true);
  });

  it('updates joints/health from state pushes', () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    act(() => {
      ws.emitMessage({
        type: 'state',
        data: {
          ts: 123,
          joints: [{ servo_id: 0, name: 'index_mcp_side', pos: 0.1, vel: 0, current_ma: 12 }],
          health: [{ servo_id: 0, temp_c: 40, voltage_v: 7.4, faults: [] }],
        },
      });
    });
    expect(screen.getByTestId('joints').textContent).toContain('index_mcp_side');
    expect(screen.getByTestId('health').textContent).toContain('temp_c');
  });

  it('reconnects with backoff after the connection drops', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', FakeWebSocket);
    render(<Harness wsUrl="ws://127.0.0.1:9003" />);
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    act(() => {
      FakeWebSocket.instances[0].emitOpen();
    });

    act(() => {
      FakeWebSocket.instances[0].onclose?.();
    });
    expect(screen.getByTestId('status').textContent).toBe('connecting');

    await act(async () => {
      vi.runOnlyPendingTimers();
      await Promise.resolve();
    });

    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('does not auto-reconnect after an explicit disconnect', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', FakeWebSocket);
    render(<Harness wsUrl="ws://127.0.0.1:9003" />);
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    act(() => {
      FakeWebSocket.instances[0].emitOpen();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(screen.getByTestId('status').textContent).toBe('disconnected');

    await act(async () => {
      vi.advanceTimersByTime(10000);
      await Promise.resolve();
    });

    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

describe('useHandGateway ops', () => {
  it('sends scan with no payload and resolves hits/duplicates', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    fireEvent.click(screen.getByRole('button', { name: 'Scan' }));
    await act(async () => {
      ws.respondOk('scan', { hits: [{ servo_id: 0 }], duplicates: [] });
      await Promise.resolve();
    });
    expect(screen.getByTestId('result').textContent).toBe(
      'scan:ok:{"hits":[{"servo_id":0}],"duplicates":[]}'
    );
  });

  it('rejects when the gateway responds ok:false', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    fireEvent.click(screen.getByRole('button', { name: 'Jog' }));
    await act(async () => {
      ws.respondErr('jog', 'servo not enabled');
      await Promise.resolve();
    });
    expect(screen.getByTestId('result').textContent).toBe('jog:err:servo not enabled');
  });

  it('sends jog with servo_id and delta_rad', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    fireEvent.click(screen.getByRole('button', { name: 'Jog' }));
    const req = ws.sent.find((m) => m.op === 'jog');
    expect(req).toMatchObject({ servo_id: 3, delta_rad: 0.05 });
  });

  it('sends pos with a targets map', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    fireEvent.click(screen.getByRole('button', { name: 'Pos' }));
    const req = ws.sent.find((m) => m.op === 'pos');
    expect(req).toMatchObject({ targets: { index_mcp_side: 0.1 } });
  });

  it('sends reboot with servo_id', () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    fireEvent.click(screen.getByRole('button', { name: 'Reboot' }));
    const req = ws.sent.find((m) => m.op === 'reboot');
    expect(req).toMatchObject({ servo_id: 3 });
  });

  it('sends error_status with no payload', () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    fireEvent.click(screen.getByRole('button', { name: 'ErrorStatus' }));
    const req = ws.sent.find((m) => m.op === 'error_status');
    expect(req).toBeTruthy();
  });
});

describe('useHandGateway heartbeat', () => {
  it('starts a 500ms heartbeat once any servo is enabled', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();

    fireEvent.click(screen.getByRole('button', { name: 'EnableAll' }));
    await act(async () => {
      ws.respondOk('enable', {});
      await Promise.resolve();
    });

    const before = ws.sent.filter((m) => m.op === 'heartbeat').length;
    await act(async () => {
      vi.advanceTimersByTime(1600);
      await Promise.resolve();
    });
    const after = ws.sent.filter((m) => m.op === 'heartbeat').length;
    expect(after - before).toBeGreaterThanOrEqual(3);
  });

  it('stops the heartbeat once all servos are disabled', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();

    fireEvent.click(screen.getByRole('button', { name: 'EnableAll' }));
    await act(async () => {
      ws.respondOk('enable', {});
      await Promise.resolve();
    });

    fireEvent.click(screen.getByRole('button', { name: 'DisableAll' }));
    await act(async () => {
      ws.respondOk('disable', {});
      await Promise.resolve();
    });

    const before = ws.sent.filter((m) => m.op === 'heartbeat').length;
    await act(async () => {
      vi.advanceTimersByTime(2000);
      await Promise.resolve();
    });
    const after = ws.sent.filter((m) => m.op === 'heartbeat').length;
    expect(after).toBe(before);
  });

  it('does not start a second heartbeat when a specific servo is enabled on top of "all"', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();

    fireEvent.click(screen.getByRole('button', { name: 'EnableAll' }));
    await act(async () => {
      ws.respondOk('enable', {});
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole('button', { name: 'EnableOne' }));
    await act(async () => {
      ws.respondOk('enable', {});
      await Promise.resolve();
    });

    await act(async () => {
      vi.advanceTimersByTime(1000);
      await Promise.resolve();
    });
    // 1000ms / 500ms heartbeat = 2 ticks; a duplicate interval would double this.
    expect(ws.sent.filter((m) => m.op === 'heartbeat').length).toBeLessThanOrEqual(2);
  });
});

describe('useHandGateway watchdog_trip', () => {
  it('surfaces a watchdog_trip push and stops heartbeating', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();

    fireEvent.click(screen.getByRole('button', { name: 'EnableAll' }));
    await act(async () => {
      ws.respondOk('enable', {});
      await Promise.resolve();
    });

    act(() => {
      ws.emitMessage({ type: 'event', data: { event: 'watchdog_trip', reason: 'heartbeat timeout' } });
    });

    expect(screen.getByTestId('watchdog').textContent).toBe('tripped');

    const before = ws.sent.filter((m) => m.op === 'heartbeat').length;
    await act(async () => {
      vi.advanceTimersByTime(2000);
      await Promise.resolve();
    });
    expect(ws.sent.filter((m) => m.op === 'heartbeat').length).toBe(before);
  });
});
