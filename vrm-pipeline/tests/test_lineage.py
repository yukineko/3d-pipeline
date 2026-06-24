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

    def test_writes_image_ref_when_provided(self):
        with tempfile.TemporaryDirectory() as d:
            # Create a fake image file to pass as image_ref
            img = Path(d) / "render.webp"
            img.write_bytes(b"FAKE")
            out = Path(d) / "drop"
            derive.emit_drop(out, "chair_v3", "赤い椅子", "parent-abc", image_ref=img)
            params = json.loads((out / "chair_v3.params.json").read_text(encoding="utf-8"))
            self.assertEqual(params["parent_id"], "parent-abc")
            self.assertEqual(params["image_ref"], str(img.resolve()))

    def test_no_image_ref_key_when_none(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "drop"
            derive.emit_drop(out, "chair_v4", "青い椅子", "parent-def", image_ref=None)
            params = json.loads((out / "chair_v4.params.json").read_text(encoding="utf-8"))
            self.assertNotIn("image_ref", params, "image_ref must not appear when None is passed")


class TestResolveImageFromRecord(unittest.TestCase):
    def test_resolves_webp_from_r0_ref_dir(self):
        with tempfile.TemporaryDirectory() as d:
            render_dir = Path(d) / "renders" / "my_asset"
            render_dir.mkdir(parents=True)
            img = render_dir / "body_front.webp"
            img.write_bytes(b"FAKE")
            record = {"r0_ref": str(render_dir), "asset_ref": "{}"}
            result = derive.resolve_image_from_record(record)
            self.assertEqual(result, img.resolve())

    def test_resolves_png_when_no_webp(self):
        with tempfile.TemporaryDirectory() as d:
            render_dir = Path(d) / "renders" / "my_asset"
            render_dir.mkdir(parents=True)
            img = render_dir / "preview.png"
            img.write_bytes(b"FAKE")
            record = {"r0_ref": str(render_dir), "asset_ref": "{}"}
            result = derive.resolve_image_from_record(record)
            self.assertEqual(result, img.resolve())

    def test_exits_when_no_images_found(self):
        with tempfile.TemporaryDirectory() as d:
            render_dir = Path(d) / "renders" / "empty"
            render_dir.mkdir(parents=True)
            record = {"r0_ref": str(render_dir), "asset_ref": "{}"}
            with self.assertRaises(SystemExit):
                derive.resolve_image_from_record(record)

    def test_exits_when_r0_ref_missing(self):
        record = {"r0_ref": "", "asset_ref": "{}"}
        with self.assertRaises(SystemExit):
            derive.resolve_image_from_record(record)


if __name__ == "__main__":
    unittest.main()
