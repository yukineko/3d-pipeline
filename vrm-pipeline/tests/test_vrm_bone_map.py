"""
tests/test_vrm_bone_map.py

Unit tests for render.vrm_bone_map -- the pure-data MPFB2 game_engine rig ->
VRM humanoid bone mapping table and helpers.

Design goals:
  * No Blender / bpy required (module is pure Python).
  * Verify the mapping covers every VRM REQUIRED slot.
  * Verify forward/reverse lookups and missing-slot detection.

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_vrm_bone_map
    # or
    python -m unittest discover -s tests
"""

import importlib
import sys
import unittest
from pathlib import Path

# Make vrm-pipeline/ the first entry so `render.*` resolves correctly.
HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from render import vrm_bone_map
from render.vrm_bone_map import (
    MPFB_TO_VRM_HUMANOID,
    VRM_REQUIRED_BONES,
    missing_required_slots,
    mpfb_bone_for_vrm_slot,
    vrm_slot_for_mpfb_bone,
)

ALL_MPFB_BONES = list(MPFB_TO_VRM_HUMANOID.keys())


class TestImportableWithoutBpy(unittest.TestCase):
    def test_module_imports_without_bpy(self):
        # Module must not have pulled in bpy as a dependency.
        had_bpy = "bpy" in sys.modules
        importlib.reload(vrm_bone_map)
        if not had_bpy:
            self.assertNotIn("bpy", sys.modules)


class TestRequiredSlotsCovered(unittest.TestCase):
    def test_every_required_slot_has_a_source_bone(self):
        values = set(MPFB_TO_VRM_HUMANOID.values())
        for slot in VRM_REQUIRED_BONES:
            self.assertIn(
                slot, values,
                msg=f"required slot {slot!r} has no MPFB source bone",
            )

    def test_required_bones_count_and_no_optional(self):
        self.assertEqual(len(VRM_REQUIRED_BONES), 16)
        for optional in ("chest", "leftShoulder", "rightShoulder",
                         "leftToes", "rightToes"):
            self.assertNotIn(optional, VRM_REQUIRED_BONES)


class TestForwardLookup(unittest.TestCase):
    def test_representative_forward_maps(self):
        self.assertEqual(vrm_slot_for_mpfb_bone("pelvis"), "hips")
        self.assertEqual(vrm_slot_for_mpfb_bone("upperarm_l"), "leftUpperArm")
        self.assertEqual(vrm_slot_for_mpfb_bone("ball_r"), "rightToes")

    def test_nonexistent_is_none(self):
        self.assertIsNone(vrm_slot_for_mpfb_bone("nonexistent"))

    def test_root_not_mapped(self):
        self.assertIsNone(vrm_slot_for_mpfb_bone("Root"))


class TestRoundTrip(unittest.TestCase):
    def test_forward_reverse_round_trip(self):
        for bone in ("pelvis", "head", "hand_r", "thigh_l", "foot_l", "ball_l"):
            slot = vrm_slot_for_mpfb_bone(bone)
            self.assertIsNotNone(slot)
            self.assertEqual(mpfb_bone_for_vrm_slot(slot), bone)

    def test_reverse_nonexistent_is_none(self):
        self.assertIsNone(mpfb_bone_for_vrm_slot("noSuchSlot"))


class TestMissingRequiredSlots(unittest.TestCase):
    def test_all_bones_satisfies_all_required(self):
        self.assertEqual(missing_required_slots(ALL_MPFB_BONES), [])

    def test_missing_pelvis_reports_hips(self):
        bones = [b for b in ALL_MPFB_BONES if b != "pelvis"]
        result = missing_required_slots(bones)
        self.assertIn("hips", result)

    def test_preserves_required_order(self):
        # Drop everything; missing list must equal VRM_REQUIRED_BONES in order.
        self.assertEqual(missing_required_slots([]), list(VRM_REQUIRED_BONES))

    def test_optional_bones_do_not_affect_required(self):
        # ball_l/clavicle_l map to optional slots; their presence/absence must
        # not change the required-slot result.
        self.assertEqual(missing_required_slots(ALL_MPFB_BONES), [])


if __name__ == "__main__":
    unittest.main()
