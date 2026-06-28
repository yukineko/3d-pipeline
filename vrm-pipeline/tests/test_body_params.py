"""
tests/test_body_params.py

Unit tests for render.body_params — the MakeHuman/MPFB2 body-morph analogue of
vroid_params.py.

Design:
  - No real Gemini API calls. body_params._call_gemini is mocked via
    unittest.mock.patch — no network is ever touched.
  - GEMINI_API_KEY is monkeypatched into the environment where relevant.
  - Module must import cleanly without bpy / google-generativeai present.

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_body_params
    # or
    python -m unittest discover -s tests
"""

import importlib
import inspect
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Make vrm-pipeline/ the first entry so `render.*` resolves correctly.
HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from render import body_params

MOCK_TARGET = "render.body_params._call_gemini"


# ---------------------------------------------------------------------------
# Import guard: module must be importable without bpy.
# ---------------------------------------------------------------------------

class TestImportability(unittest.TestCase):
    """render.body_params must import cleanly in a plain-Python environment."""

    def test_module_importable(self):
        mod = importlib.import_module("render.body_params")
        self.assertIsNotNone(mod)

    def test_bpy_not_imported(self):
        """Importing the module must not pull in bpy."""
        # If the import above had required bpy, it would have failed already.
        self.assertNotIn("bpy", sys.modules)

    def test_public_api_callable(self):
        self.assertTrue(callable(body_params.infer_body_overrides))
        self.assertTrue(callable(body_params.resolve_body_morphs))
        self.assertTrue(callable(body_params._call_gemini))

    def test_resolve_signature(self):
        sig = inspect.signature(body_params.resolve_body_morphs)
        params = list(sig.parameters.keys())
        self.assertIn("prompt", params)
        self.assertIn("preset_name", params)
        self.assertIn("image_path", params)
        self.assertIn("model", params)


# ---------------------------------------------------------------------------
# _clamp01 boundaries
# ---------------------------------------------------------------------------

class TestClamp01(unittest.TestCase):

    def test_negative_clamped_to_zero(self):
        self.assertEqual(body_params._clamp01(-3.0), 0.0)

    def test_above_one_clamped_to_one(self):
        self.assertEqual(body_params._clamp01(5.0), 1.0)

    def test_mid_passes_through(self):
        self.assertEqual(body_params._clamp01(0.5), 0.5)

    def test_boundaries_exact(self):
        self.assertEqual(body_params._clamp01(0.0), 0.0)
        self.assertEqual(body_params._clamp01(1.0), 1.0)

    def test_int_coerced_to_float(self):
        result = body_params._clamp01(1)
        self.assertIsInstance(result, float)
        self.assertEqual(result, 1.0)

    def test_numeric_string_coerced(self):
        self.assertEqual(body_params._clamp01("0.75"), 0.75)

    def test_non_numeric_raises(self):
        with self.assertRaises((ValueError, TypeError)):
            body_params._clamp01("not-a-number")
        with self.assertRaises((ValueError, TypeError)):
            body_params._clamp01(None)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

class TestPresets(unittest.TestCase):

    def test_morph_keys_count(self):
        self.assertEqual(len(body_params.BODY_MORPH_KEYS), 8)

    def test_morph_keys_exact(self):
        self.assertEqual(
            set(body_params.BODY_MORPH_KEYS),
            {"gender", "age", "height", "weight",
             "muscle", "proportions", "bodyfat", "head_size"},
        )

    def test_neutral_adult_all_keys_half(self):
        preset = body_params.BODY_PRESETS["neutral_adult"]
        self.assertEqual(set(preset.keys()), set(body_params.BODY_MORPH_KEYS))
        for key in body_params.BODY_MORPH_KEYS:
            self.assertEqual(preset[key], 0.5, f"{key} should be 0.5")

    def test_other_presets_complete(self):
        """Every preset must define all 8 morph keys."""
        for name, preset in body_params.BODY_PRESETS.items():
            self.assertEqual(
                set(preset.keys()),
                set(body_params.BODY_MORPH_KEYS),
                f"preset {name!r} missing keys",
            )
            for key, value in preset.items():
                self.assertGreaterEqual(value, 0.0, f"{name}.{key}")
                self.assertLessEqual(value, 1.0, f"{name}.{key}")

    def test_extra_presets_exist(self):
        self.assertIn("slim_youth", body_params.BODY_PRESETS)
        self.assertIn("tall_athletic", body_params.BODY_PRESETS)


# ---------------------------------------------------------------------------
# infer_body_overrides (sparse)
# ---------------------------------------------------------------------------

class TestInferBodyOverrides(unittest.TestCase):

    def test_only_emitted_keys_returned(self):
        response = json.dumps({"gender": 0.9, "age": 0.2})
        with patch(MOCK_TARGET, return_value=response):
            result = body_params.infer_body_overrides("a young man")
        self.assertEqual(set(result.keys()), {"gender", "age"})
        self.assertAlmostEqual(result["gender"], 0.9)
        self.assertAlmostEqual(result["age"], 0.2)

    def test_out_of_range_clamped(self):
        response = json.dumps({"height": 5.0, "weight": -2.0})
        with patch(MOCK_TARGET, return_value=response):
            result = body_params.infer_body_overrides("test")
        self.assertAlmostEqual(result["height"], 1.0)
        self.assertAlmostEqual(result["weight"], 0.0)

    def test_unknown_keys_dropped(self):
        response = json.dumps({"gender": 0.5, "favorite_color": "blue", "wingspan": 1.0})
        with patch(MOCK_TARGET, return_value=response):
            result = body_params.infer_body_overrides("test")
        self.assertEqual(set(result.keys()), {"gender"})

    def test_non_numeric_known_key_skipped(self):
        response = json.dumps({"gender": "male", "age": 0.5})
        with patch(MOCK_TARGET, return_value=response):
            result = body_params.infer_body_overrides("test")
        # gender was non-numeric → skipped; age survives
        self.assertEqual(set(result.keys()), {"age"})

    def test_invalid_json_returns_empty(self):
        with patch(MOCK_TARGET, return_value="I cannot help with that."):
            result = body_params.infer_body_overrides("test")
        self.assertEqual(result, {})

    def test_empty_string_returns_empty(self):
        with patch(MOCK_TARGET, return_value=""):
            result = body_params.infer_body_overrides("test")
        self.assertEqual(result, {})

    def test_non_object_json_returns_empty(self):
        with patch(MOCK_TARGET, return_value="[1, 2, 3]"):
            result = body_params.infer_body_overrides("test")
        self.assertEqual(result, {})

    def test_empty_object_returns_empty(self):
        with patch(MOCK_TARGET, return_value="{}"):
            result = body_params.infer_body_overrides("test")
        self.assertEqual(result, {})

    def test_fenced_json_parsed(self):
        response = "```json\n" + json.dumps({"muscle": 0.8}) + "\n```"
        with patch(MOCK_TARGET, return_value=response):
            result = body_params.infer_body_overrides("test")
        self.assertAlmostEqual(result["muscle"], 0.8)

    def test_all_keys_emitted(self):
        full = {k: 0.3 for k in body_params.BODY_MORPH_KEYS}
        with patch(MOCK_TARGET, return_value=json.dumps(full)):
            result = body_params.infer_body_overrides("test")
        self.assertEqual(set(result.keys()), set(body_params.BODY_MORPH_KEYS))


# ---------------------------------------------------------------------------
# resolve_body_morphs (preset + overrides merge)
# ---------------------------------------------------------------------------

class TestResolveBodyMorphs(unittest.TestCase):

    def test_empty_prompt_returns_full_neutral_adult(self):
        with patch(MOCK_TARGET, return_value="{}"):
            result = body_params.resolve_body_morphs("", preset_name="neutral_adult")
        self.assertEqual(set(result.keys()), set(body_params.BODY_MORPH_KEYS))
        for key in body_params.BODY_MORPH_KEYS:
            self.assertAlmostEqual(result[key], 0.5)

    def test_default_preset_when_none(self):
        """preset_name=None must select neutral_adult."""
        with patch(MOCK_TARGET, return_value="{}"):
            result = body_params.resolve_body_morphs("anything", preset_name=None)
        for key in body_params.BODY_MORPH_KEYS:
            self.assertAlmostEqual(result[key], 0.5)

    def test_garbage_prompt_still_reasonable(self):
        """Unparseable Gemini output → full neutral_adult baseline."""
        with patch(MOCK_TARGET, return_value="asdf not json lol"):
            result = body_params.resolve_body_morphs("@#$%^", preset_name=None)
        self.assertEqual(len(result), 8)
        for key in body_params.BODY_MORPH_KEYS:
            self.assertAlmostEqual(result[key], 0.5)

    def test_override_merges_onto_preset(self):
        response = json.dumps({"height": 0.9})
        with patch(MOCK_TARGET, return_value=response):
            result = body_params.resolve_body_morphs("tall", preset_name="neutral_adult")
        # complete 8 keys
        self.assertEqual(set(result.keys()), set(body_params.BODY_MORPH_KEYS))
        # overridden key
        self.assertAlmostEqual(result["height"], 0.9)
        # omitted keys keep preset value
        self.assertAlmostEqual(result["gender"], 0.5)
        self.assertAlmostEqual(result["weight"], 0.5)

    def test_omitted_key_keeps_preset_value(self):
        """A Gemini-omitted key must keep the non-neutral preset's value."""
        response = json.dumps({"height": 0.1})
        with patch(MOCK_TARGET, return_value=response):
            result = body_params.resolve_body_morphs("x", preset_name="tall_athletic")
        # height overridden
        self.assertAlmostEqual(result["height"], 0.1)
        # muscle omitted → keeps tall_athletic's 0.8
        self.assertAlmostEqual(
            result["muscle"],
            body_params.BODY_PRESETS["tall_athletic"]["muscle"],
        )

    def test_preset_name_selects_baseline(self):
        with patch(MOCK_TARGET, return_value="{}"):
            slim = body_params.resolve_body_morphs("", preset_name="slim_youth")
        self.assertEqual(slim, body_params.BODY_PRESETS["slim_youth"])

    def test_result_does_not_mutate_preset(self):
        """resolve must copy the preset, not mutate the module-level dict."""
        response = json.dumps({"gender": 0.0})
        with patch(MOCK_TARGET, return_value=response):
            body_params.resolve_body_morphs("x", preset_name="neutral_adult")
        # neutral_adult.gender must still be 0.5
        self.assertAlmostEqual(
            body_params.BODY_PRESETS["neutral_adult"]["gender"], 0.5
        )

    def test_unknown_preset_raises_key_error(self):
        with patch(MOCK_TARGET, return_value="{}"):
            with self.assertRaises(KeyError):
                body_params.resolve_body_morphs("x", preset_name="does_not_exist")

    def test_result_always_complete(self):
        response = json.dumps({"age": 0.7, "bodyfat": 0.9})
        with patch(MOCK_TARGET, return_value=response):
            result = body_params.resolve_body_morphs("x", preset_name="slim_youth")
        self.assertEqual(set(result.keys()), set(body_params.BODY_MORPH_KEYS))
        self.assertAlmostEqual(result["age"], 0.7)
        self.assertAlmostEqual(result["bodyfat"], 0.9)


# ---------------------------------------------------------------------------
# _call_gemini env guard (no network)
# ---------------------------------------------------------------------------

class TestCallGeminiEnvGuard(unittest.TestCase):

    def test_raises_without_api_key(self):
        env_without_key = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                body_params._call_gemini("p", None, "gemini-2.5-flash")
        self.assertIn("GEMINI_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
