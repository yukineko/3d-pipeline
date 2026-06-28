"""
tests/test_obs.py

Unit tests for render.obs (logging + apply-log persistence) and the
edit_vrm(apply_log_dir=...) integration.

Design goals:
  * No live Blender installation required (render.obs is bpy-free).
  * Verify get_logger honors VRM_LOG_LEVEL, falls back to INFO on bad values,
    and is idempotent (no duplicate handlers).
  * Verify write_apply_log round-trips a dict, creates nested dirs, returns an
    existing path.
  * Verify edit_vrm(apply_log_dir=...) persists apply_log.json containing the
    detected spec_version.

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_obs
"""

import importlib
import json
import logging
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

from glb_fixtures import write_glb_for_out_flag


# ---------------------------------------------------------------------------
# render.obs must import without bpy present.
# ---------------------------------------------------------------------------

class TestObsImport(unittest.TestCase):
    def test_module_importable_without_bpy(self):
        mod = importlib.import_module("render.obs")
        self.assertIsNotNone(mod)
        self.assertTrue(hasattr(mod, "get_logger"))
        self.assertTrue(hasattr(mod, "write_apply_log"))


# ---------------------------------------------------------------------------
# get_logger
# ---------------------------------------------------------------------------

class TestGetLogger(unittest.TestCase):
    def test_returns_logger(self):
        from render.obs import get_logger
        log = get_logger("vrm_test_returns")
        self.assertIsInstance(log, logging.Logger)

    def test_respects_debug_level(self):
        from render.obs import get_logger
        with patch.dict(os.environ, {"VRM_LOG_LEVEL": "DEBUG"}):
            log = get_logger("vrm_test_debug")
        self.assertEqual(log.level, logging.DEBUG)

    def test_invalid_level_falls_back_to_info(self):
        from render.obs import get_logger
        with patch.dict(os.environ, {"VRM_LOG_LEVEL": "NOTALEVEL"}):
            log = get_logger("vrm_test_invalid")
        self.assertEqual(log.level, logging.INFO)

    def test_default_level_is_info(self):
        from render.obs import get_logger
        env = {k: v for k, v in os.environ.items() if k != "VRM_LOG_LEVEL"}
        with patch.dict(os.environ, env, clear=True):
            log = get_logger("vrm_test_default")
        self.assertEqual(log.level, logging.INFO)

    def test_idempotent_no_duplicate_handlers(self):
        from render.obs import get_logger
        log1 = get_logger("vrm_test_idem")
        n1 = len(log1.handlers)
        log2 = get_logger("vrm_test_idem")
        n2 = len(log2.handlers)
        self.assertEqual(n1, n2)
        self.assertEqual(n1, 1)
        self.assertIs(log1, log2)

    def test_propagate_disabled(self):
        from render.obs import get_logger
        log = get_logger("vrm_test_propagate")
        self.assertFalse(log.propagate)


# ---------------------------------------------------------------------------
# write_apply_log
# ---------------------------------------------------------------------------

class TestWriteApplyLog(unittest.TestCase):
    def test_writes_apply_log_json(self):
        from render.obs import write_apply_log
        payload = {"spec_version": "1.0",
                   "expressions": {"requested": 1, "applied": 1}}
        with tempfile.TemporaryDirectory() as d:
            path = write_apply_log(d, payload)
            self.assertTrue(path.endswith("apply_log.json"))
            self.assertTrue(Path(path).exists())

    def test_round_trips_dict(self):
        from render.obs import write_apply_log
        payload = {"spec_version": "0", "height_scale": {"requested": 1, "applied": 1},
                   "nested": {"a": [1, 2, 3]}}
        with tempfile.TemporaryDirectory() as d:
            path = write_apply_log(d, payload)
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded, payload)

    def test_creates_nested_out_dir(self):
        from render.obs import write_apply_log
        with tempfile.TemporaryDirectory() as d:
            nested = Path(d) / "a" / "b" / "c"
            self.assertFalse(nested.exists())
            path = write_apply_log(nested, {"x": 1})
            self.assertTrue(nested.exists())
            self.assertTrue(Path(path).exists())

    def test_returns_str(self):
        from render.obs import write_apply_log
        with tempfile.TemporaryDirectory() as d:
            path = write_apply_log(d, {})
            self.assertIsInstance(path, str)


# ---------------------------------------------------------------------------
# edit_vrm(apply_log_dir=...) integration
# ---------------------------------------------------------------------------

def _fake_run_writes_glb_and_report(report_payload):
    """subprocess.run side_effect: write the --out GLB AND a fake --report-file."""
    def _run(cmd, **kwargs):
        write_glb_for_out_flag(cmd)
        try:
            idx = cmd.index("--report-file")
            report_path = cmd[idx + 1]
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report_payload, f)
        except (ValueError, IndexError, OSError):
            pass
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        fake_result.stdout = ""
        return fake_result
    return _run


class TestEditVrmApplyLogDir(unittest.TestCase):
    def test_accepts_apply_log_dir_kwarg(self):
        import inspect
        from render.vrm_edit import edit_vrm
        params = inspect.signature(edit_vrm).parameters
        self.assertIn("apply_log_dir", params)
        self.assertIs(params["apply_log_dir"].default, None)

    def test_persists_apply_log_with_spec_version(self):
        from render.vrm_edit import edit_vrm

        report_payload = {
            "spec_version": "1.0",
            "expressions": {"requested": 1, "applied": 1},
        }

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"
            log_dir = Path(d) / "ledger" / "r0"

            with patch("subprocess.run",
                       side_effect=_fake_run_writes_glb_and_report(report_payload)):
                edit_vrm(str(vrm_in), str(vrm_out),
                         {"expressions": {"happy": 1.0}},
                         blender_path="blender",
                         apply_log_dir=str(log_dir))

            log_file = log_dir / "apply_log.json"
            self.assertTrue(log_file.exists())
            with open(log_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded.get("spec_version"), "1.0")
            self.assertEqual(loaded.get("expressions"), {"requested": 1, "applied": 1})

    def test_write_apply_log_invoked_with_report_dict(self):
        from render.vrm_edit import edit_vrm

        report_payload = {
            "spec_version": "0",
            "height_scale": {"requested": 1, "applied": 1},
        }

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run",
                       side_effect=_fake_run_writes_glb_and_report(report_payload)), \
                 patch("render.obs.write_apply_log") as mock_write:
                edit_vrm(str(vrm_in), str(vrm_out),
                         {"height_scale": 1.2},
                         blender_path="blender",
                         apply_log_dir=str(Path(d) / "logs"))

            self.assertTrue(mock_write.called)
            args, kwargs = mock_write.call_args
            # Second positional arg is the report dict.
            self.assertEqual(args[1], report_payload)

    def test_no_apply_log_when_dir_none(self):
        from render.vrm_edit import edit_vrm

        report_payload = {"spec_version": "1.0",
                          "expressions": {"requested": 1, "applied": 1}}

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run",
                       side_effect=_fake_run_writes_glb_and_report(report_payload)), \
                 patch("render.obs.write_apply_log") as mock_write:
                edit_vrm(str(vrm_in), str(vrm_out),
                         {"expressions": {"happy": 1.0}},
                         blender_path="blender")

            mock_write.assert_not_called()

    def test_persistence_failure_does_not_fail_edit(self):
        from render.vrm_edit import edit_vrm

        report_payload = {"spec_version": "1.0",
                          "expressions": {"requested": 1, "applied": 1}}

        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"

            with patch("subprocess.run",
                       side_effect=_fake_run_writes_glb_and_report(report_payload)), \
                 patch("render.obs.write_apply_log",
                       side_effect=OSError("disk full")):
                # Persistence is best-effort: edit must still succeed.
                result = edit_vrm(str(vrm_in), str(vrm_out),
                                  {"expressions": {"happy": 1.0}},
                                  blender_path="blender",
                                  apply_log_dir=str(Path(d) / "logs"))
            self.assertEqual(result, str(vrm_out.resolve()))


if __name__ == "__main__":
    unittest.main()
