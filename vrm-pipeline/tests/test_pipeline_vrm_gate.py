"""
tests/test_pipeline_vrm_gate.py

Tests that the VRM output-quality gate (_validate_vrm_gate) is wired into the
pipeline so that:

  * a failing validation raises and BLOCKS the ledger insert (subprocess.run
    for `ledger insert` is never reached), and
  * a passing validation writes validation.json alongside the artifact.

No live Blender/ledger required — subprocess.run is mocked and assert_valid_vrm
is patched where needed.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pipeline  # noqa: E402
from glb_fixtures import minimal_glb_bytes  # noqa: E402


class TestValidateVrmGate(unittest.TestCase):
    def test_failure_blocks_ledger_insert(self):
        """When validation raises, _ledger_insert (subprocess.run) is never called."""
        with tempfile.TemporaryDirectory() as d:
            output_dir = Path(d)
            vrm_path = output_dir / "model.vrm"
            vrm_path.write_bytes(minimal_glb_bytes(128))  # GLB-valid but not a real VRM

            ran = []

            def _fake_run(cmd, **kwargs):  # would be the ledger insert
                ran.append(cmd)
                res = MagicMock()
                res.returncode = 0
                res.stdout = "rec-xyz"
                res.stderr = ""
                return res

            # Real assert_valid_vrm runs: a header-only GLB has no JSON chunk →
            # error → RuntimeError, which must propagate out of the gate.
            with patch("subprocess.run", side_effect=_fake_run):
                with self.assertRaises(RuntimeError):
                    report = pipeline._validate_vrm_gate(vrm_path, output_dir)
                    # If the gate had not raised, the caller would proceed to insert:
                    pipeline._ledger_insert(Path("db"), "p", str(output_dir), {"v": report})

            self.assertEqual(ran, [], "ledger insert must not run when validation fails")

    def test_failure_via_patched_validator_does_not_reach_insert(self):
        """Simulate the handle_prompt ordering: gate before insert; gate raises."""
        with tempfile.TemporaryDirectory() as d:
            output_dir = Path(d)
            vrm_path = output_dir / "model.vrm"
            vrm_path.write_bytes(minimal_glb_bytes(128))

            insert_called = []

            def _insert(*a, **k):
                insert_called.append(a)
                return "rec-xyz"

            def _boom(path, report_path=None):
                # write the report like the real one would, then raise
                Path(report_path).write_text(json.dumps({"ok": False, "errors": ["x"]}))
                raise RuntimeError("VRM validation failed")

            with patch("render.vrm_validate.assert_valid_vrm", side_effect=_boom), \
                 patch.object(pipeline, "_ledger_insert", side_effect=_insert):
                with self.assertRaises(RuntimeError):
                    validation = pipeline._validate_vrm_gate(vrm_path, output_dir)
                    pipeline._ledger_insert(Path("db"), "p", str(output_dir), {"v": validation})

            self.assertEqual(insert_called, [])
            self.assertTrue((output_dir / "validation.json").exists())

    def test_success_writes_validation_json(self):
        """A passing VRM writes validation.json next to the artifact and returns ok."""
        import struct
        # Build a real, healthy 1.0 VRM so the gate passes for real.
        bones = list(__import__("render.vrm_validate", fromlist=["x"])._REQUIRED_BONES_10)
        gltf = {
            "nodes": [{"name": f"n{i}"} for i in range(len(bones))],
            "extensions": {"VRMC_vrm": {"humanoid": {
                "humanBones": {b: {"node": i} for i, b in enumerate(bones)}
            }}},
        }
        raw = json.dumps(gltf).encode("utf-8")
        raw += b" " * ((-len(raw)) % 4)
        chunk = struct.pack("<I", len(raw)) + struct.pack("<I", 0x4E4F534A) + raw
        total = 12 + len(chunk)
        blob = b"glTF" + struct.pack("<I", 2) + struct.pack("<I", total) + chunk

        with tempfile.TemporaryDirectory() as d:
            output_dir = Path(d)
            vrm_path = output_dir / "model.vrm"
            vrm_path.write_bytes(blob)

            # No GLTF_VALIDATOR_PATH → built-in checks only.
            import os
            os.environ.pop("GLTF_VALIDATOR_PATH", None)
            report = pipeline._validate_vrm_gate(vrm_path, output_dir)

            self.assertTrue(report["ok"])
            vj = output_dir / "validation.json"
            self.assertTrue(vj.exists())
            self.assertEqual(json.loads(vj.read_text())["version"], "1.0")


if __name__ == "__main__":
    unittest.main()
