// @vitest-environment jsdom
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import robotsConfig from '../generated/robotsConfig.json';
import { buildCombinedModel } from '../lib/combinedModel';
import { zeroTargets } from '../lib/combinedModel';
import { PoseLibrary } from './PoseLibrary';
import { POSE_STORAGE_KEY } from '../lib/poseStore';

const MODEL = buildCombinedModel(robotsConfig);

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return {
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = String(v);
    },
    _data: data,
  };
}

function renderLib(overrides = {}) {
  const props = {
    model: MODEL,
    targets: zeroTargets(MODEL),
    onLoad: vi.fn(),
    onSend: vi.fn(),
    canSend: true,
    storage: memoryStorage(),
    ...overrides,
  };
  render(<PoseLibrary {...props} />);
  return props;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('PoseLibrary', () => {
  it('saves the current targets under a name', () => {
    const storage = memoryStorage();
    renderLib({ storage, targets: { ...zeroTargets(MODEL), joint1: 0.5 } });

    fireEvent.change(screen.getByLabelText('pose name'), { target: { value: 'ready' } });
    fireEvent.click(screen.getByRole('button', { name: /save current/i }));

    const saved = JSON.parse(storage._data[POSE_STORAGE_KEY]);
    expect(saved).toHaveLength(1);
    expect(saved[0].name).toBe('ready');
    expect(saved[0].joints.joint1).toBeCloseTo(0.5);
    expect(Object.keys(saved[0].joints)).toHaveLength(22);
  });

  it('refuses to save an unnamed pose', () => {
    const storage = memoryStorage();
    renderLib({ storage });
    fireEvent.click(screen.getByRole('button', { name: /save current/i }));
    expect(screen.getByText(/give the pose a name/i)).toBeTruthy();
    expect(storage._data[POSE_STORAGE_KEY]).toBeUndefined();
  });

  it('loads a pose into the sliders without sending it to the robot', () => {
    // Loading a pose to look at it must never move the machine on its own.
    const stored = [{ name: 'ready', joints: { joint1: 0.4 } }];
    const props = renderLib({ storage: memoryStorage({ [POSE_STORAGE_KEY]: JSON.stringify(stored) }) });

    fireEvent.click(screen.getByRole('button', { name: 'Load' }));
    expect(props.onLoad).toHaveBeenCalledTimes(1);
    expect(props.onSend).not.toHaveBeenCalled();
    expect(props.onLoad.mock.calls[0][0].joint1).toBeCloseTo(0.4);
  });

  it('sends only on the explicit send press', () => {
    const stored = [{ name: 'ready', joints: { joint1: 0.4 } }];
    const props = renderLib({ storage: memoryStorage({ [POSE_STORAGE_KEY]: JSON.stringify(stored) }) });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(props.onSend).toHaveBeenCalledTimes(1);
  });

  it('cannot send while no gateway is connected', () => {
    const stored = [{ name: 'ready', joints: { joint1: 0.4 } }];
    renderLib({
      canSend: false,
      storage: memoryStorage({ [POSE_STORAGE_KEY]: JSON.stringify(stored) }),
    });
    expect(screen.getByRole('button', { name: 'Send' }).disabled).toBe(true);
  });

  it('flags a stored pose that names joints which no longer exist', () => {
    const stored = [{ name: 'old', joints: { ring_base_rot: 0.3, joint1: 0 } }];
    renderLib({ storage: memoryStorage({ [POSE_STORAGE_KEY]: JSON.stringify(stored) }) });
    expect(screen.getByText(/1 unknown/)).toBeTruthy();
  });

  it('deletes a pose', () => {
    const storage = memoryStorage({
      [POSE_STORAGE_KEY]: JSON.stringify([{ name: 'ready', joints: {} }]),
    });
    renderLib({ storage });
    fireEvent.click(screen.getByRole('button', { name: 'delete ready' }));
    expect(JSON.parse(storage._data[POSE_STORAGE_KEY])).toEqual([]);
  });

  it('says so when the browser refuses to persist', () => {
    renderLib({
      storage: {
        getItem: () => null,
        setItem: () => {
          throw new Error('QuotaExceededError');
        },
      },
    });
    fireEvent.change(screen.getByLabelText('pose name'), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: /save current/i }));
    expect(screen.getByText(/blocking site data/i)).toBeTruthy();
  });
});
