// @vitest-environment jsdom
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import robotsConfig from '../generated/robotsConfig.json';
import { useHandGateway } from '../hooks/useHandGateway';

vi.mock('../hooks/useHandGateway', () => ({
  useHandGateway: vi.fn(),
}));

// Imported after the mock so the component picks up the mocked hook.
const { LeapHandPage } = await import('./LeapHandPage');

function makeHand(overrides = {}) {
  return {
    status: 'disconnected',
    connected: false,
    lastError: '',
    joints: [],
    health: [],
    lastStateTs: null,
    watchdogTrip: null,
    clearWatchdogTrip: vi.fn(),
    connect: vi.fn(),
    disconnect: vi.fn(),
    wsUrl: 'ws://127.0.0.1:9003',
    ops: {
      scan: vi.fn().mockResolvedValue({ hits: [], duplicates: [] }),
      enable: vi.fn().mockResolvedValue({}),
      disable: vi.fn().mockResolvedValue({}),
      jog: vi.fn().mockResolvedValue({}),
      pos: vi.fn().mockResolvedValue({}),
      errorStatus: vi.fn().mockResolvedValue({ faults: {} }),
      reboot: vi.fn().mockResolvedValue({}),
    },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const PROVISIONAL = !robotsConfig.hand.calibrated;

describe('LeapHandPage', () => {
  it('shows the provisional banner only while the map is unconfirmed, and 16 joint cards', () => {
    useHandGateway.mockReturnValue(makeHand());
    render(<LeapHandPage />);

    // Tied to robots.yaml rather than to a fixed expectation: the banner is a statement
    // about the config, and confirming the hand on the bench should not fail a UI test.
    expect(Boolean(screen.queryByText(/Provisional joint map/))).toBe(PROVISIONAL);
    expect(screen.getByText('index')).toBeTruthy();
    expect(screen.getByText('thumb')).toBeTruthy();
    expect(screen.getByText('middle')).toBeTruthy();
    expect(screen.getByText('ring')).toBeTruthy();
    expect(screen.getByText('index_mcp_side')).toBeTruthy();
    expect(screen.getByText('thumb_dip')).toBeTruthy();
    expect(screen.getAllByText(/servo \d+/)).toHaveLength(16);
  });

  it('disables the connect button once connected and enables disconnect', () => {
    useHandGateway.mockReturnValue(makeHand({ status: 'connected', connected: true }));
    render(<LeapHandPage />);

    expect(screen.getByRole('button', { name: 'Connect' }).disabled).toBe(true);
    expect(screen.getByRole('button', { name: 'Disconnect' }).disabled).toBe(false);
  });

  it('gates pose presets on the joint map being confirmed', () => {
    useHandGateway.mockReturnValue(makeHand({ status: 'connected', connected: true }));
    render(<LeapHandPage />);

    // Presets drive all 16 servos at once, so they are offered only when robots.yaml says
    // the servo->joint map is confirmed. The gateway refuses `pos` otherwise regardless.
    expect(screen.getByRole('button', { name: 'Open Hand' }).disabled).toBe(PROVISIONAL);
    expect(screen.getByRole('button', { name: 'Curl Hand' }).disabled).toBe(PROVISIONAL);
  });

  it('shows live position, temperature severity, and fault chips for a joint', () => {
    const hand = makeHand({
      status: 'connected',
      connected: true,
      joints: [{ servo_id: 2, name: 'index_pip', pos: 0.5, vel: 0.1, current_ma: 55 }],
      health: [{ servo_id: 2, temp_c: 68, voltage_v: 7.4, faults: ['overtemp_motor'] }],
    });
    useHandGateway.mockReturnValue(hand);
    render(<LeapHandPage />);

    expect(screen.getByText('28.6°')).toBeTruthy();
    expect(screen.getByText('68.0°C')).toBeTruthy();
    expect(screen.getByText('68.0°C').className).toContain('danger');
    expect(screen.getByText('Motor Overtemp')).toBeTruthy();
  });

  it('calls jog with the configured step when a joint +/- button is clicked', () => {
    const hand = makeHand({ status: 'connected', connected: true });
    useHandGateway.mockReturnValue(hand);
    render(<LeapHandPage />);

    const jogButtons = screen.getAllByTitle(/jog \+/)[0];
    fireEvent.click(jogButtons);

    expect(hand.ops.jog).toHaveBeenCalledWith(0, 0.15);
  });

  it('shows a duplicate-ID warning banner after a scan finds a duplicate servo', async () => {
    const hand = makeHand({
      status: 'connected',
      connected: true,
      ops: {
        ...makeHand().ops,
        scan: vi.fn().mockResolvedValue({ hits: [{ servo_id: 3 }, { servo_id: 3 }], duplicates: [3] }),
      },
    });
    useHandGateway.mockReturnValue(hand);
    render(<LeapHandPage />);

    fireEvent.click(screen.getByRole('button', { name: 'Scan Bus' }));

    await waitFor(() => {
      expect(screen.getByText(/Duplicate servo IDs on the bus: 3/)).toBeTruthy();
    });
  });

  it('shows the watchdog-trip banner when the hook reports one tripped', () => {
    useHandGateway.mockReturnValue(
      makeHand({ watchdogTrip: { at: Date.now(), detail: { reason: 'heartbeat timeout' } } })
    );
    render(<LeapHandPage />);

    expect(screen.getByText(/Watchdog tripped on the hand bus/)).toBeTruthy();
  });

  it('calls connect/disconnect from the connection card', () => {
    const hand = makeHand();
    useHandGateway.mockReturnValue(hand);
    render(<LeapHandPage />);

    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect(hand.connect).toHaveBeenCalled();
  });
});
