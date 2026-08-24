import React from 'react';
import robotsConfig from '../generated/robotsConfig.json';
import { useTaskRunner } from '../hooks/useTaskRunner';
import '../styles/leap-hand.css';

function statusLabel(status) {
  if (status === 'connected') return 'Connected';
  if (status === 'connecting') return 'Connecting…';
  return 'Disconnected';
}

function TaskCard({ kind, name, running, disabled, onRun }) {
  const isRunning = running?.kind === kind && running?.name === name;
  return (
    <button
      className={`taskCard ${isRunning ? 'running' : ''} ${disabled ? 'disabled' : ''}`}
      disabled={disabled}
      onClick={onRun}
    >
      <div className="taskCardKind">{kind}</div>
      <div className="taskCardName">{name}</div>
      {isRunning && <div className="tip">running…</div>}
    </button>
  );
}

export function TasksPage() {
  const task = useTaskRunner();
  const [log, setLog] = React.useState([]);

  const pushLog = React.useCallback((msg, level = 'info') => {
    setLog((prev) => [...prev, { t: new Date().toLocaleTimeString(), msg, level }].slice(-200));
  }, []);

  React.useEffect(() => {
    if (!task.lastEvent) return;
    pushLog(`${task.lastEvent.event}: ${task.lastEvent.name ?? ''}`.trim());
  }, [task.lastEvent, pushLog]);

  const runPose = (name) => {
    task.ops.poseGo(name).catch((e) => pushLog(`pose_go ${name} failed: ${e.message || e}`, 'err'));
  };

  const runSkill = (name) => {
    task.ops.skillRun(name).catch((e) => pushLog(`skill_run ${name} failed: ${e.message || e}`, 'err'));
  };

  const handleStop = () => {
    task.ops.stop().catch((e) => pushLog(`stop failed: ${e.message || e}`, 'err'));
  };

  const busy = task.running != null;

  return (
    <section className="card glass">
      <div className="sectionTitle">
        <h2>Tasks</h2>
        <span className="tip">Task runner :{robotsConfig?.ports?.task_runner_ws ?? 9010}</span>
      </div>

      <div className="handConnCard">
        <span className={`handStatusDot ${task.status}`} aria-hidden="true" />
        <span>{statusLabel(task.status)}</span>
        <span className="handConnUrl">{task.wsUrl}</span>
        <div className="row compactToolbar">
          <button className="primary" disabled={task.status !== 'disconnected'} onClick={task.connect}>
            Connect
          </button>
          <button disabled={task.status === 'disconnected'} onClick={task.disconnect}>
            Disconnect
          </button>
          <button className="ghostBtn" disabled={!task.connected} onClick={task.refreshPoseList}>
            Refresh List
          </button>
        </div>
      </div>

      <div className="taskStopBar">
        <button
          className="dangerBtn taskStopBtn"
          disabled={!task.connected || !busy}
          onClick={handleStop}
        >
          ■ Stop
        </button>
        {task.running && (
          <span className="taskRunningBadge">
            Running {task.running.kind}: {task.running.name}
          </span>
        )}
      </div>

      <div className="sectionTitle">
        <h2 style={{ fontSize: 14 }}>Poses</h2>
      </div>
      <div className="taskGrid">
        {task.poses.length === 0 && <span className="tip">(no poses loaded)</span>}
        {task.poses.map((p) => (
          <TaskCard
            key={p.name}
            kind="pose"
            name={p.name}
            running={task.running}
            disabled={!task.connected}
            onRun={() => runPose(p.name)}
          />
        ))}
      </div>

      <div className="sectionTitle">
        <h2 style={{ fontSize: 14 }}>Skills</h2>
      </div>
      <div className="taskGrid">
        {task.skills.length === 0 && <span className="tip">(no skills loaded)</span>}
        {task.skills.map((s) => (
          <TaskCard
            key={s.name}
            kind="skill"
            name={s.name}
            running={task.running}
            disabled={!task.connected}
            onRun={() => runSkill(s.name)}
          />
        ))}
      </div>

      <div className="sectionTitle">
        <h2 style={{ fontSize: 14 }}>Live Joint Readout</h2>
      </div>
      <div className="box logs">
        {task.positions ? (
          Object.entries(task.positions).map(([name, rad]) => (
            <div key={name}>
              {name}: {Number(rad).toFixed(3)} rad
            </div>
          ))
        ) : (
          <span className="tip">(no state yet)</span>
        )}
      </div>

      <div className="box logs" style={{ marginTop: 12 }}>
        {log.length === 0 ? (
          <span className="tip">(no activity yet)</span>
        ) : (
          log.map((l, i) => (
            <div key={i} className={l.level === 'err' ? 'errText' : undefined}>
              [{l.t}] {l.msg}
            </div>
          ))
        )}
      </div>
    </section>
  );
}
