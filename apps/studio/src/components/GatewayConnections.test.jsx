// @vitest-environment jsdom
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../hooks/useHandGatewayContext', () => ({ useHandGatewayContext: vi.fn() }));
vi.mock('../hooks/useMotorStudioContext', () => ({
  useConnectionContext: vi.fn(),
  useRobotArmContext: vi.fn(),
}));

const { useHandGatewayContext } = await import('../hooks/useHandGatewayContext');
const { useConnectionContext, useRobotArmContext } = await import('../hooks/useMotorStudioContext');
const { GatewayConnections } = await import('./GatewayConnections');

function setup({ armConnected = false, handStatus = 'disconnected', hand: handOverrides = {} } = {}) {
  const conn = {
    wsUrl: 'ws://127.0.0.1:9002',
    setWsUrl: vi.fn(),
    connText: armConnected ? 'connected' : 'disconnected',
    connected: armConnected,
    connectWs: vi.fn(),
    disconnectWs: vi.fn(),
  };
  const arm = { armBulkBusy: false, ensureRobotArmCards: vi.fn() };
  const hand = {
    status: handStatus,
    connected: handStatus === 'connected',
    wsUrl: 'ws://127.0.0.1:9003',
    lastError: '',
    watchdogTrip: null,
    clearWatchdogTrip: vi.fn(),
    connect: vi.fn(),
    disconnect: vi.fn(),
    setWsUrl: vi.fn(),
    ...handOverrides,
  };
  useConnectionContext.mockReturnValue(conn);
  useRobotArmContext.mockReturnValue(arm);
  useHandGatewayContext.mockReturnValue(hand);
  return { conn, arm, hand };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('GatewayConnections', () => {
  it('shows both gateways with their ports', () => {
    setup();
    expect(screen.queryByText(/9002/)).toBeNull();
    render(<GatewayConnections />);
    expect(screen.getByText(/:9002/)).toBeTruthy();
    expect(screen.getByText(/:9003/)).toBeTruthy();
  });

  it('connects both from one press', async () => {
    const { conn, hand } = setup();
    render(<GatewayConnections />);
    fireEvent.click(screen.getByRole('button', { name: 'Connect both' }));
    await waitFor(() => expect(conn.connectWs).toHaveBeenCalledTimes(1));
    expect(hand.connect).toHaveBeenCalledTimes(1);
  });

  it('creates the arm joint cards when connecting the arm', async () => {
    // The Robot Arm page used to do this on mount; with one page it has to happen on
    // connect or the arm controls come up with nothing to drive.
    const { arm, conn } = setup();
    render(<GatewayConnections />);
    fireEvent.click(screen.getAllByRole('button', { name: 'Connect' })[0]);
    await waitFor(() => expect(arm.ensureRobotArmCards).toHaveBeenCalled());
    expect(conn.connectWs).toHaveBeenCalled();
  });

  it('does not reconnect a gateway that is already up', async () => {
    const { conn, hand } = setup({ armConnected: true });
    render(<GatewayConnections />);
    fireEvent.click(screen.getByRole('button', { name: 'Connect both' }));
    await waitFor(() => expect(hand.connect).toHaveBeenCalled());
    expect(conn.connectWs).not.toHaveBeenCalled();
  });

  it('disables connect-both only when both are already connected', () => {
    setup({ armConnected: true, handStatus: 'connected' });
    render(<GatewayConnections />);
    expect(screen.getByRole('button', { name: 'Connect both' }).disabled).toBe(true);
  });

  it('locks the url while connected so it cannot drift from the live socket', () => {
    setup({ armConnected: true });
    render(<GatewayConnections />);
    expect(screen.getByLabelText('Arm url').disabled).toBe(true);
  });

  it('surfaces a hand watchdog trip and lets it be dismissed', async () => {
    const { hand } = setup({
      handStatus: 'connected',
      hand: { watchdogTrip: { at: 1, detail: 'missed heartbeat' } },
    });
    render(<GatewayConnections />);
    expect(screen.getByText(/watchdog tripped/i)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    await waitFor(() => expect(hand.clearWatchdogTrip).toHaveBeenCalled());
  });

  it('reports a hand gateway error', () => {
    setup({ hand: { lastError: 'connection refused' } });
    render(<GatewayConnections />);
    expect(screen.getByText(/connection refused/)).toBeTruthy();
  });

  it('lets the hand gateway be repointed at the controller machine', () => {
    // Brain and controller are different machines (ADR-0005), so 127.0.0.1 is only right
    // when they are co-located. This input used to be inert, which made a remote gateway
    // unreachable from the studio entirely.
    const { hand } = setup();
    render(<GatewayConnections />);

    const input = screen.getByLabelText('Hand url');
    fireEvent.change(input, { target: { value: 'ws://192.168.2.2:9003' } });
    expect(hand.setWsUrl).toHaveBeenCalledWith('ws://192.168.2.2:9003');
  });
});
