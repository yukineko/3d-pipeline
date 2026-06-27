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


# ---------------------------------------------------------------------------
# Tests for infer_vrm_overrides (sparse extraction)
# ---------------------------------------------------------------------------

class TestInferVrmOverridesSparse(unittest.TestCase):
    """Verify that infer_vrm_overrides returns sparse dicts with only Gemini-emitted keys."""

    def test_empty_response_returns_empty_dict(self):
        """Empty {} response should return empty dict."""
        with patch(MOCK_TARGET, return_value="{}"):
            result = vroid_params.infer_vrm_overrides("bare prompt")
        self.assertEqual(result, {})

    def test_only_happy_expression_returns_sparse(self):
        """If Gemini returns only happy=0.9, result should have ONLY expressions.happy."""
        response = json.dumps({"expressions": {"happy": 0.9}})
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.infer_vrm_overrides("test")
        self.assertIn("expressions", result)
        self.assertEqual(list(result["expressions"].keys()), ["happy"])
        self.assertAlmostEqual(result["expressions"]["happy"], 0.9)
        # No materials or height_scale
        self.assertNotIn("materials", result)
        self.assertNotIn("height_scale", result)

    def test_only_hair_color_returns_sparse(self):
        """If Gemini returns only materials.hair, sparse dict has only that."""
        response = json.dumps({"materials": {"hair": [0.5, 0.3, 0.1, 1.0]}})
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.infer_vrm_overrides("test")
        self.assertIn("materials", result)
        self.assertEqual(list(result["materials"].keys()), ["hair"])
        self.assertEqual(len(result["materials"]["hair"]), 4)
        # No expressions or height_scale
        self.assertNotIn("expressions", result)
        self.assertNotIn("height_scale", result)

    def test_only_height_scale_returns_sparse(self):
        """If Gemini returns only height_scale, sparse dict has only that."""
        response = json.dumps({"height_scale": 1.2})
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.infer_vrm_overrides("test")
        self.assertAlmostEqual(result["height_scale"], 1.2)
        # No expressions or materials
        self.assertNotIn("expressions", result)
        self.assertNotIn("materials", result)

    def test_multiple_expressions_sparse(self):
        """Multiple expressions but not all 6."""
        response = json.dumps({"expressions": {"happy": 0.8, "sad": 0.3}})
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.infer_vrm_overrides("test")
        self.assertEqual(set(result["expressions"].keys()), {"happy", "sad"})
        self.assertAlmostEqual(result["expressions"]["happy"], 0.8)
        self.assertAlmostEqual(result["expressions"]["sad"], 0.3)

    def test_multiple_materials_sparse(self):
        """Multiple materials but not all 4."""
        response = json.dumps({
            "materials": {
                "hair": [0.1, 0.2, 0.3, 1.0],
                "eye": [0.5, 0.5, 0.8, 1.0],
            }
        })
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.infer_vrm_overrides("test")
        self.assertEqual(set(result["materials"].keys()), {"hair", "eye"})

    def test_values_are_clamped_in_sparse(self):
        """Out-of-range values must be clamped even in sparse mode."""
        response = json.dumps({
            "expressions": {"happy": 5.0},
            "materials": {"hair": [2.0, -0.5, 0.5, 1.0]},
            "height_scale": 10.0,
        })
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.infer_vrm_overrides("test")
        self.assertAlmostEqual(result["expressions"]["happy"], 1.0)
        self.assertAlmostEqual(result["materials"]["hair"][0], 1.0)
        self.assertAlmostEqual(result["materials"]["hair"][1], 0.0)
        self.assertAlmostEqual(result["height_scale"], 2.0)

    def test_empty_expressions_dict_omitted(self):
        """If expressions dict is empty, it should be omitted from result."""
        response = json.dumps({
            "expressions": {},
            "materials": {"hair": [0.1, 0.1, 0.1, 1.0]},
        })
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.infer_vrm_overrides("test")
        self.assertNotIn("expressions", result)
        self.assertIn("materials", result)

    def test_empty_materials_dict_omitted(self):
        """If materials dict is empty, it should be omitted from result."""
        response = json.dumps({
            "expressions": {"happy": 0.5},
            "materials": {},
        })
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.infer_vrm_overrides("test")
        self.assertIn("expressions", result)
        self.assertNotIn("materials", result)


# ---------------------------------------------------------------------------
# Tests for resolve_vrm_adjustments (preset + overrides)
# ---------------------------------------------------------------------------

class TestResolveVrmAdjustments(unittest.TestCase):
    """Verify that resolve_vrm_adjustments layers Gemini overrides onto preset baseline."""

    def test_empty_prompt_returns_full_preset_cheerful(self):
        """Bare/empty response should return the full preset (all 6 expr + 4 mat + height)."""
        with patch(MOCK_TARGET, return_value="{}"):
            result = vroid_params.resolve_vrm_adjustments("", preset_name="cheerful")

        # Should have all 6 expressions from cheerful preset
        self.assertEqual(len(result["expressions"]), 6)
        self.assertAlmostEqual(result["expressions"]["happy"], 0.80)
        self.assertAlmostEqual(result["expressions"]["angry"], 0.00)
        self.assertAlmostEqual(result["expressions"]["sad"], 0.00)
        self.assertAlmostEqual(result["expressions"]["relaxed"], 0.30)
        self.assertAlmostEqual(result["expressions"]["surprised"], 0.20)
        self.assertAlmostEqual(result["expressions"]["blink"], 0.10)

        # Should have all 4 materials from cheerful preset
        self.assertEqual(len(result["materials"]), 4)
        self.assertEqual(len(result["materials"]["hair"]), 4)
        self.assertAlmostEqual(result["materials"]["hair"][0], 0.55)

        # Should have height_scale from preset
        self.assertAlmostEqual(result["height_scale"], 1.00)

    def test_empty_prompt_returns_full_preset_cool(self):
        """Bare prompt with cool preset should return full cool preset."""
        with patch(MOCK_TARGET, return_value="{}"):
            result = vroid_params.resolve_vrm_adjustments("", preset_name="cool")

        # cool preset has relaxed=0.70
        self.assertAlmostEqual(result["expressions"]["relaxed"], 0.70)
        # cool preset has height_scale=1.05
        self.assertAlmostEqual(result["height_scale"], 1.05)

    def test_default_preset_used_when_none_specified(self):
        """When preset_name is None, DEFAULT_PRESET_NAME should be used."""
        with patch(MOCK_TARGET, return_value="{}"):
            result = vroid_params.resolve_vrm_adjustments("", preset_name=None)

            # Should match DEFAULT_PRESET_NAME (which is "cheerful")
            expected = vroid_params.resolve_vrm_adjustments("", preset_name="cheerful")
            self.assertEqual(result["expressions"], expected["expressions"])
            self.assertEqual(result["materials"], expected["materials"])
            self.assertEqual(result["height_scale"], expected["height_scale"])

    def test_partial_override_happy_only(self):
        """Gemini returns only happy; all other exprs and all materials preserved."""
        response = json.dumps({"expressions": {"happy": 0.95}})
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.resolve_vrm_adjustments("happy", preset_name="cheerful")

        # happy should be overridden to 0.95
        self.assertAlmostEqual(result["expressions"]["happy"], 0.95)
        # Other expressions should keep preset values
        self.assertAlmostEqual(result["expressions"]["angry"], 0.00)
        self.assertAlmostEqual(result["expressions"]["sad"], 0.00)
        self.assertAlmostEqual(result["expressions"]["relaxed"], 0.30)
        self.assertAlmostEqual(result["expressions"]["surprised"], 0.20)
        self.assertAlmostEqual(result["expressions"]["blink"], 0.10)
        # All materials and height_scale from preset
        self.assertAlmostEqual(result["materials"]["hair"][0], 0.55)
        self.assertAlmostEqual(result["height_scale"], 1.00)

    def test_partial_override_hair_color_only(self):
        """Gemini returns only hair color; all expressions and other materials preserved."""
        response = json.dumps({"materials": {"hair": [0.2, 0.2, 0.2, 1.0]}})
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.resolve_vrm_adjustments("dark", preset_name="cheerful")

        # hair should be overridden
        self.assertAlmostEqual(result["materials"]["hair"][0], 0.2)
        # Other materials should keep preset values
        self.assertAlmostEqual(result["materials"]["skin"][0], 0.95)
        self.assertAlmostEqual(result["materials"]["eye"][0], 0.85)
        self.assertAlmostEqual(result["materials"]["outfit"][0], 0.90)
        # All expressions preserved from preset
        self.assertAlmostEqual(result["expressions"]["happy"], 0.80)

    def test_multiple_overrides_layered(self):
        """Gemini returns happy + sad + hair; all others preserved."""
        response = json.dumps({
            "expressions": {"happy": 0.5, "sad": 0.8},
            "materials": {"hair": [0.3, 0.3, 0.3, 1.0]},
        })
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.resolve_vrm_adjustments("mixed", preset_name="cheerful")

        # Overridden
        self.assertAlmostEqual(result["expressions"]["happy"], 0.5)
        self.assertAlmostEqual(result["expressions"]["sad"], 0.8)
        self.assertAlmostEqual(result["materials"]["hair"][0], 0.3)
        # Preserved from preset
        self.assertAlmostEqual(result["expressions"]["angry"], 0.00)
        self.assertAlmostEqual(result["expressions"]["relaxed"], 0.30)
        self.assertAlmostEqual(result["materials"]["skin"][0], 0.95)

    def test_height_scale_override(self):
        """Gemini returns height_scale; other values preserved."""
        response = json.dumps({"height_scale": 1.5})
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.resolve_vrm_adjustments("tall", preset_name="cheerful")

        # height_scale overridden
        self.assertAlmostEqual(result["height_scale"], 1.5)
        # Expressions and materials preserved
        self.assertAlmostEqual(result["expressions"]["happy"], 0.80)
        self.assertAlmostEqual(result["materials"]["hair"][0], 0.55)

    def test_all_sections_override(self):
        """Gemini returns all sections; everything should be overridden."""
        response = json.dumps({
            "expressions": {
                "happy": 0.1, "angry": 0.9, "sad": 0.5,
                "relaxed": 0.2, "surprised": 0.3, "blink": 0.0,
            },
            "materials": {
                "hair": [0.1, 0.1, 0.1, 1.0],
                "skin": [0.5, 0.5, 0.5, 1.0],
                "eye": [0.2, 0.2, 0.2, 1.0],
                "outfit": [0.7, 0.7, 0.7, 1.0],
            },
            "height_scale": 0.8,
        })
        with patch(MOCK_TARGET, return_value=response):
            result = vroid_params.resolve_vrm_adjustments("test", preset_name="cheerful")

        # All should be Gemini's values
        self.assertAlmostEqual(result["expressions"]["happy"], 0.1)
        self.assertAlmostEqual(result["expressions"]["angry"], 0.9)
        self.assertAlmostEqual(result["materials"]["hair"][0], 0.1)
        self.assertAlmostEqual(result["height_scale"], 0.8)

    def test_result_is_complete(self):
        """Result should always have all 6 expressions, 4 materials, height_scale."""
        with patch(MOCK_TARGET, return_value="{}"):
            result = vroid_params.resolve_vrm_adjustments("test", preset_name="cute")

        self.assertEqual(len(result["expressions"]), 6)
        self.assertEqual(len(result["materials"]), 4)
        self.assertIn("height_scale", result)


if __name__ == "__main__":
    unittest.main()
