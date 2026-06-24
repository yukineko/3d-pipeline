"""
Tests for the derivation-lineage wiring in pipeline.py and derive.py.

Stdlib unittest only (the repo has no pytest harness). Run with:
    python -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import pipeline  # noqa: E402
import derive  # noqa: E402


class TestPopParentId(unittest.TestCase):
    def test_pops_and_removes_parent_id(self):
        params = {"parent_id": "abc-123", "asset_type": "object"}
        pid = pipeline._pop_parent_id(params)
        self.assertEqual(pid, "abc-123")
        self.assertNotIn("parent_id", params, "parent_id must not leak into generation_params")
        self.assertEqual(params, {"asset_type": "object"})

    def test_absent_parent_id_returns_none(self):
        params = {"asset_type": "object"}
        self.assertIsNone(pipeline._pop_parent_id(params))
        self.assertEqual(params, {"asset_type": "object"})


class TestEmitDrop(unittest.TestCase):
    def test_writes_prompt_and_params_pair(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "drop"
            derive.emit_drop(out, "chair_v2", "金属脚の椅子", "parent-xyz")
            prompt = (out / "chair_v2.prompt").read_text(encoding="utf-8")
            params = json.loads((out / "chair_v2.params.json").read_text(encoding="utf-8"))
            self.assertEqual(prompt.strip(), "金属脚の椅子")
            self.assertEqual(params, {"parent_id": "parent-xyz"})

    def test_rejects_path_traversal_name(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ["../evil", "a/b", "..", "", "a\\b"]:
                with self.assertRaises(ValueError):
                    derive.emit_drop(Path(d), bad, "p", "pid")


if __name__ == "__main__":
    unittest.main()
