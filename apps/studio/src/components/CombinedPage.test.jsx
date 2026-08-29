// @vitest-environment jsdom
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import robotsConfig from '../generated/robotsConfig.json';
import { useHandGatewayContext } from '../hooks/useHandGatewayContext';

vi.mock('../hooks/useHandGatewayContext', () => ({ useHandGatewayContext: vi.fn() }));
// The arm is driven through the shared studio session rather than its own gateway hook.
vi.mock('../hooks/useMotorStudioContext', () => ({
  useRobotArmContext: vi.fn(),
  useConnectionContext: vi.fn(),
}));
// The viewer pulls in three.js + WebGL, which jsdom has no business running; the page's
// contract with it is just "gets a joint map and the clankers profile".
// The shell embeds these; they have their own tests and drag in three.js / the whole arm
// stack, so here they stand in as markers that the shell mounted them.
vi.mock('./GatewayConnections', () => ({ GatewayConnections: () => <div data-testid="gateways" /> }));
vi.mock('./RobotArmPage', () => ({
  RobotArmPage: ({ showViewer }) => <div data-testid="armPage" data-viewer={String(showViewer)} />,
}));
vi.mock('./LeapHandPage', () => ({ LeapHandPage: () => <div data-testid="handPage" /> }));
vi.mock('./ArmUrdfViewer', () => ({
  ArmUrdfViewer: ({ jointTargets, profile }) => (
    <div
      data-testid="viewer"
      data-profile={profile}
      data-joints={JSON.stringify(jointTargets)}
    />
  ),
}));

const { useConnectionContext, useRobotArmContext } = await import('../hooks/useMotorStudioContext');
const { CombinedPage } = await import('./CombinedPage');

function makeArm(overrides = {}) {
  return {
    armBulkBusy: false,
    enableAllRobotArm: vi.fn().mockResolvedValue(true),
    disableAllRobotArm: vi.fn().mockResolvedValue(true),
    resetPoseRobotArm: vi.fn().mockResolvedValue(true),
    ...overrides,
  };
}

function useArm(armOverrides = {}, connected = true) {
  const armCtx = makeArm(armOverrides);
  useRobotArmContext.mockReturnValue(armCtx);
  useConnectionContext.mockReturnValue({ connected });
  return armCtx;
}

function makeHand(overrides = {}) {
  return {
    status: 'disconnected',
    connected: false,
    lastError: '',
    joints: [],
    health: [],
    watchdogTrip: null,
    clearWatchdogTrip: vi.fn(),
    connect: vi.fn(),
    disconnect: vi.fn(),
    wsUrl: 'ws://127.0.0.1:9003',
    ops: {
      scan: vi.fn().mockResolvedValue({}),
      enable: vi.fn().mockResolvedValue({}),
      disable: vi.fn().mockResolvedValue({}),
      jog: vi.fn().mockResolvedValue({}),
      pos: vi.fn().mockResolvedValue({}),
      errorStatus: vi.fn().mockResolvedValue({}),
      reboot: vi.fn().mockResolvedValue({}),
    },
    ...overrides,
  };
}

const viewerJoints = () => JSON.parse(screen.getByTestId('viewer').dataset.joints);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('CombinedPage', () => {
  it('drives the combined profile with every arm and hand joint', () => {
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(makeHand());
    render(<CombinedPage />);
    expect(screen.getByTestId('viewer').dataset.profile).toBe('clankers');
    const joints = viewerJoints();
    expect(Object.keys(joints)).toHaveLength(22); // 6 arm + 16 hand
    expect(joints).toHaveProperty('joint1');
    expect(joints).toHaveProperty('index_mcp_flex');
  });

  it('warns that the adapter mount is provisional', () => {
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(makeHand());
    render(<CombinedPage />);
    expect(screen.getByText(/provisional/i)).toBeTruthy();
  });

  it('gates whole-hand pos on the joint map being confirmed', () => {
    // Mirrors the gateway's own rule: it refuses `pos` unless robots.yaml says the
    // servo->joint map is confirmed, so the UI must not offer a command that gets refused.
    // Tied to the config rather than a fixed value — confirming the hand on the bench
    // should flip this button, not fail this test.
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(makeHand({ connected: true }));
    render(<CombinedPage />);
    const btn = screen.getByRole('button', { name: /send hand pose/i });
    expect(btn.disabled).toBe(!robotsConfig.hand.calibrated);
  });

  it('jogs a single joint through the gateway, which is the bring-up path', async () => {
    const hand = makeHand({ connected: true });
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);
    fireEvent.click(screen.getByLabelText('jog index_mcp_flex positive'));
    await waitFor(() => expect(hand.ops.jog).toHaveBeenCalled());
    const [servoId, delta] = hand.ops.jog.mock.calls[0];
    expect(servoId).toBe(1);
    expect(delta).toBeCloseTo(robotsConfig.hand.safety.max_step_rad);
  });

  it('mirrors live gateway positions into the model', async () => {
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(
      makeHand({ connected: true, joints: [{ servo_id: 1, pos: 0.4 }] }),
    );
    render(<CombinedPage />);
    await waitFor(() => expect(viewerJoints().index_mcp_flex).toBeCloseTo(0.4));
  });

  it('stops mirroring when the operator turns it off', async () => {
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(
      makeHand({ connected: true, joints: [{ servo_id: 1, pos: 0.4 }] }),
    );
    render(<CombinedPage />);
    await waitFor(() => expect(viewerJoints().index_mcp_flex).toBeCloseTo(0.4));
    fireEvent.click(screen.getByLabelText('mirror live positions'));
    fireEvent.change(screen.getByLabelText('index_mcp_flex'), { target: { value: '0.1' } });
    expect(viewerJoints().index_mcp_flex).toBeCloseTo(0.1);
  });

  it('poses the model from an arm slider', () => {
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(makeHand());
    render(<CombinedPage />);
    fireEvent.change(screen.getByLabelText('joint1'), { target: { value: '0.75' } });
    expect(viewerJoints().joint1).toBeCloseTo(0.75);
  });

  it('clamps an arm slider to its robots.yaml limit', () => {
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(makeHand());
    render(<CombinedPage />);
    const j1 = robotsConfig.arm.joints.find((j) => j.name === 'joint1');
    fireEvent.change(screen.getByLabelText('joint1'), { target: { value: '99' } });
    expect(viewerJoints().joint1).toBeCloseTo(j1.limit.max);
  });

  it('curls the hand without touching the arm', () => {
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(makeHand());
    render(<CombinedPage />);
    fireEvent.change(screen.getByLabelText('joint1'), { target: { value: '0.5' } });
    fireEvent.click(screen.getByRole('button', { name: /curl hand/i }));
    const joints = viewerJoints();
    expect(joints.joint1).toBeCloseTo(0.5);
    expect(joints.index_mcp_flex).not.toBe(0);
  });

  it('resets both halves from one press, through the shared arm session', async () => {
    const armCtx = useArm({}, true);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);

    fireEvent.click(screen.getByRole('button', { name: 'Go to zero pose' }));
    await waitFor(() => expect(armCtx.resetPoseRobotArm).toHaveBeenCalledTimes(1));
    // The hand half only goes out if robots.yaml says the joint map is confirmed.
    if (robotsConfig.hand.calibrated) {
      await waitFor(() => expect(hand.ops.pos).toHaveBeenCalledTimes(1));
      const sent = hand.ops.pos.mock.calls[0][0];
      expect(Object.keys(sent)).toHaveLength(16);
      for (const v of Object.values(sent)) expect(v).toBeCloseTo(0);
    }
  });

  it('does not touch the arm when its gateway is disconnected', async () => {
    const armCtx = useArm({}, false);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);

    fireEvent.click(screen.getByRole('button', { name: 'Go to zero pose' }));
    await waitFor(() => expect(screen.getByText(/skipped .*arm/i)).toBeTruthy());
    expect(armCtx.resetPoseRobotArm).not.toHaveBeenCalled();
  });

  it('offers no reset at all when neither side is ready', () => {
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(makeHand({ connected: false }));
    render(<CombinedPage />);
    expect(screen.getByRole('button', { name: 'Go to zero pose' }).disabled).toBe(true);
  });

  it('drives arm enable/disable through the shared session', async () => {
    const armCtx = useArm({}, true);
    useHandGatewayContext.mockReturnValue(makeHand());
    render(<CombinedPage />);

    const armButtons = screen.getAllByRole('button', { name: /^enable all$/i });
    fireEvent.click(armButtons[0]);
    await waitFor(() => expect(armCtx.enableAllRobotArm).toHaveBeenCalled());
  });

  it('locks arm controls while a bulk arm op is already running', () => {
    useArm({ armBulkBusy: true }, true);
    useHandGatewayContext.mockReturnValue(makeHand());
    render(<CombinedPage />);
    expect(screen.getAllByRole('button', { name: 'Enable all' })[0].disabled).toBe(true);
  });

  it('never offers two differently-meaning buttons called "zero all"', () => {
    // The arm section's "Set Mechanical Zero" rewrites the encoder reference and persists
    // to motor flash; this page's zero only commands the zero pose. They were once both
    // called "Zero all", one capital letter apart, on the same screen.
    useArm({}, true);
    useHandGatewayContext.mockReturnValue(makeHand({ connected: true }));
    render(<CombinedPage />);

    expect(screen.getByRole('button', { name: 'Go to zero pose' })).toBeTruthy();
    const ambiguous = screen
      .getAllByRole('button')
      .filter((b) => /^\s*zero all\s*$/i.test(b.textContent || ''));
    expect(ambiguous).toEqual([]);
  });

  it('mounts the gateway panel and both detail sections, arm viewer suppressed', () => {
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(makeHand());
    render(<CombinedPage />);
    expect(screen.getByTestId('gateways')).toBeTruthy();

    // Both detail sections start collapsed so the page opens on the whole-system controls
    // rather than a wall of motor tooling.
    expect(screen.queryByTestId('armPage')).toBeNull();
    expect(screen.queryByTestId('handPage')).toBeNull();

    // No i18n provider in this test, so CollapsibleSection's label is the raw key.
    for (const btn of screen.getAllByRole('button', { name: /expand/i })) fireEvent.click(btn);
    // One 3D view of the robot, not two: the shell already shows the combined model.
    expect(screen.getByTestId('armPage').dataset.viewer).toBe('false');
    expect(screen.getByTestId('handPage')).toBeTruthy();
  });

  it('enables hand torque through the gateway', async () => {
    useArm({}, false);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);

    const buttons = screen.getAllByRole('button', { name: /^enable all$/i });
    fireEvent.click(buttons[buttons.length - 1]);
    await waitFor(() => expect(hand.ops.enable).toHaveBeenCalled());
  });
});
