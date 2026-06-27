"""
tests/test_vrm_edit.py

Smoke / unit tests for render.vrm_edit.edit_vrm.

Design goals:
  * No live Blender installation required.
  * Verify the module is importable and exposes the correct public API.
  * Verify that meaningful exceptions are raised when pre-conditions fail
    (missing input file, Blender binary absent, bpy-script reports addon error).
  * Verify the bpy-script helper (_check_vrm_addon_or_raise) raises
    RuntimeError with the expected message when called outside Blender
    (simulated by monkeypatching).
  * Verify adjustments are serialized to a temp JSON file and passed via
    --adjustments-file flag.

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_vrm_edit
    # or
    python -m unittest discover -s tests
"""

import importlib
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Make vrm-pipeline/ the first entry so `render.*` resolves correctly.
HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# A successful (mocked) Blender run must also leave a valid GLB on disk, since
# edit_vrm now asserts the output is a real glTF binary before returning.
from glb_fixtures import write_glb_for_out_flag


def _fake_run_writes_glb(fake_result):
    """subprocess.run side_effect: simulate Blender writing --out, then succeed."""
    def _run(cmd, **kwargs):
        write_glb_for_out_flag(cmd)
        return fake_result
    return _run


# ---------------------------------------------------------------------------
# Import guard: the module must be importable without bpy/Blender present.
# ---------------------------------------------------------------------------

class TestImport(unittest.TestCase):
    """render.vrm_edit must import cleanly in a plain-Python environment."""

    def test_module_importable(self):
        mod = importlib.import_module("render.vrm_edit")
        self.assertIsNotNone(mod)

    def test_edit_vrm_is_callable(self):
        from render.vrm_edit import edit_vrm
        self.assertTrue(callable(edit_vrm))

    def test_edit_vrm_signature(self):
        """Function must accept in_vrm, out_vrm, adjustments, and optional blender_path."""
        from render.vrm_edit import edit_vrm
        sig = inspect.signature(edit_vrm)
        params = list(sig.parameters.keys())
        self.assertIn("in_vrm", params)
        self.assertIn("out_vrm", params)
        self.assertIn("adjustments", params)
        self.assertIn("blender_path", params)

    def test_edit_vrm_blender_path_is_optional(self):
        """blender_path must default to None."""
        from render.vrm_edit import edit_vrm
        sig = inspect.signature(edit_vrm)
        param = sig.parameters["blender_path"]
        self.assertIs(param.default, None)

    def test_inside_blender_flag_is_false_in_plain_python(self):
        """_INSIDE_BLENDER must be False when bpy is not installed."""
        import render.vrm_edit as mod
        self.assertFalse(mod._INSIDE_BLENDER)


# ---------------------------------------------------------------------------
# Pre-condition checks (no Blender needed)
# ---------------------------------------------------------------------------

class TestEditVrmPreconditions(unittest.TestCase):
    """edit_vrm must fail fast and clearly on bad inputs."""

    def test_raises_file_not_found_for_missing_vrm(self):
        from render.vrm_edit import edit_vrm
        with self.assertRaises(FileNotFoundError) as ctx:
            edit_vrm("/nonexistent/path/model.vrm", "/tmp/out.vrm", {})
        self.assertIn("model.vrm", str(ctx.exception))

    def test_raises_file_not_found_message_contains_path(self):
        from render.vrm_edit import edit_vrm
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "missing.vrm"
            with self.assertRaises(FileNotFoundError) as ctx:
                edit_vrm(str(missing), str(Path(d) / "out.vrm"), {})
            self.assertIn("missing.vrm", str(ctx.exception))

    def test_error_message_mentions_input_file(self):
        from render.vrm_edit import edit_vrm
        with self.assertRaises(FileNotFoundError) as ctx:
            edit_vrm("/some/path/avatar.vrm", "/tmp/out.vrm", {})
        self.assertIn("avatar.vrm", str(ctx.exception))


# ---------------------------------------------------------------------------
# Blender binary absent → exception raised
# ---------------------------------------------------------------------------

class TestBlenderBinaryAbsent(unittest.TestCase):
    """
    When the Blender binary does not exist (or is not on PATH), subprocess
    raises FileNotFoundError which edit_vrm should propagate or wrap.
    """

    def test_raises_when_blender_binary_missing(self):
        from render.vrm_edit import edit_vrm
        with tempfile.TemporaryDirectory() as d:
            # Create a real (empty) VRM file so the pre-condition check passes
            vrm_in = Path(d) / "test.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with self.assertRaises((FileNotFoundError, RuntimeError, OSError)):
                edit_vrm(
                    str(vrm_in),
                    str(vrm_out),
                    {},
                    blender_path="/absolutely/nonexistent/blender_binary",
                )


# ---------------------------------------------------------------------------
# Adjustments temp file pass-through
# ---------------------------------------------------------------------------

class TestAdjustmentsTempFile(unittest.TestCase):
    """
    edit_vrm must serialize adjustments to a temp JSON file and pass
    --adjustments-file to Blender. The temp file must be cleaned up after.
    """

    def test_adjustments_passed_via_temp_file(self):
        """Subprocess command must include --adjustments-file flag."""
        from render.vrm_edit import edit_vrm

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        fake_result.stdout = ""

        adjustments = {
            "expressions": {"happy": 1.0},
            "height_scale": 1.1,
        }

        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            write_glb_for_out_flag(cmd)
            return fake_result

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run", side_effect=fake_run):
                edit_vrm(str(vrm_in), str(vrm_out), adjustments, blender_path="blender")

        self.assertIn("--adjustments-file", captured_cmd)

    def test_adjustments_file_contains_valid_json(self):
        """The temp file written for Blender must contain valid JSON with the adjustments."""
        from render.vrm_edit import edit_vrm

        adjustments = {
            "expressions": {"sad": 0.5, "blink": 0.8},
            "materials": {"hair": [0.2, 0.1, 0.05, 1.0]},
        }

        written_content = {}

        def fake_run(cmd, **kwargs):
            # Extract the adjustments-file path from cmd
            try:
                idx = cmd.index("--adjustments-file")
                adj_file = cmd[idx + 1]
                with open(adj_file, "r", encoding="utf-8") as f:
                    written_content.update(json.load(f))
            except (ValueError, IndexError, FileNotFoundError):
                pass

            write_glb_for_out_flag(cmd)

            fake_result = MagicMock()
            fake_result.returncode = 0
            fake_result.stderr = ""
            fake_result.stdout = ""
            return fake_result

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run", side_effect=fake_run):
                edit_vrm(str(vrm_in), str(vrm_out), adjustments, blender_path="blender")

        self.assertEqual(written_content.get("expressions"), {"sad": 0.5, "blink": 0.8})
        self.assertEqual(written_content.get("materials"), {"hair": [0.2, 0.1, 0.05, 1.0]})

    def test_temp_file_cleaned_up_after_success(self):
        """The adjustments temp file must be deleted after Blender exits successfully."""
        from render.vrm_edit import edit_vrm

        temp_files_created = []

        original_NamedTemporaryFile = tempfile.NamedTemporaryFile

        def tracking_tempfile(**kwargs):
            f = original_NamedTemporaryFile(**kwargs)
            temp_files_created.append(f.name)
            return f

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        fake_result.stdout = ""

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run", side_effect=_fake_run_writes_glb(fake_result)), \
                 patch("tempfile.NamedTemporaryFile", side_effect=tracking_tempfile):
                edit_vrm(str(vrm_in), str(vrm_out), {}, blender_path="blender")

        for path in temp_files_created:
            self.assertFalse(
                Path(path).exists(),
                f"Temp file {path} was not cleaned up after successful run",
            )

    def test_temp_file_cleaned_up_after_failure(self):
        """The adjustments temp file must be deleted even when Blender fails."""
        from render.vrm_edit import edit_vrm

        temp_files_created = []

        original_NamedTemporaryFile = tempfile.NamedTemporaryFile

        def tracking_tempfile(**kwargs):
            f = original_NamedTemporaryFile(**kwargs)
            temp_files_created.append(f.name)
            return f

        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stderr = "ERROR: something went wrong"
        fake_result.stdout = ""

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run", return_value=fake_result), \
                 patch("tempfile.NamedTemporaryFile", side_effect=tracking_tempfile):
                with self.assertRaises(RuntimeError):
                    edit_vrm(str(vrm_in), str(vrm_out), {}, blender_path="blender")

        for path in temp_files_created:
            self.assertFalse(
                Path(path).exists(),
                f"Temp file {path} was not cleaned up after failed run",
            )


# ---------------------------------------------------------------------------
# VRM addon absent: bpy-level check raises RuntimeError with clear message
# ---------------------------------------------------------------------------

class TestVrmAddonAbsent(unittest.TestCase):
    """
    _check_vrm_addon_or_raise must raise RuntimeError mentioning
    VRM_Addon_for_Blender when the import_scene.vrm operator is not registered.

    We simulate the Blender environment by temporarily injecting a fake
    `bpy` module that has no VRM import operators.
    """

    def _make_fake_bpy_no_vrm(self):
        """Return a minimal bpy stub with no VRM import operators registered."""
        fake_bpy = MagicMock()

        # import_scene namespace exists but has no 'vrm' attribute
        import_scene_ns = MagicMock(spec=[])  # spec=[] → no attributes
        fake_bpy.ops.import_scene = import_scene_ns

        return fake_bpy

    def test_raises_runtime_error_without_vrm_addon(self):
        """
        Patch bpy into sys.modules and call _check_vrm_addon_or_raise.
        Expect RuntimeError mentioning VRM_Addon_for_Blender.
        """
        from render import vrm_edit

        fake_bpy = self._make_fake_bpy_no_vrm()

        with patch.dict(sys.modules, {"bpy": fake_bpy}):
            with self.assertRaises(RuntimeError) as ctx:
                vrm_edit._check_vrm_addon_or_raise()

        self.assertIn("VRM_Addon_for_Blender", str(ctx.exception))

    def test_error_message_contains_install_hint(self):
        """The RuntimeError must include an install hint."""
        from render import vrm_edit

        fake_bpy = self._make_fake_bpy_no_vrm()

        with patch.dict(sys.modules, {"bpy": fake_bpy}):
            with self.assertRaises(RuntimeError) as ctx:
                vrm_edit._check_vrm_addon_or_raise()

        msg = str(ctx.exception)
        self.assertTrue(
            "install" in msg.lower() or "VRM_Addon_for_Blender" in msg,
            f"Error message should contain install hint, got: {msg!r}",
        )

    def test_error_message_contains_url(self):
        """The RuntimeError must include the addon URL."""
        from render import vrm_edit

        fake_bpy = self._make_fake_bpy_no_vrm()

        with patch.dict(sys.modules, {"bpy": fake_bpy}):
            with self.assertRaises(RuntimeError) as ctx:
                vrm_edit._check_vrm_addon_or_raise()

        msg = str(ctx.exception)
        self.assertIn("vrm-addon-for-blender.info", msg)


# ---------------------------------------------------------------------------
# Subprocess result propagation: non-zero exit → RuntimeError
# ---------------------------------------------------------------------------

class TestSubprocessResultPropagation(unittest.TestCase):
    """
    edit_vrm wraps subprocess result; non-zero exit code must produce a
    RuntimeError that includes the exit code.
    """

    def test_non_zero_exit_raises_runtime_error(self):
        from render.vrm_edit import edit_vrm

        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stderr = "[vrm_edit] ERROR: VRM addon not available: install VRM_Addon_for_Blender"
        fake_result.stdout = ""

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run", return_value=fake_result):
                with self.assertRaises(RuntimeError) as ctx:
                    edit_vrm(str(vrm_in), str(vrm_out), {}, blender_path="blender")

            error_msg = str(ctx.exception)
            self.assertIn("1", error_msg)  # exit code mentioned

    def test_runtime_error_includes_stderr_tail(self):
        """stderr content (e.g. VRM addon message) must surface in the exception."""
        from render.vrm_edit import edit_vrm

        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stderr = "VRM addon not available: install VRM_Addon_for_Blender"
        fake_result.stdout = ""

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run", return_value=fake_result):
                with self.assertRaises(RuntimeError) as ctx:
                    edit_vrm(str(vrm_in), str(vrm_out), {}, blender_path="blender")

            self.assertIn("VRM addon not available", str(ctx.exception))

    def test_successful_edit_returns_vrm_path(self):
        """When Blender exits 0, edit_vrm returns the absolute out_vrm path string."""
        from render.vrm_edit import edit_vrm

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        fake_result.stdout = "[vrm_edit] VRM written: /tmp/out.vrm"

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run", side_effect=_fake_run_writes_glb(fake_result)):
                result = edit_vrm(str(vrm_in), str(vrm_out), {}, blender_path="blender")

            self.assertEqual(result, str(vrm_out.resolve()))


# ---------------------------------------------------------------------------
# Environment variable BLENDER_PATH fallback
# ---------------------------------------------------------------------------

class TestBlenderPathEnvFallback(unittest.TestCase):
    """
    When blender_path=None, BLENDER_PATH env var must be used; if absent, "blender"
    is the default.
    """

    def test_uses_blender_path_env_var(self):
        from render.vrm_edit import edit_vrm

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        fake_result.stdout = ""

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run", side_effect=_fake_run_writes_glb(fake_result)) as mock_run, \
                 patch.dict(os.environ, {"BLENDER_PATH": "/custom/blender"}):
                edit_vrm(str(vrm_in), str(vrm_out), {}, blender_path=None)

            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], "/custom/blender")

    def test_defaults_to_blender_when_env_absent(self):
        from render.vrm_edit import edit_vrm

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        fake_result.stdout = ""

        env_without_blender = {k: v for k, v in os.environ.items() if k != "BLENDER_PATH"}

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run", side_effect=_fake_run_writes_glb(fake_result)) as mock_run, \
                 patch.dict(os.environ, env_without_blender, clear=True):
                edit_vrm(str(vrm_in), str(vrm_out), {}, blender_path=None)

            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], "blender")


# ---------------------------------------------------------------------------
# Blender command structure
# ---------------------------------------------------------------------------

class TestBlenderCommandStructure(unittest.TestCase):
    """
    edit_vrm must call Blender with --background --python <script> -- --in --out --adjustments-file.
    """

    def _run_edit_vrm_with_fake_blender(self, adjustments=None):
        from render.vrm_edit import edit_vrm
        import render.vrm_edit as mod

        if adjustments is None:
            adjustments = {}

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        fake_result.stdout = ""

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            write_glb_for_out_flag(cmd)
            return fake_result

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run", side_effect=fake_run):
                edit_vrm(str(vrm_in), str(vrm_out), adjustments, blender_path="blender")

        return captured["cmd"]

    def test_blender_background_flag(self):
        cmd = self._run_edit_vrm_with_fake_blender()
        self.assertIn("--background", cmd)

    def test_blender_python_flag(self):
        cmd = self._run_edit_vrm_with_fake_blender()
        self.assertIn("--python", cmd)

    def test_blender_script_is_vrm_edit_py(self):
        from render import vrm_edit as mod
        cmd = self._run_edit_vrm_with_fake_blender()
        idx = cmd.index("--python")
        script = cmd[idx + 1]
        self.assertTrue(
            script.endswith("vrm_edit.py"),
            f"Expected script to end with vrm_edit.py, got: {script}",
        )

    def test_separator_present(self):
        cmd = self._run_edit_vrm_with_fake_blender()
        self.assertIn("--", cmd)

    def test_in_flag_present(self):
        cmd = self._run_edit_vrm_with_fake_blender()
        self.assertIn("--in", cmd)

    def test_out_flag_present(self):
        cmd = self._run_edit_vrm_with_fake_blender()
        self.assertIn("--out", cmd)


# ---------------------------------------------------------------------------
# Material matching heuristic
# ---------------------------------------------------------------------------

class TestMaterialMatchHeuristic(unittest.TestCase):
    """_material_matches_category must classify materials by name substring."""

    def test_hair_material(self):
        from render.vrm_edit import _material_matches_category
        self.assertTrue(_material_matches_category("Hair_01", "hair"))
        self.assertTrue(_material_matches_category("Material_hair", "hair"))

    def test_skin_material(self):
        from render.vrm_edit import _material_matches_category
        self.assertTrue(_material_matches_category("Body_skin", "skin"))
        self.assertTrue(_material_matches_category("Face_skin", "skin"))

    def test_eye_material(self):
        from render.vrm_edit import _material_matches_category
        self.assertTrue(_material_matches_category("EyeIris", "eye"))
        self.assertTrue(_material_matches_category("eyes_left", "eye"))

    def test_outfit_material(self):
        from render.vrm_edit import _material_matches_category
        self.assertTrue(_material_matches_category("outfit_01", "outfit"))
        self.assertTrue(_material_matches_category("Clothes", "outfit"))

    def test_no_match(self):
        from render.vrm_edit import _material_matches_category
        self.assertFalse(_material_matches_category("UnknownMaterial", "hair"))
        self.assertFalse(_material_matches_category("Metallic", "skin"))


# ---------------------------------------------------------------------------
# _run_vrm_export fallback chain
# ---------------------------------------------------------------------------

class TestRunVrmExportFallback(unittest.TestCase):
    """
    _run_vrm_export tries export_scene.vrm → export_scene.vrm1 → vrm.export_scene
    in order, using the first available operator.
    """

    def _make_fake_bpy_with_export_op(self, namespace, name, result=None):
        """Create a fake bpy where only ops.<namespace>.<name> is available."""
        fake_bpy = MagicMock()

        if result is None:
            result = {"FINISHED"}

        mock_op = MagicMock(return_value=result)

        # All namespaces are MagicMock by default (no spec restriction)
        # but we need the specific op to be reachable
        export_ns = MagicMock()
        setattr(export_ns, name, mock_op)

        setattr(fake_bpy.ops, namespace, export_ns)

        # Make other namespaces/attributes return MagicMock with no relevant ops
        return fake_bpy, mock_op

    def test_uses_export_scene_vrm_first(self):
        """_run_vrm_export should prefer export_scene.vrm."""
        from render import vrm_edit

        fake_bpy, mock_op = self._make_fake_bpy_with_export_op("export_scene", "vrm")

        with patch.dict(sys.modules, {"bpy": fake_bpy}):
            result = vrm_edit._run_vrm_export("/tmp/out.vrm")

        mock_op.assert_called_once_with(filepath="/tmp/out.vrm")

    def test_raises_runtime_error_when_no_export_op(self):
        """_run_vrm_export raises RuntimeError when no export operator is found."""
        from render import vrm_edit

        fake_bpy = MagicMock()
        # All op attributes return MagicMock but calling them raises TypeError
        fake_bpy.ops.export_scene.vrm.side_effect = Exception("no op")
        fake_bpy.ops.export_scene.vrm1.side_effect = Exception("no op")
        fake_bpy.ops.vrm.export_scene.side_effect = Exception("no op")

        with patch.dict(sys.modules, {"bpy": fake_bpy}):
            with self.assertRaises(RuntimeError) as ctx:
                vrm_edit._run_vrm_export("/tmp/out.vrm")

        self.assertIn("VRM_Addon_for_Blender", str(ctx.exception))


class TestSanitizeHeightScale(unittest.TestCase):
    """_sanitize_height_scale clamps valid factors and rejects unsafe ones.

    edit_vrm() is a public API; a 0/negative/NaN/inf height_scale must not reach
    bpy (it would collapse or invert the avatar). This helper is pure (no bpy).
    """

    def test_in_range_value_unchanged(self):
        from render.vrm_edit import _sanitize_height_scale
        self.assertEqual(_sanitize_height_scale(1.5), 1.5)

    def test_too_large_clamped_to_max(self):
        from render.vrm_edit import _sanitize_height_scale, _HEIGHT_SCALE_MAX
        self.assertEqual(_sanitize_height_scale(5.0), _HEIGHT_SCALE_MAX)

    def test_too_small_clamped_to_min(self):
        from render.vrm_edit import _sanitize_height_scale, _HEIGHT_SCALE_MIN
        self.assertEqual(_sanitize_height_scale(0.2), _HEIGHT_SCALE_MIN)

    def test_zero_rejected(self):
        from render.vrm_edit import _sanitize_height_scale
        with self.assertRaises(ValueError):
            _sanitize_height_scale(0)

    def test_negative_rejected(self):
        from render.vrm_edit import _sanitize_height_scale
        with self.assertRaises(ValueError):
            _sanitize_height_scale(-1.0)

    def test_nan_rejected(self):
        from render.vrm_edit import _sanitize_height_scale
        with self.assertRaises(ValueError):
            _sanitize_height_scale(float("nan"))

    def test_inf_rejected(self):
        from render.vrm_edit import _sanitize_height_scale
        with self.assertRaises(ValueError):
            _sanitize_height_scale(float("inf"))


if __name__ == "__main__":
    unittest.main()
