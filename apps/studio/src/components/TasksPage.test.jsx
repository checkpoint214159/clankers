// @vitest-environment jsdom
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useTaskRunner } from '../hooks/useTaskRunner';

vi.mock('../hooks/useTaskRunner', () => ({
  useTaskRunner: vi.fn(),
}));

const { TasksPage } = await import('./TasksPage');

function makeTask(overrides = {}) {
  return {
    status: 'disconnected',
    connected: false,
    lastError: '',
    poses: [],
    skills: [],
    positions: null,
    lastStateTs: null,
    running: null,
    lastEvent: null,
    connect: vi.fn(),
    disconnect: vi.fn(),
    refreshPoseList: vi.fn(),
    wsUrl: 'ws://127.0.0.1:9010',
    ops: {
      poseGo: vi.fn().mockResolvedValue({}),
      skillRun: vi.fn().mockResolvedValue({}),
      stop: vi.fn().mockResolvedValue({}),
    },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('TasksPage', () => {
  it('renders pose and skill cards from the task runner', () => {
    useTaskRunner.mockReturnValue(
      makeTask({
        status: 'connected',
        connected: true,
        poses: [{ name: 'home' }, { name: 'ready' }],
        skills: [{ name: 'wave' }],
      })
    );
    render(<TasksPage />);

    expect(screen.getByText('home')).toBeTruthy();
    expect(screen.getByText('ready')).toBeTruthy();
    expect(screen.getByText('wave')).toBeTruthy();
  });

  it('calls pose_go when a pose card is clicked', () => {
    const task = makeTask({
      status: 'connected',
      connected: true,
      poses: [{ name: 'home' }],
    });
    useTaskRunner.mockReturnValue(task);
    render(<TasksPage />);

    fireEvent.click(screen.getByText('home'));
    expect(task.ops.poseGo).toHaveBeenCalledWith('home');
  });

  it('calls skill_run when a skill card is clicked', () => {
    const task = makeTask({
      status: 'connected',
      connected: true,
      skills: [{ name: 'wave' }],
    });
    useTaskRunner.mockReturnValue(task);
    render(<TasksPage />);

    fireEvent.click(screen.getByText('wave'));
    expect(task.ops.skillRun).toHaveBeenCalledWith('wave');
  });

  it('disables the Stop button when nothing is running', () => {
    useTaskRunner.mockReturnValue(makeTask({ status: 'connected', connected: true }));
    render(<TasksPage />);

    expect(screen.getByRole('button', { name: '■ Stop' }).disabled).toBe(true);
  });

  it('enables Stop and shows a running badge while a skill runs', () => {
    useTaskRunner.mockReturnValue(
      makeTask({
        status: 'connected',
        connected: true,
        skills: [{ name: 'wave' }],
        running: { kind: 'skill', name: 'wave' },
      })
    );
    render(<TasksPage />);

    expect(screen.getByRole('button', { name: '■ Stop' }).disabled).toBe(false);
    expect(screen.getByText('Running skill: wave')).toBeTruthy();
  });

  it('calls stop when the Stop button is clicked', () => {
    const task = makeTask({
      status: 'connected',
      connected: true,
      running: { kind: 'pose', name: 'home' },
    });
    useTaskRunner.mockReturnValue(task);
    render(<TasksPage />);

    fireEvent.click(screen.getByRole('button', { name: '■ Stop' }));
    expect(task.ops.stop).toHaveBeenCalled();
  });

  it('renders the live joint readout from state positions', () => {
    useTaskRunner.mockReturnValue(
      makeTask({ status: 'connected', connected: true, positions: { joint1: 0.5 } })
    );
    render(<TasksPage />);

    expect(screen.getByText(/joint1: 0.500 rad/)).toBeTruthy();
  });

  it('disables task cards while disconnected', () => {
    useTaskRunner.mockReturnValue(makeTask({ poses: [{ name: 'home' }] }));
    render(<TasksPage />);

    expect(screen.getByText('home').closest('button').disabled).toBe(true);
  });
});
