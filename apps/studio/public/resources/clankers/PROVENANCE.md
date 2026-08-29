# clankers_combined — generated package

Everything here is produced by `uv run clankers-build-urdf`. Do not hand-edit
`urdf/clankers_combined.urdf`; change `adapter:` in
`packages/clankers/src/clankers/config/robots.yaml` and re-run the builder.

## Sources

- **Arm** — `reBot_B601_DM_with_gripper` URDF that ships with the studio
  (`resources/arm02/`). Its meshes are referenced in place, not copied, so the ~24 MB of arm
  STLs is not duplicated in git. That is why the viewer configures two mesh packages.
- **Adapter** — `meshes/adapter_v5.stl`, exported from the user's CAD, authored in
  **millimetres** (the URDF applies `scale="0.001 0.001 0.001"`).
- **Hand** — `source/leap_hand.urdf` and the `meshes/leap_*.stl` files, vendored from
  <https://github.com/checkpoint214159/LEAP_Hand_Sim> (`assets/leap_hand/`), a fork of the
  LEAP Hand sim repo. Meshes are in metres. The 16 joints are renamed on the way in, from the
  LEAP URDF's `"0".."15"` to the semantic names in robots.yaml.

## Gripper

`adapter.keep_gripper` is `false`: the parallel gripper and the adapter bolt to the same
link6 flange, so they cannot both be on the real robot. The generated URDF therefore has no
`gripper_link` / `gripper_left` / `gripper_right`. That is a separate decision from
`arm.lerobot.use_gripper`, which tells the arm gateway how many motors to expect.

## Mount geometry

Measured off `adapter_v5.stl` on 2026-08-29:

| Face | Features | Meaning |
|---|---|---|
| z=0 | 4 × Ø7.05 on a 35×35 square (Ø49.5 bolt circle) + Ø14 central bore, centred on the STL origin | arm tool flange; the STL origin **is** the flange axis |
| z=26 | 6 × Ø6.2 on a Ø27 bolt circle, also centred on the origin | hand mount, coaxial with the wrist |

`link6`'s own mesh runs z = [-0.5, 9.0] mm in its frame, so its flange face is at z = +9 mm.

The **arm side is measured; the hand side is an assumption** — the palm's largest flat face
(z = +11.28 mm in the `palm_lower` link frame, normal +Z) is taken as its mounting face,
centred on the wrist axis, with yaw clocking 0. The 6-hole ring permits 60° increments, so if
the real build is clocked differently, change `adapter.hand_mount.rpy[2]`. `adapter.calibrated`
stays `false` until someone confirms it against the physical assembly.
