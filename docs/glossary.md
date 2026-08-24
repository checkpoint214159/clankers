# Glossary (ubiquitous language)

Naming collisions between the arm stack, hand stack, sim, and datasets are the #1 source of
silent bugs here. These are the canonical terms; avoid the synonyms.

| Term | Meaning | Avoid / don't confuse with |
|---|---|---|
| **joint index** | Position in a canonical ordered vector. Arm: `joint1..joint6, gripper` (1-based in UI, 0-based in arrays). Hand: `canonical` 0–15 in dexmanip `canonical_pose` order (URDF declaration order, NOT root→tip). | URDF link names — the LEAP URDF's `thumb_*` links are actually the ring finger. |
| **motor_id** | Damiao CAN ID (arm, 0x01–0x07). | `feedback_id` |
| **feedback_id** | Damiao Master ID for state frames = `0x10 + motor_id`. Never 0x00. | motor_id |
| **servo_id** | Dynamixel ID on the hand bus (0–15, provisional until bring-up). | canonical index — they are equal only by convention, verified at calibration. |
| **pose** | A named full configuration: 6 arm joints (+gripper) + 16 hand joints, radians. Lives in the pose library. | Cartesian pose (say "EE pose" for x/y/z/rpy). |
| **target / setpoint** | The commanded joint value sent this tick. | "goal" (Dynamixel register name `Goal_Position` only at the bus layer). |
| **command** | One gateway/WS op or one `send_action` call. | skill |
| **skill** | A named, timed sequence of poses or a policy rollout, run by the task runner. | pose |
| **episode** | One recorded LeRobotDataset segment with success/failure metadata. | session |
| **session** | One continuous run of a mode (debug/teleop/record/infer/task) owning the buses. | episode |
| **teleop** | Human drives the robot live (sliders now; retargeting later). | replay |
| **replay** | Re-executing a recorded episode's actions on hardware or in the viewer. | inference |
| **inference** | A policy generates actions from observations. | task |
| **frame conventions** | Quaternions `(x, y, z, w)` repo-wide. ROS URDFs are Z-up; the three.js viewer rotates −90° about X. | scipy/ROS `(w, x, y, z)` — convert at boundaries only. |
| **bus** | One physical serial connection: arm dm-serial OR hand U2D2. Exclusive: one process per bus per session. | gateway (the WS server that owns a bus during debug mode) |
