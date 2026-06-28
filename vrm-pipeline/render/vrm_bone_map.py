"""
render/vrm_bone_map.py

Pure-data mapping table + helpers between MPFB2's ``game_engine`` rig bone
names and the VRM (VRM1) humanoid bone slots.

Pipeline context
----------------
MPFB2 (MakeHuman Plugin for Blender) creates a human armature using its
``game_engine`` rig. The VRM_Addon_for_Blender exporter requires a Humanoid
bone-assignment table mapping each VRM humanoid slot to the actual armature
bone name. This module provides that mapping plus small lookup helpers.

IMPORTANT
---------
This module is intentionally **pure Python** and MUST NOT import ``bpy`` so it
can be imported in a bare environment (e.g. unit tests, tooling).

The bone names here assume MPFB2's ``game_engine`` rig. The actual rig bone
names can vary by MPFB2 version, so ``generate_body.py`` must reconcile this
table against the real ``armature.bones`` at runtime rather than trusting these
literals blindly.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

# MPFB2 ``game_engine`` rig bone name -> VRM humanoid slot name.
#
# Note: ``Root`` is intentionally NOT mapped -- it has no VRM humanoid slot.
MPFB_TO_VRM_HUMANOID: dict[str, str] = {
    "pelvis": "hips",
    "spine_01": "spine",
    "spine_02": "chest",
    "neck_01": "neck",
    "head": "head",
    "clavicle_l": "leftShoulder",
    "clavicle_r": "rightShoulder",
    "upperarm_l": "leftUpperArm",
    "upperarm_r": "rightUpperArm",
    "lowerarm_l": "leftLowerArm",
    "lowerarm_r": "rightLowerArm",
    "hand_l": "leftHand",
    "hand_r": "rightHand",
    "thigh_l": "leftUpperLeg",
    "thigh_r": "rightUpperLeg",
    "calf_l": "leftLowerLeg",
    "calf_r": "rightLowerLeg",
    "foot_l": "leftFoot",
    "foot_r": "rightFoot",
    "ball_l": "leftToes",
    "ball_r": "rightToes",
}

# VRM1 humanoid REQUIRED slots. chest/shoulders/toes are optional and are
# intentionally excluded here.
VRM_REQUIRED_BONES: tuple[str, ...] = (
    "hips",
    "spine",
    "neck",
    "head",
    "leftUpperArm",
    "leftLowerArm",
    "leftHand",
    "rightUpperArm",
    "rightLowerArm",
    "rightHand",
    "leftUpperLeg",
    "leftLowerLeg",
    "leftFoot",
    "rightUpperLeg",
    "rightLowerLeg",
    "rightFoot",
)


def vrm_slot_for_mpfb_bone(bone_name: str) -> Optional[str]:
    """Forward lookup: VRM humanoid slot for an MPFB bone, or ``None``."""
    return MPFB_TO_VRM_HUMANOID.get(bone_name)


def mpfb_bone_for_vrm_slot(slot: str) -> Optional[str]:
    """Reverse lookup: MPFB bone that maps to ``slot``, or ``None``."""
    for bone, mapped_slot in MPFB_TO_VRM_HUMANOID.items():
        if mapped_slot == slot:
            return bone
    return None


def missing_required_slots(available_mpfb_bones: Iterable[str]) -> List[str]:
    """Return the VRM REQUIRED slots not covered by the given MPFB bones.

    ``available_mpfb_bones`` is an iterable of MPFB bone names actually present
    on an armature. They are mapped through ``MPFB_TO_VRM_HUMANOID``; the
    returned list contains the ``VRM_REQUIRED_BONES`` slots that are NOT
    covered, preserving ``VRM_REQUIRED_BONES`` order. An empty list means every
    required slot is satisfied.
    """
    covered = {
        MPFB_TO_VRM_HUMANOID[bone]
        for bone in available_mpfb_bones
        if bone in MPFB_TO_VRM_HUMANOID
    }
    return [slot for slot in VRM_REQUIRED_BONES if slot not in covered]
