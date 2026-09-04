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
  useControlContext: vi.fn(),
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

const { useConnectionContext, useControlContext, useRobotArmContext } = await import(
  '../hooks/useMotorStudioContext'
);
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

// Six joint rows, matching robots.yaml's joint1..joint6, as the shared session would supply.
function armRows() {
  return [1, 2, 3, 4, 5, 6].map((joint) => ({
    joint,
    key: `damiao:${joint}`,
    hit: { esc_id: joint, vendor: 'damiao' },
    control: { mode: 'pos_vel', target: 0 },
  }));
}

function useArm(armOverrides = {}, connected = true) {
  const armCtx = makeArm({ robotArmJointRows: armRows(), ...armOverrides });
  useRobotArmContext.mockReturnValue(armCtx);
  useConnectionContext.mockReturnValue({ connected });
  useControlContext.mockReturnValue({
    controlMotor: controlMotorMock,
    patchControl: patchControlMock,
  });
  return armCtx;
}

const controlMotorMock = vi.fn().mockResolvedValue(true);
const patchControlMock = vi.fn();

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
      setMechanicalZero: vi.fn().mockResolvedValue({ previous_offsets: { 0: 12, 1: -4 } }),
      restoreHomingOffsets: vi.fn().mockResolvedValue({}),
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

  it('sends every joint to zero from one press, through the shared arm session', async () => {
    useArm({}, true);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);

    fireEvent.click(screen.getByRole('button', { name: 'Go to zero pose' }));
    // Commands the pose rather than calling resetPoseRobotArm, so one path serves both
    // "go to zero" and "send this saved pose".
    await waitFor(() => expect(controlMotorMock).toHaveBeenCalledTimes(6));
    for (const call of controlMotorMock.mock.calls) {
      expect(call[1]).toBe('move');
      expect(call[2].target).toBeCloseTo(0);
    }
    // The hand half only goes out if robots.yaml says the joint map is confirmed.
    if (robotsConfig.hand.calibrated) {
      await waitFor(() => expect(hand.ops.pos).toHaveBeenCalledTimes(1));
      const sent = hand.ops.pos.mock.calls[0][0];
      expect(Object.keys(sent)).toHaveLength(16);
      for (const v of Object.values(sent)) expect(v).toBeCloseTo(0);
    }
  });

  it('does not touch the arm when its gateway is disconnected', async () => {
    useArm({}, false);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);

    fireEvent.click(screen.getByRole('button', { name: 'Go to zero pose' }));
    await waitFor(() => expect(screen.getByText(/skipped .*arm/i)).toBeTruthy());
    expect(controlMotorMock).not.toHaveBeenCalled();
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

  it('does not move the arm from a slider while live is off', () => {
    useArm({}, true);
    useHandGatewayContext.mockReturnValue(makeHand({ connected: true }));
    render(<CombinedPage />);

    fireEvent.change(screen.getByLabelText('joint1'), { target: { value: '0.3' } });
    expect(controlMotorMock).not.toHaveBeenCalled();
    expect(viewerJoints().joint1).toBeCloseTo(0.3);
  });

  it('streams slider changes to the arm once live is on', async () => {
    useArm({}, true);
    useHandGatewayContext.mockReturnValue(makeHand({ connected: true }));
    render(<CombinedPage />);

    fireEvent.click(screen.getByLabelText('live arm'));
    fireEvent.change(screen.getByLabelText('joint1'), { target: { value: '0.3' } });
    // Only the dragged joint is commanded. Sending the whole pose would fling the other
    // five to whatever their sliders happened to say.
    await waitFor(() => expect(controlMotorMock).toHaveBeenCalled());
    const moved = controlMotorMock.mock.calls.filter((c) => c[1] === 'move');
    expect(moved.every((c) => c[0].esc_id === 1)).toBe(true);
    expect(moved.at(-1)[2].target).toBeCloseTo(0.3);
  });

  it('sends the arm pose on demand while live is off', async () => {
    useArm({}, true);
    useHandGatewayContext.mockReturnValue(makeHand({ connected: true }));
    render(<CombinedPage />);

    fireEvent.change(screen.getByLabelText('joint2'), { target: { value: '-0.4' } });
    fireEvent.click(screen.getByRole('button', { name: /send arm pose/i }));
    await waitFor(() => expect(controlMotorMock).toHaveBeenCalledTimes(6));
    const j2 = controlMotorMock.mock.calls.find((c) => c[0].esc_id === 2);
    expect(j2[2].target).toBeCloseTo(-0.4);
  });

  it('streams the hand pose only when hand live is on', async () => {
    useArm({}, false);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);

    fireEvent.change(screen.getByLabelText('index_mcp_flex'), { target: { value: '0.2' } });
    expect(hand.ops.pos).not.toHaveBeenCalled();

    fireEvent.click(screen.getByLabelText('live hand'));
    fireEvent.change(screen.getByLabelText('index_mcp_flex'), { target: { value: '0.25' } });
    await waitFor(() => expect(hand.ops.pos).toHaveBeenCalled());
  });

  it('pauses mirroring while the hand is driven live, so the two do not fight', async () => {
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(
      makeHand({ connected: true, joints: [{ servo_id: 1, pos: 0.4 }] }),
    );
    render(<CombinedPage />);
    await waitFor(() => expect(viewerJoints().index_mcp_flex).toBeCloseTo(0.4));

    fireEvent.click(screen.getByLabelText('live hand'));
    fireEvent.change(screen.getByLabelText('index_mcp_flex'), { target: { value: '0.1' } });
    // Without the pause the next mirror tick would snap this straight back to 0.4.
    expect(viewerJoints().index_mcp_flex).toBeCloseTo(0.1);
  });

  it('cannot drive the arm live while its gateway is down', () => {
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(makeHand({ connected: true }));
    render(<CombinedPage />);
    expect(screen.getByLabelText('live arm').disabled).toBe(true);
  });

  it('shows the arm where it actually is, not at zero', async () => {
    // Sliders that read 0 while the arm is elsewhere make "Send arm pose" command a pose
    // nobody chose -- which is what made one slider appear to move all six.
    const rows = armRows();
    rows[0].hit.pos = 0.62;
    useArm({ robotArmJointRows: rows }, true);
    useHandGatewayContext.mockReturnValue(makeHand());
    render(<CombinedPage />);
    await waitFor(() => expect(viewerJoints().joint1).toBeCloseTo(0.62));
  });

  it('stops mirroring the arm while it is being driven live', async () => {
    const rows = armRows();
    rows[0].hit.pos = 0.62;
    useArm({ robotArmJointRows: rows }, true);
    useHandGatewayContext.mockReturnValue(makeHand());
    render(<CombinedPage />);
    await waitFor(() => expect(viewerJoints().joint1).toBeCloseTo(0.62));

    fireEvent.click(screen.getByLabelText('live arm'));
    fireEvent.change(screen.getByLabelText('joint1'), { target: { value: '0.1' } });
    expect(viewerJoints().joint1).toBeCloseTo(0.1);
  });

  it('will not re-zero the hand while torque is on', () => {
    // The servo locks its EEPROM when powered, so a write would land on only some joints.
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(
      makeHand({ connected: true, joints: [{ servo_id: 0, pos: 0, torque_enabled: true }] }),
    );
    render(<CombinedPage />);
    expect(screen.getByRole('button', { name: /set mechanical zero \(hand\)/i }).disabled).toBe(
      true,
    );
  });

  it('asks before re-zeroing the hand, and does nothing if declined', () => {
    useArm({}, false);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<CombinedPage />);

    fireEvent.click(screen.getByRole('button', { name: /set mechanical zero \(hand\)/i }));
    expect(confirmSpy).toHaveBeenCalled();
    expect(hand.ops.setMechanicalZero).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('re-zeroes the hand on confirmation and then offers an undo', async () => {
    useArm({}, false);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<CombinedPage />);

    expect(screen.queryByRole('button', { name: /undo re-zero/i })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /set mechanical zero \(hand\)/i }));
    await waitFor(() => expect(hand.ops.setMechanicalZero).toHaveBeenCalledWith({ confirm: true }));

    // Unlike the arm's one-way write to flash, this can be put back.
    const undo = await screen.findByRole('button', { name: /undo re-zero/i });
    fireEvent.click(undo);
    await waitFor(() =>
      expect(hand.ops.restoreHomingOffsets).toHaveBeenCalledWith({
        offsets: { 0: 12, 1: -4 },
        confirm: true,
      }),
    );
    confirmSpy.mockRestore();
  });

  it('live-drags only the touched hand joint, never the whole hand', async () => {
    // The gateway moves each joint at most max_step_rad per command, so a joint set earlier
    // is often still converging. Re-asserting all 16 every tick keeps walking those toward
    // their old targets while you are already dragging a different finger.
    useArm({}, false);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);

    fireEvent.click(screen.getByLabelText('live hand'));
    fireEvent.change(screen.getByLabelText('index_mcp_flex'), { target: { value: '0.5' } });
    await waitFor(() => expect(hand.ops.pos).toHaveBeenCalled());

    const sent = hand.ops.pos.mock.calls.at(-1)[0];
    expect(Object.keys(sent)).toEqual(['index_mcp_flex']);
  });

  it('still sends the whole hand for an explicit pose send', () => {
    useArm({}, false);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);

    fireEvent.click(screen.getByRole('button', { name: /send hand pose/i }));
    expect(Object.keys(hand.ops.pos.mock.calls.at(-1)[0])).toHaveLength(16);
  });

  it('drops out of live driving when the watchdog trips', async () => {
    // Torque is off after a trip, so the hand is back-drivable and may have flopped. Staying
    // in live mode would keep mirroring paused and let the next drag re-assert stale targets.
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(
      makeHand({ connected: true, watchdogTrip: { at: 1, gap_s: 12.4, timeout_s: 2 } }),
    );
    render(<CombinedPage />);
    await waitFor(() => expect(screen.getByLabelText('live hand').checked).toBe(false));
  });

  it('enables both sides from the whole-system toolbar', async () => {
    const armCtx = useArm({}, true);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);

    // The per-section Enable all buttons still exist; this is the first one, in the
    // whole-system toolbar.
    fireEvent.click(screen.getAllByRole('button', { name: 'Enable all' })[0]);
    await waitFor(() => expect(armCtx.enableAllRobotArm).toHaveBeenCalledTimes(1));
    expect(hand.ops.enable).toHaveBeenCalledTimes(1);
  });

  it('enables only the side whose gateway is up, and says which it skipped', async () => {
    const armCtx = useArm({}, false);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);

    fireEvent.click(screen.getAllByRole('button', { name: 'Enable all' })[0]);
    await waitFor(() => expect(hand.ops.enable).toHaveBeenCalled());
    expect(armCtx.enableAllRobotArm).not.toHaveBeenCalled();
    expect(screen.getByText(/skipped .*arm/i)).toBeTruthy();
  });

  it('lets live be switched back on after a trip, while the trip is still latched', async () => {
    // A trip stays latched until dismissed. Reacting to its presence rather than to a NEW
    // trip re-cleared handLive on every render, so the checkbox could be ticked but never
    // stayed on -- and live dragging silently did nothing.
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(
      makeHand({ connected: true, watchdogTrip: { at: 1, gap_s: 12.4, timeout_s: 2 } }),
    );
    render(<CombinedPage />);
    const live = screen.getByLabelText('live hand');
    await waitFor(() => expect(live.checked).toBe(false));

    fireEvent.click(live);
    expect(live.checked).toBe(true);
  });

  it('still drives the hand live after a trip is dismissed and torque is back', async () => {
    useArm({}, false);
    const hand = makeHand({ connected: true });
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);

    fireEvent.click(screen.getByLabelText('live hand'));
    fireEvent.change(screen.getByLabelText('index_mcp_flex'), { target: { value: '0.3' } });
    await waitFor(() => expect(hand.ops.pos).toHaveBeenCalled());
  });

  it('keeps a curl preset when telemetry arrives before you send it', async () => {
    // Mirroring rewrites all 16 hand targets from measured positions on every state push.
    // Without pausing it for an unsent edit, a preset is wiped within one poll and never
    // reaches the robot -- which looks exactly like "curl hand does nothing".
    useArm({}, false);
    const hand = makeHand({ connected: true, joints: [{ servo_id: 1, pos: 0.0 }] });
    useHandGatewayContext.mockReturnValue(hand);
    const { rerender } = render(<CombinedPage />);

    fireEvent.click(screen.getByRole('button', { name: /curl hand/i }));
    const curled = viewerJoints().index_mcp_flex;
    expect(curled).not.toBeCloseTo(0);

    // A telemetry push lands, reporting the hand still physically at rest.
    useHandGatewayContext.mockReturnValue(
      makeHand({ connected: true, joints: [{ servo_id: 1, pos: 0.0 }] }),
    );
    rerender(<CombinedPage />);
    expect(viewerJoints().index_mcp_flex).toBeCloseTo(curled);
  });

  it('says a hand pose is unsent, and can discard it back to measured', async () => {
    useArm({}, false);
    useHandGatewayContext.mockReturnValue(
      makeHand({ connected: true, joints: [{ servo_id: 1, pos: 0.25 }] }),
    );
    render(<CombinedPage />);
    await waitFor(() => expect(viewerJoints().index_mcp_flex).toBeCloseTo(0.25));

    fireEvent.click(screen.getByRole('button', { name: /curl hand/i }));
    expect(screen.getByText(/unsent hand pose/i)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /discard and re-sync from the hand/i }));
    await waitFor(() => expect(viewerJoints().index_mcp_flex).toBeCloseTo(0.25));
  });

  it('resumes mirroring once the pose has been sent', async () => {
    useArm({}, false);
    const hand = makeHand({ connected: true, joints: [{ servo_id: 1, pos: 0.25 }] });
    useHandGatewayContext.mockReturnValue(hand);
    render(<CombinedPage />);

    fireEvent.click(screen.getByRole('button', { name: /curl hand/i }));
    expect(screen.getByText(/unsent hand pose/i)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /send hand pose/i }));
    await waitFor(() => expect(hand.ops.pos).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText(/unsent hand pose/i)).toBeNull());
  });
});
