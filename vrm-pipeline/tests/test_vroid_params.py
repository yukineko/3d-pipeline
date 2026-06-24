"""
tests/test_vroid_params.py

Unit tests for vroid_params.infer_vrm_adjustments.

Design:
  - No real Gemini API calls. _call_gemini is mocked via unittest.mock.patch.
  - Tests cover:
      (a) Normal JSON parsing and schema normalization
      (b) Out-of-range values (expression > 1, height_scale > 2.0) are clamped
      (c) Missing keys are filled with safe defaults
      (d) ```json fenced responses are parsed correctly
      (e) Module is importable without google.generativeai installed

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_vroid_params
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure vrm-pipeline/ is on sys.path so `import vroid_params` works.
HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import vroid_params


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_full_response(**overrides) -> str:
    """Return a JSON string for a complete valid Gemini response."""
    data = {
        "expressions": {
            "happy": 0.8,
            "angry": 0.0,
            "sad": 0.1,
            "relaxed": 0.5,
            "surprised": 0.2,
            "blink": 0.0,
        },
        "materials": {
            "hair": [0.1, 0.05, 0.02, 1.0],
            "skin": [0.9, 0.75, 0.65, 1.0],
            "eye":  [0.2, 0.5, 0.9, 1.0],
            "outfit": [0.3, 0.3, 0.3, 1.0],
        },
        "height_scale": 1.1,
    }
    data.update(overrides)
    return json.dumps(data)


MOCK_TARGET = "vroid_params._call_gemini"


# ---------------------------------------------------------------------------
# (a) Normal JSON parse and schema normalization
# ---------------------------------------------------------------------------

class TestNormalParsing(unittest.TestCase):
    """Verify that a well-formed Gemini response is parsed correctly."""

    def test_returns_dict(self):
        with patch(MOCK_TARGET, return_value=_make_full_response()):
            result = vroid_params.infer_vrm_adjustments("a happy anime girl")
        self.assertIsInstance(result, dict)

    def test_expressions_present_and_correct(self):
        with patch(MOCK_TARGET, return_value=_make_full_response()):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertIn("expressions", result)
        expr = result["expressions"]
        for key in ("happy", "angry", "sad", "relaxed", "surprised", "blink"):
            self.assertIn(key, expr)
        self.assertAlmostEqual(expr["happy"], 0.8)
        self.assertAlmostEqual(expr["relaxed"], 0.5)

    def test_materials_present_and_correct(self):
        with patch(MOCK_TARGET, return_value=_make_full_response()):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertIn("materials", result)
        mat = result["materials"]
        self.assertIn("hair", mat)
        self.assertEqual(len(mat["hair"]), 4)
        self.assertAlmostEqual(mat["hair"][0], 0.1)

    def test_height_scale_present_and_correct(self):
        with patch(MOCK_TARGET, return_value=_make_full_response()):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertIn("height_scale", result)
        self.assertAlmostEqual(result["height_scale"], 1.1)

    def test_all_expression_keys_present(self):
        """All 6 expression keys must always be present in the result."""
        with patch(MOCK_TARGET, return_value=_make_full_response()):
            result = vroid_params.infer_vrm_adjustments("test")
        expected_keys = {"happy", "angry", "sad", "relaxed", "surprised", "blink"}
        self.assertEqual(set(result["expressions"].keys()), expected_keys)


# ---------------------------------------------------------------------------
# (b) Out-of-range values are clamped
# ---------------------------------------------------------------------------

class TestClamping(unittest.TestCase):
    """Out-of-range values must be clamped, not rejected."""

    def test_expression_above_1_is_clamped(self):
        data = json.loads(_make_full_response())
        data["expressions"]["happy"] = 5.0  # out of range
        raw = json.dumps(data)
        with patch(MOCK_TARGET, return_value=raw):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertAlmostEqual(result["expressions"]["happy"], 1.0)

    def test_expression_below_0_is_clamped(self):
        data = json.loads(_make_full_response())
        data["expressions"]["sad"] = -3.0
        raw = json.dumps(data)
        with patch(MOCK_TARGET, return_value=raw):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertAlmostEqual(result["expressions"]["sad"], 0.0)

    def test_height_scale_above_2_is_clamped(self):
        data = json.loads(_make_full_response())
        data["height_scale"] = 10.0
        raw = json.dumps(data)
        with patch(MOCK_TARGET, return_value=raw):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertAlmostEqual(result["height_scale"], 2.0)

    def test_height_scale_below_05_is_clamped(self):
        data = json.loads(_make_full_response())
        data["height_scale"] = 0.1
        raw = json.dumps(data)
        with patch(MOCK_TARGET, return_value=raw):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertAlmostEqual(result["height_scale"], 0.5)

    def test_color_component_above_1_is_clamped(self):
        data = json.loads(_make_full_response())
        data["materials"]["hair"] = [2.0, 0.5, 0.5, 1.0]
        raw = json.dumps(data)
        with patch(MOCK_TARGET, return_value=raw):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertAlmostEqual(result["materials"]["hair"][0], 1.0)

    def test_color_component_below_0_is_clamped(self):
        data = json.loads(_make_full_response())
        data["materials"]["skin"] = [-0.5, 0.75, 0.65, 1.0]
        raw = json.dumps(data)
        with patch(MOCK_TARGET, return_value=raw):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertAlmostEqual(result["materials"]["skin"][0], 0.0)


# ---------------------------------------------------------------------------
# (c) Missing keys filled with safe defaults
# ---------------------------------------------------------------------------

class TestMissingKeyDefaults(unittest.TestCase):
    """Missing keys in the Gemini response must be filled with safe defaults."""

    def test_missing_expression_keys_default_to_zero(self):
        # Only include 'happy' in expressions
        data = {
            "expressions": {"happy": 0.5},
            "materials": {},
            "height_scale": 1.0,
        }
        with patch(MOCK_TARGET, return_value=json.dumps(data)):
            result = vroid_params.infer_vrm_adjustments("test")
        expr = result["expressions"]
        # All 6 keys must be present
        for key in ("happy", "angry", "sad", "relaxed", "surprised", "blink"):
            self.assertIn(key, expr)
        self.assertAlmostEqual(expr["happy"], 0.5)
        self.assertAlmostEqual(expr["angry"], 0.0)
        self.assertAlmostEqual(expr["blink"], 0.0)

    def test_missing_height_scale_defaults_to_1(self):
        data = {
            "expressions": {},
            "materials": {},
        }
        with patch(MOCK_TARGET, return_value=json.dumps(data)):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertAlmostEqual(result["height_scale"], 1.0)

    def test_missing_expressions_key_entirely(self):
        data = {
            "materials": {"hair": [0.1, 0.1, 0.1, 1.0]},
            "height_scale": 1.2,
        }
        with patch(MOCK_TARGET, return_value=json.dumps(data)):
            result = vroid_params.infer_vrm_adjustments("test")
        expr = result["expressions"]
        for key in ("happy", "angry", "sad", "relaxed", "surprised", "blink"):
            self.assertAlmostEqual(expr[key], 0.0)

    def test_missing_material_key_is_omitted(self):
        """Material keys that are absent from the response are omitted, not defaulted."""
        data = {
            "expressions": {},
            "materials": {"hair": [0.1, 0.1, 0.1, 1.0]},
            "height_scale": 1.0,
        }
        with patch(MOCK_TARGET, return_value=json.dumps(data)):
            result = vroid_params.infer_vrm_adjustments("test")
        mat = result["materials"]
        self.assertIn("hair", mat)
        # skin, eye, outfit were not provided → should be absent
        self.assertNotIn("skin", mat)
        self.assertNotIn("eye", mat)
        self.assertNotIn("outfit", mat)

    def test_color_with_only_rgb_gets_alpha_1(self):
        """A 3-component color list should have alpha defaulted to 1.0."""
        data = {
            "expressions": {},
            "materials": {"eye": [0.2, 0.5, 0.9]},
            "height_scale": 1.0,
        }
        with patch(MOCK_TARGET, return_value=json.dumps(data)):
            result = vroid_params.infer_vrm_adjustments("test")
        eye = result["materials"]["eye"]
        self.assertEqual(len(eye), 4)
        self.assertAlmostEqual(eye[3], 1.0)


# ---------------------------------------------------------------------------
# (d) ```json fenced responses are parsed correctly
# ---------------------------------------------------------------------------

class TestFencedJSON(unittest.TestCase):
    """Gemini may wrap JSON in ```json ... ``` fences; these must be stripped."""

    def test_json_fenced_response(self):
        data = json.loads(_make_full_response())
        fenced = "```json\n" + json.dumps(data) + "\n```"
        with patch(MOCK_TARGET, return_value=fenced):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertAlmostEqual(result["expressions"]["happy"], 0.8)
        self.assertAlmostEqual(result["height_scale"], 1.1)

    def test_plain_backtick_fenced_response(self):
        data = json.loads(_make_full_response())
        fenced = "```\n" + json.dumps(data) + "\n```"
        with patch(MOCK_TARGET, return_value=fenced):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertAlmostEqual(result["expressions"]["happy"], 0.8)

    def test_fence_with_whitespace(self):
        data = json.loads(_make_full_response())
        fenced = "  ```json  \n" + json.dumps(data) + "\n  ```  "
        with patch(MOCK_TARGET, return_value=fenced):
            result = vroid_params.infer_vrm_adjustments("test")
        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# (e) Module importable without google.generativeai
# ---------------------------------------------------------------------------

class TestImportability(unittest.TestCase):
    """The module must be importable even when google.generativeai is absent."""

    def test_module_importable(self):
        import importlib
        mod = importlib.import_module("vroid_params")
        self.assertIsNotNone(mod)

    def test_infer_vrm_adjustments_is_callable(self):
        self.assertTrue(callable(vroid_params.infer_vrm_adjustments))

    def test_function_signature(self):
        import inspect
        sig = inspect.signature(vroid_params.infer_vrm_adjustments)
        params = list(sig.parameters.keys())
        self.assertIn("prompt", params)
        self.assertIn("image_path", params)
        self.assertIn("model", params)

    def test_call_gemini_is_callable(self):
        """_call_gemini must be a module-level function (the test seam)."""
        self.assertTrue(callable(vroid_params._call_gemini))


# ---------------------------------------------------------------------------
# Extra: invalid JSON from Gemini raises ValueError
# ---------------------------------------------------------------------------

class TestInvalidJSON(unittest.TestCase):

    def test_non_json_response_raises_value_error(self):
        with patch(MOCK_TARGET, return_value="I cannot answer that."):
            with self.assertRaises(ValueError):
                vroid_params.infer_vrm_adjustments("test")

    def test_empty_response_raises_value_error(self):
        with patch(MOCK_TARGET, return_value=""):
            with self.assertRaises(ValueError):
                vroid_params.infer_vrm_adjustments("test")


if __name__ == "__main__":
    unittest.main()
