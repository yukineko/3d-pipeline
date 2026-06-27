"""
tests/test_presets.py

Unit tests for presets.py — versioned vetted-preset registry + merge logic.

Covers:
  (a) Module-level constants: PRESETS_VERSION, DEFAULT_PRESET_NAME
  (b) list_presets() returns all registered names
  (c) get_preset() returns a complete, schema-valid, non-trivially-styled dict
  (d) get_preset() returns a deep copy (registry isolation)
  (e) get_preset() raises KeyError for unknown names
  (f) merge_adjustments() — sparse override (expressions)
  (g) merge_adjustments() — sparse override (materials)
  (h) merge_adjustments() — height_scale override
  (i) merge_adjustments() — empty overrides keeps base intact
  (j) merge_adjustments() — does not mutate inputs
  (k) merge_adjustments() — overlapping keys replaced, non-overlapping kept

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_presets
"""

import copy
import sys
import unittest
from pathlib import Path

# Ensure vrm-pipeline/ is on sys.path so `import presets` works.
HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import presets
from presets import (
    DEFAULT_PRESET_NAME,
    PRESETS_VERSION,
    get_preset,
    list_presets,
    merge_adjustments,
)

# ---------------------------------------------------------------------------
# Schema constants (mirror vroid_params.py)
# ---------------------------------------------------------------------------

EXPRESSION_KEYS = ("happy", "angry", "sad", "relaxed", "surprised", "blink")
MATERIAL_KEYS = ("hair", "skin", "eye", "outfit")


def _assert_schema_valid(tc: unittest.TestCase, adj: dict, label: str = "") -> None:
    """Assert that *adj* is a complete, in-range adjustments dict."""
    pfx = f"[{label}] " if label else ""

    tc.assertIn("expressions", adj, f"{pfx}missing 'expressions'")
    tc.assertIn("materials", adj, f"{pfx}missing 'materials'")
    tc.assertIn("height_scale", adj, f"{pfx}missing 'height_scale'")

    expr = adj["expressions"]
    for key in EXPRESSION_KEYS:
        tc.assertIn(key, expr, f"{pfx}expressions missing key {key!r}")
        val = expr[key]
        tc.assertIsInstance(val, (int, float), f"{pfx}expressions[{key!r}] not numeric")
        tc.assertGreaterEqual(val, 0.0, f"{pfx}expressions[{key!r}] < 0")
        tc.assertLessEqual(val, 1.0, f"{pfx}expressions[{key!r}] > 1")

    mat = adj["materials"]
    for key in MATERIAL_KEYS:
        tc.assertIn(key, mat, f"{pfx}materials missing key {key!r}")
        color = mat[key]
        tc.assertEqual(len(color), 4, f"{pfx}materials[{key!r}] must have 4 components")
        for i, c in enumerate(color):
            tc.assertIsInstance(c, (int, float),
                                f"{pfx}materials[{key!r}][{i}] not numeric")
            tc.assertGreaterEqual(c, 0.0, f"{pfx}materials[{key!r}][{i}] < 0")
            tc.assertLessEqual(c, 1.0, f"{pfx}materials[{key!r}][{i}] > 1")

    hs = adj["height_scale"]
    tc.assertIsInstance(hs, (int, float), f"{pfx}height_scale not numeric")
    tc.assertGreaterEqual(hs, 0.5, f"{pfx}height_scale < 0.5")
    tc.assertLessEqual(hs, 2.0, f"{pfx}height_scale > 2.0")


# ---------------------------------------------------------------------------
# (a) Module constants
# ---------------------------------------------------------------------------

class TestModuleConstants(unittest.TestCase):

    def test_presets_version_is_string(self):
        self.assertIsInstance(PRESETS_VERSION, str)

    def test_presets_version_nonempty(self):
        self.assertTrue(PRESETS_VERSION.strip(), "PRESETS_VERSION must not be empty")

    def test_presets_version_semver_shape(self):
        """Version should look like X.Y.Z (three dot-separated segments)."""
        parts = PRESETS_VERSION.split(".")
        self.assertEqual(len(parts), 3,
                         f"PRESETS_VERSION {PRESETS_VERSION!r} is not X.Y.Z")
        for part in parts:
            self.assertTrue(part.isdigit(),
                            f"Version segment {part!r} is not an integer")

    def test_default_preset_name_is_string(self):
        self.assertIsInstance(DEFAULT_PRESET_NAME, str)

    def test_default_preset_name_in_registry(self):
        self.assertIn(DEFAULT_PRESET_NAME, list_presets(),
                      "DEFAULT_PRESET_NAME must be a registered preset")


# ---------------------------------------------------------------------------
# (b) list_presets()
# ---------------------------------------------------------------------------

class TestListPresets(unittest.TestCase):

    def test_returns_list(self):
        result = list_presets()
        self.assertIsInstance(result, list)

    def test_at_least_three_presets(self):
        self.assertGreaterEqual(len(list_presets()), 3,
                                "Registry must contain at least 3 presets")

    def test_all_names_are_strings(self):
        for name in list_presets():
            self.assertIsInstance(name, str,
                                  f"Preset name {name!r} is not a string")

    def test_default_preset_in_list(self):
        self.assertIn(DEFAULT_PRESET_NAME, list_presets())


# ---------------------------------------------------------------------------
# (c) get_preset() — schema validity + non-trivial styling
# ---------------------------------------------------------------------------

class TestGetPresetSchema(unittest.TestCase):
    """Each registered preset must be schema-valid and non-trivially styled."""

    def test_all_presets_schema_valid(self):
        for name in list_presets():
            with self.subTest(preset=name):
                adj = get_preset(name)
                _assert_schema_valid(self, adj, label=name)

    def test_all_presets_non_zero_expressions(self):
        """No preset should have ALL expression values at 0.0 (that is un-styled)."""
        for name in list_presets():
            with self.subTest(preset=name):
                expr = get_preset(name)["expressions"]
                total = sum(expr[k] for k in EXPRESSION_KEYS)
                self.assertGreater(
                    total, 0.0,
                    f"Preset {name!r} has all-zero expressions (un-styled)"
                )

    def test_all_presets_have_distinct_colors(self):
        """Different presets must not have identical color palettes."""
        names = list_presets()
        palettes = []
        for name in names:
            mat = get_preset(name)["materials"]
            palette = tuple(
                tuple(mat[k]) for k in sorted(mat.keys())
            )
            palettes.append((name, palette))
        # Check pairwise distinctness
        for i, (n1, p1) in enumerate(palettes):
            for n2, p2 in palettes[i + 1:]:
                self.assertNotEqual(
                    p1, p2,
                    f"Presets {n1!r} and {n2!r} have identical color palettes"
                )

    def test_cheerful_preset_present(self):
        """The 'cheerful' preset must exist by name."""
        self.assertIn("cheerful", list_presets())

    def test_cool_preset_present(self):
        """The 'cool' preset must exist by name."""
        self.assertIn("cool", list_presets())

    def test_cute_preset_present(self):
        """The 'cute' preset must exist by name."""
        self.assertIn("cute", list_presets())


# ---------------------------------------------------------------------------
# (d) get_preset() — deep copy / registry isolation
# ---------------------------------------------------------------------------

class TestGetPresetDeepCopy(unittest.TestCase):

    def test_mutating_result_does_not_affect_registry(self):
        """Mutating the returned dict must not change a subsequent call."""
        p1 = get_preset(DEFAULT_PRESET_NAME)
        p1["expressions"]["happy"] = 9999.0
        p1["materials"]["hair"] = [0.0, 0.0, 0.0, 0.0]
        p1["height_scale"] = 99.0

        p2 = get_preset(DEFAULT_PRESET_NAME)
        self.assertNotEqual(p2["expressions"]["happy"], 9999.0,
                            "Registry expressions was mutated by caller")
        self.assertNotEqual(p2["materials"]["hair"], [0.0, 0.0, 0.0, 0.0],
                            "Registry materials was mutated by caller")
        self.assertNotEqual(p2["height_scale"], 99.0,
                            "Registry height_scale was mutated by caller")

    def test_two_calls_return_independent_objects(self):
        """Two consecutive calls must return distinct dict objects."""
        p1 = get_preset(DEFAULT_PRESET_NAME)
        p2 = get_preset(DEFAULT_PRESET_NAME)
        self.assertIsNot(p1, p2)
        self.assertIsNot(p1["expressions"], p2["expressions"])
        self.assertIsNot(p1["materials"], p2["materials"])

    def test_inner_lists_are_independent(self):
        """Color lists inside the returned dict are independent copies."""
        p1 = get_preset(DEFAULT_PRESET_NAME)
        p2 = get_preset(DEFAULT_PRESET_NAME)
        hair_list = p1["materials"]["hair"]
        hair_list[0] = 0.0
        # p2's hair list should not be affected
        self.assertNotEqual(p2["materials"]["hair"][0], 0.0,
                            "Inner color list shared between get_preset() calls")


# ---------------------------------------------------------------------------
# (e) get_preset() — KeyError for unknown name
# ---------------------------------------------------------------------------

class TestGetPresetErrors(unittest.TestCase):

    def test_unknown_name_raises_key_error(self):
        with self.assertRaises(KeyError):
            get_preset("__nonexistent_preset__")

    def test_empty_name_raises_key_error(self):
        with self.assertRaises(KeyError):
            get_preset("")

    def test_case_sensitivity(self):
        """Preset names are case-sensitive."""
        name = list_presets()[0]
        with self.assertRaises(KeyError):
            get_preset(name.upper())


# ---------------------------------------------------------------------------
# (f) merge_adjustments() — sparse expression override
# ---------------------------------------------------------------------------

class TestMergeExpressions(unittest.TestCase):

    def setUp(self):
        self.base = get_preset("cheerful")

    def test_sparse_expression_changes_only_specified_key(self):
        overrides = {"expressions": {"happy": 0.01}}
        result = merge_adjustments(self.base, overrides)
        self.assertAlmostEqual(result["expressions"]["happy"], 0.01)
        # All other expression keys must retain base values
        for key in EXPRESSION_KEYS:
            if key != "happy":
                self.assertAlmostEqual(
                    result["expressions"][key],
                    self.base["expressions"][key],
                    msg=f"expressions[{key!r}] should not have changed"
                )

    def test_multiple_expression_keys_override(self):
        overrides = {"expressions": {"happy": 0.1, "sad": 0.9}}
        result = merge_adjustments(self.base, overrides)
        self.assertAlmostEqual(result["expressions"]["happy"], 0.1)
        self.assertAlmostEqual(result["expressions"]["sad"], 0.9)
        # relaxed, angry, surprised, blink unchanged
        for key in ("relaxed", "angry", "surprised", "blink"):
            self.assertAlmostEqual(
                result["expressions"][key],
                self.base["expressions"][key],
                msg=f"expressions[{key!r}] should not have changed"
            )

    def test_no_expressions_in_overrides_keeps_base(self):
        overrides = {"height_scale": 1.5}
        result = merge_adjustments(self.base, overrides)
        for key in EXPRESSION_KEYS:
            self.assertAlmostEqual(
                result["expressions"][key],
                self.base["expressions"][key],
                msg=f"expressions[{key!r}] should not have changed"
            )


# ---------------------------------------------------------------------------
# (g) merge_adjustments() — sparse material override
# ---------------------------------------------------------------------------

class TestMergeMaterials(unittest.TestCase):

    def setUp(self):
        self.base = get_preset("cool")

    def test_sparse_material_changes_only_specified_key(self):
        new_hair = [0.5, 0.5, 0.5, 1.0]
        overrides = {"materials": {"hair": new_hair}}
        result = merge_adjustments(self.base, overrides)
        self.assertEqual(result["materials"]["hair"], new_hair)
        # Other material keys unchanged
        for key in ("skin", "eye", "outfit"):
            self.assertEqual(
                result["materials"][key],
                self.base["materials"][key],
                msg=f"materials[{key!r}] should not have changed"
            )

    def test_no_materials_in_overrides_keeps_base(self):
        overrides = {"expressions": {"relaxed": 0.0}}
        result = merge_adjustments(self.base, overrides)
        for key in MATERIAL_KEYS:
            self.assertEqual(
                result["materials"][key],
                self.base["materials"][key],
                msg=f"materials[{key!r}] should not have changed"
            )

    def test_material_override_value_is_deep_copied(self):
        """Mutating the overrides list after merge must not affect result."""
        new_eye = [0.1, 0.2, 0.3, 1.0]
        overrides = {"materials": {"eye": new_eye}}
        result = merge_adjustments(self.base, overrides)
        new_eye[0] = 0.99  # mutate the original list
        self.assertAlmostEqual(result["materials"]["eye"][0], 0.1,
                               msg="Result material was not deep-copied from overrides")


# ---------------------------------------------------------------------------
# (h) merge_adjustments() — height_scale override
# ---------------------------------------------------------------------------

class TestMergeHeightScale(unittest.TestCase):

    def setUp(self):
        self.base = get_preset("cute")

    def test_height_scale_override_replaces(self):
        overrides = {"height_scale": 1.8}
        result = merge_adjustments(self.base, overrides)
        self.assertAlmostEqual(result["height_scale"], 1.8)

    def test_absent_height_scale_keeps_base(self):
        overrides = {"expressions": {"happy": 0.3}}
        result = merge_adjustments(self.base, overrides)
        self.assertAlmostEqual(
            result["height_scale"],
            self.base["height_scale"],
            msg="height_scale should not change when absent from overrides"
        )


# ---------------------------------------------------------------------------
# (i) merge_adjustments() — empty overrides
# ---------------------------------------------------------------------------

class TestMergeEmptyOverrides(unittest.TestCase):

    def test_empty_overrides_returns_copy_of_base(self):
        base = get_preset("cheerful")
        result = merge_adjustments(base, {})
        self.assertEqual(result["expressions"], base["expressions"])
        self.assertEqual(result["materials"], base["materials"])
        self.assertAlmostEqual(result["height_scale"], base["height_scale"])

    def test_result_is_not_same_object_as_base(self):
        base = get_preset("cheerful")
        result = merge_adjustments(base, {})
        self.assertIsNot(result, base)


# ---------------------------------------------------------------------------
# (j) merge_adjustments() — no mutation of inputs
# ---------------------------------------------------------------------------

class TestMergeNoMutation(unittest.TestCase):

    def test_base_not_mutated(self):
        base = get_preset("cheerful")
        base_snapshot = copy.deepcopy(base)
        overrides = {
            "expressions": {"happy": 0.01, "surprised": 0.99},
            "materials": {"hair": [0.0, 0.0, 0.0, 1.0]},
            "height_scale": 1.9,
        }
        merge_adjustments(base, overrides)
        self.assertEqual(base, base_snapshot, "base was mutated by merge_adjustments")

    def test_overrides_not_mutated(self):
        base = get_preset("cool")
        overrides = {
            "expressions": {"relaxed": 0.1},
            "materials": {"eye": [0.9, 0.1, 0.1, 1.0]},
        }
        overrides_snapshot = copy.deepcopy(overrides)
        merge_adjustments(base, overrides)
        self.assertEqual(overrides, overrides_snapshot,
                         "overrides was mutated by merge_adjustments")


# ---------------------------------------------------------------------------
# (k) merge_adjustments() — combined / result completeness
# ---------------------------------------------------------------------------

class TestMergeResultCompleteness(unittest.TestCase):

    def test_result_has_all_expression_keys(self):
        base = get_preset("cute")
        result = merge_adjustments(base, {"expressions": {"happy": 0.99}})
        for key in EXPRESSION_KEYS:
            self.assertIn(key, result["expressions"],
                          f"expressions[{key!r}] missing from merge result")

    def test_result_has_all_material_keys(self):
        base = get_preset("cute")
        result = merge_adjustments(base, {"materials": {"hair": [0.1, 0.1, 0.1, 1.0]}})
        for key in MATERIAL_KEYS:
            self.assertIn(key, result["materials"],
                          f"materials[{key!r}] missing from merge result")

    def test_full_override_replaces_everything(self):
        base = get_preset("cheerful")
        full_override = get_preset("cool")
        result = merge_adjustments(base, full_override)
        self.assertEqual(result["expressions"], full_override["expressions"])
        self.assertEqual(result["materials"], full_override["materials"])
        self.assertAlmostEqual(result["height_scale"], full_override["height_scale"])

    def test_merge_with_sparse_nested_override_is_schema_valid(self):
        """Any merge result (even from sparse overrides) must still be schema-valid."""
        base = get_preset("cheerful")
        sparse = {"expressions": {"angry": 0.5}, "height_scale": 1.1}
        result = merge_adjustments(base, sparse)
        _assert_schema_valid(self, result, label="merge(cheerful, sparse)")


if __name__ == "__main__":
    unittest.main()
