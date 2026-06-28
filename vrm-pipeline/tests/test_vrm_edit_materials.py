"""
Tests for VRoid/VRM-aware material classification in render.vrm_edit.

These run without bpy: the module uses lazy ``import bpy`` inside functions, so
``_classify_material`` / ``_material_matches_category`` are importable and callable
with plain strings and tiny fake objects.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from render.vrm_edit import _classify_material, _material_matches_category


class TestClassifyMaterialVRoid(unittest.TestCase):
    def test_body_skin(self):
        self.assertEqual(_classify_material("N00_000_00_Body_00_SKIN"), "skin")

    def test_face_skin(self):
        self.assertEqual(_classify_material("N00_000_00_Face_00_SKIN"), "skin")

    def test_eye_iris(self):
        self.assertEqual(_classify_material("N00_000_00_EyeIris_00_EYE"), "eye")

    def test_eye_white(self):
        self.assertEqual(_classify_material("N00_000_00_EyeWhite_00_EYE"), "eye")

    def test_eyeline_face_suffix_is_eye(self):
        # FACE suffix, but eye-region part wins via precedence.
        self.assertEqual(_classify_material("N00_000_00_Eyeline_00_FACE"), "eye")

    def test_eyelash_face_suffix_is_eye(self):
        self.assertEqual(_classify_material("N00_000_00_Eyelashout_00_FACE"), "eye")

    def test_eyebrow_face_suffix_is_eye(self):
        self.assertEqual(_classify_material("N00_000_00_Eyebrow_00_FACE"), "eye")

    def test_hair(self):
        self.assertEqual(_classify_material("N00_000_00_Hair_00_HAIR"), "hair")

    def test_hair_short_code(self):
        self.assertEqual(_classify_material("N00_000_Hair_00_HAIR"), "hair")

    def test_tops_outfit(self):
        self.assertEqual(_classify_material("N00_007_Tops_01_CLOTH"), "outfit")

    def test_bottoms_outfit(self):
        self.assertEqual(_classify_material("N00_007_Bottoms_01_CLOTH"), "outfit")

    def test_shoes_outfit(self):
        self.assertEqual(_classify_material("N00_007_Shoes_01_CLOTH"), "outfit")

    def test_onepiece_outfit(self):
        self.assertEqual(_classify_material("N00_007_Onepiece_01_CLOTH"), "outfit")

    def test_accessory_outfit(self):
        self.assertEqual(_classify_material("N00_007_Accessory_01_CLOTH"), "outfit")


class TestClassifyMaterialJapanese(unittest.TestCase):
    def test_hair_jp(self):
        self.assertEqual(_classify_material("髪"), "hair")

    def test_skin_jp(self):
        self.assertEqual(_classify_material("肌"), "skin")

    def test_skin_jp_face(self):
        self.assertEqual(_classify_material("顔"), "skin")

    def test_eye_jp(self):
        self.assertEqual(_classify_material("瞳"), "eye")

    def test_eye_jp_eyelash(self):
        self.assertEqual(_classify_material("まつ毛"), "eye")

    def test_eye_jp_eyebrow(self):
        self.assertEqual(_classify_material("眉"), "eye")

    def test_outfit_jp(self):
        self.assertEqual(_classify_material("服"), "outfit")

    def test_outfit_jp_shoes(self):
        self.assertEqual(_classify_material("靴"), "outfit")


class TestClassifyMaterialLegacyAndNone(unittest.TestCase):
    def test_legacy_hair_keyword(self):
        self.assertEqual(_classify_material("hair_mat"), "hair")

    def test_legacy_skin_keyword(self):
        self.assertEqual(_classify_material("skin_material"), "skin")

    def test_unrelated_returns_none(self):
        self.assertIsNone(_classify_material("Lambert1"))

    def test_empty_returns_none(self):
        self.assertIsNone(_classify_material(""))


class TestMaterialMatchesCategoryBackwardCompat(unittest.TestCase):
    def test_two_arg_hair_true(self):
        self.assertTrue(_material_matches_category("N00_000_00_Hair_00_HAIR", "hair"))

    def test_two_arg_non_matching_category_false(self):
        self.assertFalse(_material_matches_category("N00_000_00_Hair_00_HAIR", "skin"))

    def test_two_arg_legacy_skin(self):
        self.assertTrue(_material_matches_category("skin_mat", "skin"))

    def test_three_arg_with_mat(self):
        self.assertTrue(
            _material_matches_category("N00_000_00_EyeIris_00_EYE", "eye", None)
        )


# --- Tiny fake bpy-like material for the structural-hint path ---

class _FakeImage:
    def __init__(self, name):
        self.name = name


class _FakeNode:
    def __init__(self, name, image=None):
        self.name = name
        self.image = image


class _FakeNodeTree:
    def __init__(self, nodes):
        self.nodes = nodes


class _FakeMaterial:
    def __init__(self, node_tree=None):
        self.node_tree = node_tree


class TestStructuralHint(unittest.TestCase):
    def test_image_node_name_classifies_ambiguous_name(self):
        # Material name alone is ambiguous, but a texture image name says hair.
        mat = _FakeMaterial(
            _FakeNodeTree([_FakeNode("Image Texture", _FakeImage("Hair_Base.png"))])
        )
        self.assertEqual(_classify_material("mat_001", mat), "hair")

    def test_node_name_classifies_ambiguous_name(self):
        mat = _FakeMaterial(_FakeNodeTree([_FakeNode("EyeIris_tex")]))
        self.assertEqual(_classify_material("mat_002", mat), "eye")

    def test_garbage_structure_does_not_crash(self):
        # node_tree present but nodes is a non-iterable garbage value.
        class _Garbage:
            node_tree = object()
        self.assertIsNone(_classify_material("mat_003", _Garbage()))

    def test_no_node_tree_does_not_crash(self):
        mat = _FakeMaterial(node_tree=None)
        self.assertIsNone(_classify_material("mat_004", mat))

    def test_name_wins_over_hint(self):
        # Explicit name should classify before texture-hint fallback is reached.
        mat = _FakeMaterial(
            _FakeNodeTree([_FakeNode("Image Texture", _FakeImage("Hair.png"))])
        )
        self.assertEqual(_classify_material("N00_000_00_Body_00_SKIN", mat), "skin")


if __name__ == "__main__":
    unittest.main()
