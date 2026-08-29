import React from 'react';
import {
  poseFromTargets,
  poseToTargets,
  readPoses,
  removePose,
  unknownJoints,
  upsertPose,
  writePoses,
} from '../lib/poseStore';

/**
 * Named whole-system poses (arm + hand together), kept in this browser.
 *
 * `onLoad` only moves the sliders; sending to hardware is a separate, explicit press, so
 * loading a pose to look at it can never move the robot by itself.
 */
export function PoseLibrary({ model, targets, onLoad, onSend, canSend, storage }) {
  const store = storage ?? (typeof window !== 'undefined' ? window.localStorage : null);
  const [poses, setPoses] = React.useState(() => readPoses(store));
  const [name, setName] = React.useState('');
  const [note, setNote] = React.useState('');

  // Returns whether the write actually landed, so callers do not report success over a
  // storage failure — the pose is still in memory, but it will not survive a reload.
  const persist = React.useCallback(
    (next) => {
      setPoses(next);
      const ok = writePoses(store, next);
      if (!ok) {
        setNote('Could not save — this browser is blocking site data. Poses last until reload.');
      }
      return ok;
    },
    [store],
  );

  const save = React.useCallback(() => {
    const trimmed = name.trim();
    if (!trimmed) {
      setNote('Give the pose a name first.');
      return;
    }
    const existing = poses.some((p) => p.name.toLowerCase() === trimmed.toLowerCase());
    const ok = persist(upsertPose(poses, poseFromTargets(trimmed, targets, model)));
    setName('');
    if (ok) setNote(existing ? `Updated "${trimmed}".` : `Saved "${trimmed}".`);
  }, [name, poses, persist, targets, model]);

  return (
    <div className="poseLibrary">
      <div className="row toolbar compactToolbar">
        <strong>Poses</strong>
        <input
          aria-label="pose name"
          placeholder="name this pose"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') save();
          }}
        />
        <button onClick={save}>Save current</button>
        <span className="muted">{poses.length} saved</span>
      </div>
      {note && <p className="muted poseNote">{note}</p>}

      {poses.length === 0 ? (
        <p className="muted">
          No saved poses yet. Set the sliders where you want them and press Save current — it
          captures all {model.arm.length + model.hand.joints.length} joints.
        </p>
      ) : (
        <ul className="poseList">
          {poses.map((pose) => {
            const stale = unknownJoints(pose, model);
            return (
              <li key={pose.name} className="poseListItem">
                <span className="poseName">{pose.name}</span>
                <span className="muted">{Object.keys(pose.joints).length} joints</span>
                {stale.length > 0 && (
                  <span className="warnChip" title={stale.join(', ')}>
                    {stale.length} unknown
                  </span>
                )}
                <button onClick={() => onLoad(poseToTargets(pose, model, targets))}>Load</button>
                <button
                  onClick={() => onSend(poseToTargets(pose, model, targets))}
                  disabled={!canSend}
                  title={canSend ? 'Load and send to the robot' : 'Connect a gateway first'}
                >
                  Send
                </button>
                <button
                  className="ghostBtn small"
                  onClick={() => {
                    persist(removePose(poses, pose.name));
                    setNote(`Deleted "${pose.name}".`);
                  }}
                  aria-label={`delete ${pose.name}`}
                >
                  ×
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
