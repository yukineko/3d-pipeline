"""
tests/test_output_gate.py

Tests for the VRM pipeline "output guarantee" gate:

  * render.vrm_utils.assert_valid_glb rejects missing / too-small / bad-magic /
    length-mismatch output files.
  * render.vrm_edit.edit_vrm raises (rather than reporting success) when a
    mocked-but-successful Blender run leaves no valid VRM, or leaves a valid VRM
    whose applied-vs-requested report shows a requested adjustment matched zero
    targets.
  * The happy path (valid GLB + all-applied report) returns the output path.

No live Blender installation required — subprocess.run is mocked and made to
simulate Blender writing its --out (and --report-file) artifacts.

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_output_gate
"""

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make vrm-pipeline/ and tests/ importable.
HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from glb_fixtures import minimal_glb_bytes


def _glb_bytes(size=128, magic=b"glTF", declared_len=None):
    """Build GLB-ish bytes with controllable magic / declared length / size."""
    if declared_len is None:
        declared_len = size
    body = b"\x00" * max(0, size - 12)
    header = magic[:4].ljust(4, b"\x00") + struct.pack("<I", 2) + struct.pack("<I", declared_len)
    return (header + body)[:max(size, 0)] if size >= 12 else b"\x00" * size


def _fake_run(*, returncode=0, out_bytes=None, report=None):
    """
    Return a subprocess.run side_effect that simulates Blender:
      * writes *out_bytes* to the --out path (when not None), and
      * writes *report* (a dict) as JSON to the --report-file path (when not None),
    then returns a result object with the given returncode.
    """
    def _run(cmd, **kwargs):
        if out_bytes is not None and "--out" in cmd:
            Path(cmd[cmd.index("--out") + 1]).write_bytes(out_bytes)
        if report is not None and "--report-file" in cmd:
            Path(cmd[cmd.index("--report-file") + 1]).write_text(
                json.dumps(report), encoding="utf-8"
            )
        res = MagicMock()
        res.returncode = returncode
        res.stderr = ""
        res.stdout = ""
        return res
    return _run


# ---------------------------------------------------------------------------
# assert_valid_glb unit tests (the post-condition helper)
# ---------------------------------------------------------------------------

class TestAssertValidGlb(unittest.TestCase):
    def test_missing_file_raises(self):
        from render.vrm_utils import assert_valid_glb
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError) as ctx:
                assert_valid_glb(Path(d) / "nope.vrm")
            self.assertIn("not written", str(ctx.exception))

    def test_too_small_raises(self):
        from render.vrm_utils import assert_valid_glb
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "tiny.vrm"
            p.write_bytes(b"glTF\x02\x00\x00\x00")  # 8 bytes < min_bytes
            with self.assertRaises(RuntimeError) as ctx:
                assert_valid_glb(p)
            self.assertIn("too small", str(ctx.exception))

    def test_bad_magic_raises(self):
        from render.vrm_utils import assert_valid_glb
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.vrm"
            p.write_bytes(_glb_bytes(size=128, magic=b"XXXX"))
            with self.assertRaises(RuntimeError) as ctx:
                assert_valid_glb(p)
            self.assertIn("not a glTF binary", str(ctx.exception))

    def test_length_mismatch_raises(self):
        from render.vrm_utils import assert_valid_glb
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "corrupt.vrm"
            # declares 999 bytes but file is 128
            p.write_bytes(_glb_bytes(size=128, declared_len=999))
            with self.assertRaises(RuntimeError) as ctx:
                assert_valid_glb(p)
            self.assertIn("corrupt", str(ctx.exception))

    def test_valid_glb_passes(self):
        from render.vrm_utils import assert_valid_glb
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ok.vrm"
            p.write_bytes(minimal_glb_bytes(128))
            # Should not raise.
            assert_valid_glb(p)


# ---------------------------------------------------------------------------
# edit_vrm gate integration (subprocess mocked)
# ---------------------------------------------------------------------------

class TestEditVrmOutputGate(unittest.TestCase):
    def _run_edit(self, adjustments, side_effect):
        from render.vrm_edit import edit_vrm
        with tempfile.TemporaryDirectory() as d:
            vrm_in = Path(d) / "model.vrm"
            vrm_in.write_bytes(b"")
            vrm_out = Path(d) / "out.vrm"
            with patch("subprocess.run", side_effect=side_effect):
                return edit_vrm(str(vrm_in), str(vrm_out), adjustments, blender_path="blender")

    def test_missing_output_after_success_raises(self):
        # Blender exits 0 but writes nothing.
        with self.assertRaises(RuntimeError) as ctx:
            self._run_edit({}, _fake_run(out_bytes=None))
        self.assertIn("not written", str(ctx.exception))

    def test_too_small_output_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run_edit({}, _fake_run(out_bytes=b"glTF\x02\x00\x00\x00"))
        self.assertIn("too small", str(ctx.exception))

    def test_bad_magic_output_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run_edit({}, _fake_run(out_bytes=_glb_bytes(magic=b"XXXX")))
        self.assertIn("not a glTF binary", str(ctx.exception))

    def test_length_mismatch_output_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run_edit({}, _fake_run(out_bytes=_glb_bytes(size=128, declared_len=999)))
        self.assertIn("corrupt", str(ctx.exception))

    def test_zero_applied_but_requested_raises(self):
        # Valid GLB, but the report says a requested material matched nothing.
        adjustments = {"materials": {"hair": [0.1, 0.1, 0.1, 1.0]}}
        report = {"materials": {"requested": 1, "applied": 0}}
        with self.assertRaises(RuntimeError) as ctx:
            self._run_edit(
                adjustments,
                _fake_run(out_bytes=minimal_glb_bytes(128), report=report),
            )
        self.assertIn("materials", str(ctx.exception))

    def test_zero_applied_expressions_raises(self):
        adjustments = {"expressions": {"happy": 1.0}}
        report = {"expressions": {"requested": 1, "applied": 0}}
        with self.assertRaises(RuntimeError):
            self._run_edit(
                adjustments,
                _fake_run(out_bytes=minimal_glb_bytes(128), report=report),
            )

    def test_zero_applied_height_scale_raises(self):
        adjustments = {"height_scale": 1.2}
        report = {"height_scale": {"requested": 1, "applied": 0}}
        with self.assertRaises(RuntimeError):
            self._run_edit(
                adjustments,
                _fake_run(out_bytes=minimal_glb_bytes(128), report=report),
            )

    def test_valid_output_with_all_applied_returns_path(self):
        adjustments = {"materials": {"hair": [0.1, 0.1, 0.1, 1.0]}}
        report = {"materials": {"requested": 1, "applied": 1}}
        result = self._run_edit(
            adjustments,
            _fake_run(out_bytes=minimal_glb_bytes(128), report=report),
        )
        self.assertTrue(result.endswith("out.vrm"))

    def test_valid_output_without_report_returns_path(self):
        # No report written (e.g. older Blender / mock): gate is skipped, not failed.
        result = self._run_edit({}, _fake_run(out_bytes=minimal_glb_bytes(128)))
        self.assertTrue(result.endswith("out.vrm"))


if __name__ == "__main__":
    unittest.main()
