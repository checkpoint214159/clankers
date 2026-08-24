// @vitest-environment jsdom
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useTaskRunner } from './useTaskRunner';

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

  respondOk(op, data = {}) {
    const req = [...this.sent].reverse().find((m) => !op || m.op === op);
    if (!req) throw new Error(`no sent request for op=${op}`);
    this.emitMessage({ ok: true, op: req.op, req_id: req.req_id, data });
  }
}

FakeWebSocket.instances = [];

function Harness({ wsUrl }) {
  const task = useTaskRunner({ wsUrl });
  return (
    <>
      <div data-testid="status">{task.status}</div>
      <div data-testid="poses">{JSON.stringify(task.poses)}</div>
      <div data-testid="skills">{JSON.stringify(task.skills)}</div>
      <div data-testid="running">{task.running ? `${task.running.kind}:${task.running.name}` : 'none'}</div>
      <div data-testid="positions">{JSON.stringify(task.positions)}</div>
      <button onClick={task.connect}>Connect</button>
      <button onClick={() => task.ops.poseGo('home').catch(() => {})}>PoseGo</button>
      <button onClick={() => task.ops.skillRun('wave').catch(() => {})}>SkillRun</button>
      <button onClick={() => task.ops.stop().catch(() => {})}>Stop</button>
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

function connectAndOpen(url = 'ws://127.0.0.1:9010') {
  render(<Harness wsUrl={url} />);
  fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
  act(() => {
    FakeWebSocket.instances[0].emitOpen();
  });
  return FakeWebSocket.instances[0];
}

describe('useTaskRunner', () => {
  it('connects and requests the pose/skill list on open', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    expect(screen.getByTestId('status').textContent).toBe('connected');
    expect(ws.sent.some((m) => m.op === 'pose_list')).toBe(true);

    await act(async () => {
      ws.respondOk('pose_list', { poses: [{ name: 'home' }], skills: [{ name: 'wave' }] });
      await Promise.resolve();
    });

    expect(screen.getByTestId('poses').textContent).toContain('home');
    expect(screen.getByTestId('skills').textContent).toContain('wave');
  });

  it('sends pose_go with the pose name and tracks running state', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    fireEvent.click(screen.getByRole('button', { name: 'PoseGo' }));

    expect(screen.getByTestId('running').textContent).toBe('pose:home');
    const req = ws.sent.find((m) => m.op === 'pose_go');
    expect(req).toMatchObject({ name: 'home' });
  });

  it('sends skill_run with the skill name', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    fireEvent.click(screen.getByRole('button', { name: 'SkillRun' }));

    expect(screen.getByTestId('running').textContent).toBe('skill:wave');
    const req = ws.sent.find((m) => m.op === 'skill_run');
    expect(req).toMatchObject({ name: 'wave' });
  });

  it('clears running state on a task "done" push', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    fireEvent.click(screen.getByRole('button', { name: 'SkillRun' }));
    expect(screen.getByTestId('running').textContent).toBe('skill:wave');

    act(() => {
      ws.emitMessage({ type: 'task', data: { event: 'done', name: 'wave' } });
    });

    expect(screen.getByTestId('running').textContent).toBe('none');
  });

  it('clears running state when stop is called', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    fireEvent.click(screen.getByRole('button', { name: 'SkillRun' }));
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));

    await act(async () => {
      ws.respondOk('stop', {});
      await Promise.resolve();
    });

    expect(screen.getByTestId('running').textContent).toBe('none');
    expect(ws.sent.some((m) => m.op === 'stop')).toBe(true);
  });

  it('updates positions from state pushes', () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = connectAndOpen();
    act(() => {
      ws.emitMessage({ type: 'state', data: { ts: 1, positions: { joint1: 0.2 } } });
    });
    expect(screen.getByTestId('positions').textContent).toBe('{"joint1":0.2}');
  });
});
