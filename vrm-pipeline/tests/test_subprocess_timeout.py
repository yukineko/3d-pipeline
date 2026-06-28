"""
tests/test_subprocess_timeout.py

Verifies that every ``subprocess.run`` call site which previously lacked a
``timeout`` now:

  (a) passes a ``timeout`` keyword argument to ``subprocess.run``, and
  (b) converts a ``subprocess.TimeoutExpired`` into the function's existing
      error channel:
        * raise-type functions  -> raise RuntimeError
        * tuple-return functions -> return a non-zero (124) return code.

No live Blender / ledger binary is required: ``subprocess.run`` is patched in
each module under test.  Modules must remain importable without ``bpy``.

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_subprocess_timeout
    # or
    python -m unittest discover -s tests
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Make vrm-pipeline/ the first entry so top-level and `render.*` modules resolve.
HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _timeout_exc():
    return subprocess.TimeoutExpired(cmd=["x"], timeout=600)


# ---------------------------------------------------------------------------
# Tuple-return sites: print a WARN and return a non-zero (124) return code.
# ---------------------------------------------------------------------------

class TestPipelineRunTimeout(unittest.TestCase):
    """pipeline._run -> (stdout, stderr, returncode)."""

    def test_passes_timeout_and_returns_124(self):
        import pipeline

        with patch.object(pipeline.subprocess, "run", side_effect=_timeout_exc()) as m:
            stdout, stderr, rc = pipeline._run(["echo", "hi"], label="t")

        # (a) timeout kwarg present
        self.assertIn("timeout", m.call_args.kwargs)
        # (b) tuple-return: non-zero (124) returncode
        self.assertEqual(rc, 124)
        self.assertEqual(stdout, "")


class TestCalibrateRunTimeout(unittest.TestCase):
    """calibrate._run -> (stdout, stderr, returncode); existing except tuple."""

    def test_passes_timeout_and_returns_nonzero(self):
        # calibrate.py hard-requires imagehash/Pillow at import (sys.exit(1)
        # otherwise).  Stub them so the module is importable in CI without the
        # optional rendering deps; we only exercise the pure-Python _run helper.
        import types

        stubs = {}
        for name in ("imagehash", "PIL", "PIL.Image"):
            if name not in sys.modules:
                stubs[name] = sys.modules.get(name)
                sys.modules[name] = types.ModuleType(name)
        try:
            import calibrate
        finally:
            for name, prev in stubs.items():
                if prev is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = prev

        with patch.object(calibrate.subprocess, "run", side_effect=_timeout_exc()) as m:
            stdout, stderr, rc = calibrate._run(["echo", "hi"], label="t")

        self.assertIn("timeout", m.call_args.kwargs)
        # (b) calibrate's existing handler returns 1 (non-zero) for the tuple.
        self.assertNotEqual(rc, 0)


class TestRunBlenderTimeout(unittest.TestCase):
    """generate.run_blender -> (returncode, stdout, stderr)."""

    def test_passes_timeout_and_returns_124(self):
        import generate

        with patch.object(generate.subprocess, "run", side_effect=_timeout_exc()) as m:
            rc, stdout, stderr = generate.run_blender(
                "blender", "script.py", "/tmp/out.glb"
            )

        self.assertIn("timeout", m.call_args.kwargs)
        # (b) tuple-return: non-zero (124) returncode in first position
        self.assertEqual(rc, 124)


# ---------------------------------------------------------------------------
# Raise-type sites: convert TimeoutExpired into RuntimeError.
# ---------------------------------------------------------------------------

class TestDeriveGetParentRecordTimeout(unittest.TestCase):
    """derive.get_parent_record raises RuntimeError on failure."""

    def test_passes_timeout_and_raises_runtimeerror(self):
        import derive

        with patch.object(derive.subprocess, "run", side_effect=_timeout_exc()) as m:
            with self.assertRaises(RuntimeError):
                derive.get_parent_record(Path("/tmp/ledger.db"), "rec123")

        self.assertIn("timeout", m.call_args.kwargs)


class TestVrmEditTimeout(unittest.TestCase):
    """render.vrm_edit.edit_vrm raises RuntimeError on Blender failure."""

    def test_passes_timeout_and_raises_runtimeerror(self):
        from render import vrm_edit

        with tempfile.NamedTemporaryFile(suffix=".vrm", delete=False) as tf:
            in_vrm = tf.name
        out_vrm = in_vrm.replace(".vrm", ".out.vrm")

        with patch.object(vrm_edit.subprocess, "run", side_effect=_timeout_exc()) as m:
            with self.assertRaises(RuntimeError):
                vrm_edit.edit_vrm(in_vrm, out_vrm, {}, blender_path="blender")

        self.assertIn("timeout", m.call_args.kwargs)


class TestVrmConvertTimeout(unittest.TestCase):
    """render.vrm_convert.glb_to_vrm raises RuntimeError on Blender failure."""

    def test_passes_timeout_and_raises_runtimeerror(self):
        from render import vrm_convert

        with tempfile.NamedTemporaryFile(suffix=".glb", delete=False) as tf:
            glb = tf.name
        vrm = glb.replace(".glb", ".vrm")

        with patch.object(vrm_convert.subprocess, "run", side_effect=_timeout_exc()) as m:
            with self.assertRaises(RuntimeError):
                vrm_convert.glb_to_vrm(glb, vrm, blender_path="blender")

        self.assertIn("timeout", m.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
