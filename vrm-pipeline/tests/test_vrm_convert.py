"""
tests/test_vrm_convert.py

Smoke / unit tests for render.vrm_convert.glb_to_vrm.

Design goals:
  * No live Blender installation required.
  * Verify the module is importable and exposes the correct public API.
  * Verify that meaningful exceptions are raised when pre-conditions fail
    (missing input file, Blender binary absent, bpy-script reports addon error).
  * Verify the bpy-script helper (_check_vrm_addon_or_raise) raises
    RuntimeError with the expected message when called outside Blender
    (simulated by monkeypatching).

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_vrm_convert
    # or
    python -m unittest discover -s tests
"""

import importlib
import os
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

# A successful (mocked) Blender run must also leave a valid GLB on disk, since
# glb_to_vrm now asserts the output is a real glTF binary before returning.
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
    """render.vrm_convert must import cleanly in a plain-Python environment."""

    def test_module_importable(self):
        mod = importlib.import_module("render.vrm_convert")
        self.assertIsNotNone(mod)

    def test_glb_to_vrm_is_callable(self):
        from render.vrm_convert import glb_to_vrm
        self.assertTrue(callable(glb_to_vrm))

    def test_glb_to_vrm_signature(self):
        """Function must accept glb_path, vrm_path, and optional blender_path."""
        import inspect
        from render.vrm_convert import glb_to_vrm
        sig = inspect.signature(glb_to_vrm)
        params = list(sig.parameters.keys())
        self.assertIn("glb_path", params)
        self.assertIn("vrm_path", params)
        self.assertIn("blender_path", params)


# ---------------------------------------------------------------------------
# Pre-condition checks (no Blender needed)
# ---------------------------------------------------------------------------

class TestGlbToVrmPreconditions(unittest.TestCase):
    """glb_to_vrm must fail fast and clearly on bad inputs."""

    def test_raises_file_not_found_for_missing_glb(self):
        from render.vrm_convert import glb_to_vrm
        with self.assertRaises(FileNotFoundError) as ctx:
            glb_to_vrm("/nonexistent/path/model.glb", "/tmp/out.vrm")
        self.assertIn("model.glb", str(ctx.exception))

    def test_raises_file_not_found_message_contains_path(self):
        from render.vrm_convert import glb_to_vrm
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "missing.glb"
            with self.assertRaises(FileNotFoundError) as ctx:
                glb_to_vrm(str(missing), str(Path(d) / "out.vrm"))
            self.assertIn("missing.glb", str(ctx.exception))


# ---------------------------------------------------------------------------
# Blender binary absent → RuntimeError with informative message
# ---------------------------------------------------------------------------

class TestBlenderBinaryAbsent(unittest.TestCase):
    """
    When the Blender binary does not exist (or is not on PATH), subprocess
    raises FileNotFoundError which glb_to_vrm should propagate or wrap.
    We verify that calling glb_to_vrm with a bogus binary raises an exception
    (FileNotFoundError from subprocess OR RuntimeError from non-zero exit).
    """

    def test_raises_when_blender_binary_missing(self):
        from render.vrm_convert import glb_to_vrm
        with tempfile.TemporaryDirectory() as d:
            # Create a real (empty) GLB file so the pre-condition check passes
            glb = Path(d) / "test.glb"
            glb.write_bytes(b"")
            vrm = Path(d) / "out.vrm"

            with self.assertRaises((FileNotFoundError, RuntimeError, OSError)):
                glb_to_vrm(
                    str(glb),
                    str(vrm),
                    blender_path="/absolutely/nonexistent/blender_binary",
                )


# ---------------------------------------------------------------------------
# VRM addon absent: bpy-level check raises RuntimeError with clear message
# ---------------------------------------------------------------------------

class TestVrmAddonAbsent(unittest.TestCase):
    """
    _check_vrm_addon_or_raise must raise RuntimeError mentioning
    VRM_Addon_for_Blender when no VRM export operator is registered.

    We simulate the Blender environment by temporarily injecting a fake
    `bpy` module that has no VRM export operators.
    """

    def _make_fake_bpy(self):
        """Return a minimal bpy stub with no VRM operators registered."""
        fake_bpy = MagicMock()

        # export_scene namespace exists but has no 'vrm', 'vrm1' attributes
        export_scene_ns = MagicMock(spec=[])  # spec=[] → no attributes
        fake_bpy.ops.export_scene = export_scene_ns

        # vrm namespace also absent
        vrm_ns = MagicMock(spec=[])
        fake_bpy.ops.vrm = vrm_ns

        return fake_bpy

    def test_raises_runtime_error_without_vrm_addon(self):
        """
        Patch bpy into sys.modules and call _check_vrm_addon_or_raise.
        Expect RuntimeError mentioning VRM_Addon_for_Blender.
        """
        from render import vrm_convert

        fake_bpy = self._make_fake_bpy()

        with patch.dict(sys.modules, {"bpy": fake_bpy}):
            # Reload to pick up the patched bpy in module-level detection
            with self.assertRaises(RuntimeError) as ctx:
                # Directly call the helper; it will import bpy internally
                vrm_convert._check_vrm_addon_or_raise()

        self.assertIn("VRM_Addon_for_Blender", str(ctx.exception))

    def test_error_message_contains_install_hint(self):
        """The RuntimeError must include an install hint."""
        from render import vrm_convert

        fake_bpy = self._make_fake_bpy()

        with patch.dict(sys.modules, {"bpy": fake_bpy}):
            with self.assertRaises(RuntimeError) as ctx:
                vrm_convert._check_vrm_addon_or_raise()

        msg = str(ctx.exception)
        # Should mention installation and the addon name
        self.assertTrue(
            "install" in msg.lower() or "VRM_Addon_for_Blender" in msg,
            f"Error message should contain install hint, got: {msg!r}",
        )


# ---------------------------------------------------------------------------
# Subprocess result propagation: non-zero exit → RuntimeError
# ---------------------------------------------------------------------------

class TestSubprocessResultPropagation(unittest.TestCase):
    """
    glb_to_vrm wraps subprocess result; non-zero exit code must produce a
    RuntimeError that includes the exit code.
    """

    def test_non_zero_exit_raises_runtime_error(self):
        from render.vrm_convert import glb_to_vrm

        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stderr = "[vrm_convert] ERROR: VRM addon not available: install VRM_Addon_for_Blender"
        fake_result.stdout = ""

        with tempfile.TemporaryDirectory() as d:
            glb = Path(d) / "model.glb"
            glb.write_bytes(b"")  # file must exist to pass pre-condition
            vrm = Path(d) / "out.vrm"

            with patch("subprocess.run", return_value=fake_result):
                with self.assertRaises(RuntimeError) as ctx:
                    glb_to_vrm(str(glb), str(vrm), blender_path="blender")

            error_msg = str(ctx.exception)
            self.assertIn("1", error_msg)  # exit code mentioned

    def test_runtime_error_includes_stderr_tail(self):
        """stderr content (e.g. VRM addon message) must surface in the exception."""
        from render.vrm_convert import glb_to_vrm

        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stderr = "VRM addon not available: install VRM_Addon_for_Blender"
        fake_result.stdout = ""

        with tempfile.TemporaryDirectory() as d:
            glb = Path(d) / "model.glb"
            glb.write_bytes(b"")
            vrm = Path(d) / "out.vrm"

            with patch("subprocess.run", return_value=fake_result):
                with self.assertRaises(RuntimeError) as ctx:
                    glb_to_vrm(str(glb), str(vrm), blender_path="blender")

            self.assertIn("VRM addon not available", str(ctx.exception))

    def test_successful_conversion_returns_vrm_path(self):
        """When Blender exits 0, glb_to_vrm returns the absolute vrm_path string."""
        from render.vrm_convert import glb_to_vrm

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        fake_result.stdout = "[vrm_convert] VRM written: /tmp/out.vrm"

        with tempfile.TemporaryDirectory() as d:
            glb = Path(d) / "model.glb"
            glb.write_bytes(b"")
            vrm = Path(d) / "out.vrm"

            with patch("subprocess.run", side_effect=_fake_run_writes_glb(fake_result)):
                result = glb_to_vrm(str(glb), str(vrm), blender_path="blender")

            self.assertEqual(result, str(vrm.resolve()))


# ---------------------------------------------------------------------------
# Environment variable BLENDER_PATH fallback
# ---------------------------------------------------------------------------

class TestBlenderPathEnvFallback(unittest.TestCase):
    """
    When blender_path=None, BLENDER_PATH env var must be used; if absent, "blender"
    is the default.
    """

    def test_uses_blender_path_env_var(self):
        from render.vrm_convert import glb_to_vrm

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        fake_result.stdout = ""

        with tempfile.TemporaryDirectory() as d:
            glb = Path(d) / "model.glb"
            glb.write_bytes(b"")
            vrm = Path(d) / "out.vrm"

            with patch("subprocess.run", side_effect=_fake_run_writes_glb(fake_result)) as mock_run, \
                 patch.dict(os.environ, {"BLENDER_PATH": "/custom/blender"}):
                glb_to_vrm(str(glb), str(vrm), blender_path=None)

            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], "/custom/blender")

    def test_defaults_to_blender_when_env_absent(self):
        from render.vrm_convert import glb_to_vrm

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        fake_result.stdout = ""

        env_without_blender = {k: v for k, v in os.environ.items() if k != "BLENDER_PATH"}

        with tempfile.TemporaryDirectory() as d:
            glb = Path(d) / "model.glb"
            glb.write_bytes(b"")
            vrm = Path(d) / "out.vrm"

            with patch("subprocess.run", side_effect=_fake_run_writes_glb(fake_result)) as mock_run, \
                 patch.dict(os.environ, env_without_blender, clear=True):
                glb_to_vrm(str(glb), str(vrm), blender_path=None)

            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], "blender")


if __name__ == "__main__":
    unittest.main()
