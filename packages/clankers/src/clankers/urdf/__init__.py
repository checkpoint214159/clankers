"""Compose the combined arm + adapter + LEAP hand URDF from robots.yaml.

The three source models come from different places and never move: the arm URDF ships with
the studio, the adapter is a raw STL exported from CAD (in millimetres), and the hand URDF
comes from the dexmanip/LEAP_Hand_Sim assets. This package stitches them into one URDF whose
joint names match `robots.yaml`, so the studio can drive arm and hand through a single tree.
"""

from .compose import (
    ComposeError,
    MountTransform,
    compose_urdf,
    hand_joint_rename_map,
)

__all__ = ["ComposeError", "MountTransform", "compose_urdf", "hand_joint_rename_map"]
