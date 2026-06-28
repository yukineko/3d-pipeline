"""
tests/test_generate_body.py

Smoke / unit tests for render.generate_body.

Design goals (mirroring tests/test_vrm_edit.py + test_subprocess_timeout.py):
  * No live Blender / MPFB2 installation required — the module must import in a
    bare environment with no top-level bpy/mpfb import.
  * MODIFIER_MAP keys exactly cover body_params.BODY_MORPH_KEYS.
  * The module integrates with vrm_bone_map (no hardcoded conflicting table).
  * The generate_body() host wrapper builds the right Blender command, serializes
    morphs to a temp JSON, passes a timeout to subprocess.run, and propagates
    errors (timeout -> RuntimeError, non-zero exit -> RuntimeError, missing
    binary -> FileNotFoundError/RuntimeError/OSError).

The bpy helpers (_create_human, _apply_morphs, _attach_rig, _assign_vrm_humanoid,
_bake_and_export, _enable_addons_or_raise) require Blender and are NOT invoked.

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_generate_body
    # or
    python -m unittest discover -s tests
"""

import importlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make vrm-pipeline/ the first entry so `render.*` resolves correctly.
HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _fake_ok_result(stdout="", stderr=""):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = stdout
    fake.stderr = stderr
    return fake


# ---------------------------------------------------------------------------
# Import guard: importable without bpy/mpfb present.
# ---------------------------------------------------------------------------

class TestImport(unittest.TestCase):
    """render.generate_body must import cleanly in plain Python (no bpy/mpfb)."""

    def test_module_importable(self):
        mod = importlib.import_module("render.generate_body")
        self.assertIsNotNone(mod)

    def test_no_top_level_bpy_import(self):
        """The module source must not import bpy/mpfb at top level."""
        src = (Path(PIPELINE_ROOT) / "render" / "generate_body.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            # Top-level (column 0) import statements only.
            if line == stripped and (stripped.startswith("import ") or stripped.startswith("from ")):
                self.assertNotIn("bpy", stripped, f"top-level bpy import found: {stripped!r}")
                self.assertFalse(
                    stripped.startswith("import mpfb") or stripped.startswith("from mpfb"),
                    f"top-level mpfb import found: {stripped!r}",
                )

    def test_inside_blender_flag_false_in_plain_python(self):
        import render.generate_body as mod
        self.assertFalse(mod._INSIDE_BLENDER)

    def test_generate_body_is_callable(self):
        from render.generate_body import generate_body
        self.assertTrue(callable(generate_body))

    def test_generate_body_signature(self):
        from render.generate_body import generate_body
        params = list(inspect.signature(generate_body).parameters.keys())
        self.assertIn("morphs", params)
        self.assertIn("out_vrm", params)
        self.assertIn("blender_path", params)
        self.assertIn("report_file", params)

    def test_blender_path_optional(self):
        from render.generate_body import generate_body
        self.assertIs(inspect.signature(generate_body).parameters["blender_path"].default, None)


# ---------------------------------------------------------------------------
# MODIFIER_MAP / vrm_bone_map consistency (pure data).
# ---------------------------------------------------------------------------

class TestModifierMap(unittest.TestCase):
    """MODIFIER_MAP must cover exactly the canonical BODY_MORPH_KEYS."""

    def test_keys_cover_body_morph_keys_exactly(self):
        from render.generate_body import MODIFIER_MAP
        from render.body_params import BODY_MORPH_KEYS
        self.assertEqual(set(MODIFIER_MAP.keys()), set(BODY_MORPH_KEYS))

    def test_values_are_non_empty_strings(self):
        from render.generate_body import MODIFIER_MAP
        for key, target in MODIFIER_MAP.items():
            self.assertIsInstance(target, str, f"target for {key!r} must be a string")
            self.assertTrue(target, f"target for {key!r} must be non-empty")

    def test_bodyfat_overlaps_weight_target(self):
        """bodyfat documented as overlapping the universal Weight macro."""
        from render.generate_body import MODIFIER_MAP
        self.assertEqual(MODIFIER_MAP["bodyfat"], MODIFIER_MAP["weight"])


class TestBoneMapIntegration(unittest.TestCase):
    """generate_body must use vrm_bone_map, not a hardcoded conflicting table."""

    def test_imports_mpfb_to_vrm_humanoid(self):
        import render.generate_body as mod
        from render.vrm_bone_map import MPFB_TO_VRM_HUMANOID
        # The module references the shared mapping object (identity), proving it
        # does not maintain a divergent copy.
        self.assertIs(mod.MPFB_TO_VRM_HUMANOID, MPFB_TO_VRM_HUMANOID)

    def test_uses_missing_required_slots_helper(self):
        import render.generate_body as mod
        from render import vrm_bone_map
        self.assertIs(mod.missing_required_slots, vrm_bone_map.missing_required_slots)


# ---------------------------------------------------------------------------
# Host wrapper: command structure + temp-file serialization + success path.
# ---------------------------------------------------------------------------

class TestGenerateBodyCommand(unittest.TestCase):
    """generate_body builds the right Blender command and returns the out path."""

    def test_command_structure_and_morphs_file(self):
        from render.generate_body import generate_body

        captured = {"cmd": None, "kwargs": None, "morphs": None}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
            idx = cmd.index("--morphs-file")
            with open(cmd[idx + 1], "r", encoding="utf-8") as f:
                captured["morphs"] = json.load(f)
            return _fake_ok_result()

        morphs = {"gender": 0.0, "height": 0.8, "muscle": 0.6}

        with tempfile.TemporaryDirectory() as d:
            out_vrm = Path(d) / "char.vrm"
            with patch("subprocess.run", side_effect=fake_run):
                result = generate_body(morphs, str(out_vrm), blender_path="blender")

        cmd = captured["cmd"]
        self.assertEqual(cmd[0], "blender")
        self.assertIn("--background", cmd)
        self.assertIn("--python", cmd)
        # script after --python is generate_body.py
        script = cmd[cmd.index("--python") + 1]
        self.assertTrue(script.endswith("generate_body.py"), script)
        # separator + flags
        self.assertIn("--", cmd)
        self.assertIn("--morphs-file", cmd)
        self.assertIn("--out", cmd)
        # --out points at the resolved output path
        self.assertEqual(cmd[cmd.index("--out") + 1], str(Path(out_vrm).resolve()))
        # morphs were serialized to the temp JSON
        self.assertEqual(captured["morphs"], morphs)
        # timeout kwarg passed to subprocess.run
        self.assertIn("timeout", captured["kwargs"])
        # returns absolute out path str
        self.assertEqual(result, str(Path(out_vrm).resolve()))

    def test_report_file_flag_passed_when_given(self):
        from render.generate_body import generate_body

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return _fake_ok_result()

        with tempfile.TemporaryDirectory() as d:
            out_vrm = Path(d) / "char.vrm"
            report = Path(d) / "report.json"
            with patch("subprocess.run", side_effect=fake_run):
                generate_body({"gender": 0.5}, str(out_vrm), blender_path="blender",
                              report_file=str(report))

        self.assertIn("--report-file", captured["cmd"])
        self.assertEqual(captured["cmd"][captured["cmd"].index("--report-file") + 1], str(report))

    def test_report_file_flag_absent_by_default(self):
        from render.generate_body import generate_body

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return _fake_ok_result()

        with tempfile.TemporaryDirectory() as d:
            out_vrm = Path(d) / "char.vrm"
            with patch("subprocess.run", side_effect=fake_run):
                generate_body({"gender": 0.5}, str(out_vrm), blender_path="blender")

        self.assertNotIn("--report-file", captured["cmd"])

    def test_blender_path_env_fallback(self):
        from render.generate_body import generate_body

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return _fake_ok_result()

        with tempfile.TemporaryDirectory() as d:
            out_vrm = Path(d) / "char.vrm"
            with patch("subprocess.run", side_effect=fake_run), \
                 patch.dict(os.environ, {"BLENDER_PATH": "/custom/blender"}):
                generate_body({"gender": 0.5}, str(out_vrm), blender_path=None)

        self.assertEqual(captured["cmd"][0], "/custom/blender")

    def test_temp_morphs_file_cleaned_up(self):
        from render.generate_body import generate_body

        created = []
        original = tempfile.NamedTemporaryFile

        def tracking(**kwargs):
            f = original(**kwargs)
            created.append(f.name)
            return f

        with tempfile.TemporaryDirectory() as d:
            out_vrm = Path(d) / "char.vrm"
            with patch("subprocess.run", side_effect=lambda cmd, **kw: _fake_ok_result()), \
                 patch("tempfile.NamedTemporaryFile", side_effect=tracking):
                generate_body({"gender": 0.5}, str(out_vrm), blender_path="blender")

        self.assertTrue(created)
        for path in created:
            self.assertFalse(Path(path).exists(), f"temp file {path} not cleaned up")


# ---------------------------------------------------------------------------
# Host wrapper: error propagation.
# ---------------------------------------------------------------------------

class TestGenerateBodyErrors(unittest.TestCase):
    """Timeout / non-zero / missing-binary surface as clear errors."""

    def test_timeout_raises_runtime_error(self):
        from render.generate_body import generate_body

        def boom(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=600)

        with tempfile.TemporaryDirectory() as d:
            out_vrm = Path(d) / "char.vrm"
            with patch("subprocess.run", side_effect=boom) as m:
                with self.assertRaises(RuntimeError) as ctx:
                    generate_body({"gender": 0.5}, str(out_vrm), blender_path="blender")
            self.assertIn("timed out", str(ctx.exception).lower())
            self.assertIn("timeout", m.call_args.kwargs)

    def test_temp_file_cleaned_up_after_timeout(self):
        from render.generate_body import generate_body

        created = []
        original = tempfile.NamedTemporaryFile

        def tracking(**kwargs):
            f = original(**kwargs)
            created.append(f.name)
            return f

        def boom(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=600)

        with tempfile.TemporaryDirectory() as d:
            out_vrm = Path(d) / "char.vrm"
            with patch("subprocess.run", side_effect=boom), \
                 patch("tempfile.NamedTemporaryFile", side_effect=tracking):
                with self.assertRaises(RuntimeError):
                    generate_body({"gender": 0.5}, str(out_vrm), blender_path="blender")

        for path in created:
            self.assertFalse(Path(path).exists(), f"temp file {path} not cleaned up")

    def test_non_zero_exit_raises_runtime_error(self):
        from render.generate_body import generate_body

        fake = MagicMock()
        fake.returncode = 1
        fake.stderr = "[generate_body] ERROR: MPFB2 not available"
        fake.stdout = ""

        with tempfile.TemporaryDirectory() as d:
            out_vrm = Path(d) / "char.vrm"
            with patch("subprocess.run", return_value=fake):
                with self.assertRaises(RuntimeError) as ctx:
                    generate_body({"gender": 0.5}, str(out_vrm), blender_path="blender")
            msg = str(ctx.exception)
            self.assertIn("1", msg)
            self.assertIn("MPFB2 not available", msg)

    def test_missing_blender_binary_surfaces_error(self):
        from render.generate_body import generate_body

        with tempfile.TemporaryDirectory() as d:
            out_vrm = Path(d) / "char.vrm"
            with patch("subprocess.run", side_effect=FileNotFoundError("no blender")):
                with self.assertRaises((FileNotFoundError, RuntimeError, OSError)):
                    generate_body({"gender": 0.5}, str(out_vrm),
                                  blender_path="/absolutely/nonexistent/blender")


if __name__ == "__main__":
    unittest.main()
