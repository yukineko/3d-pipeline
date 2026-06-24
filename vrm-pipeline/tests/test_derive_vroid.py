"""
Tests for the VRoid-edit derivation mode in derive.py (--vroid-edit).

Covers:
- emit_drop writing vroid_edit / base_vrm / change into params.json
- backwards compatibility: vroid keys are absent when vroid_edit=False
- resolve_base_vrm_from_record resolving vrm_path from generation_params
- resolve_base_vrm_from_record falling back to asset_ref
- resolve_base_vrm_from_record exiting when unresolvable
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import derive  # noqa: E402


class EmitDropVroidEditTest(unittest.TestCase):
    def _read_params(self, d: str, name: str) -> dict:
        return json.loads((Path(d) / f"{name}.params.json").read_text(encoding="utf-8"))

    def test_vroid_edit_writes_fields(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            base_vrm = out / "base.vrm"
            base_vrm.write_text("vrm", encoding="utf-8")
            derive.emit_drop(
                out,
                "char_v2",
                "美少女キャラ",
                "parent-xyz",
                vroid_edit=True,
                base_vrm=base_vrm,
                change="髪を赤くして目を大きく",
            )
            params = self._read_params(d, "char_v2")
            self.assertEqual(params["parent_id"], "parent-xyz")
            self.assertTrue(params["vroid_edit"])
            self.assertEqual(params["base_vrm"], str(base_vrm.resolve()))
            self.assertTrue(os.path.isabs(params["base_vrm"]))
            self.assertEqual(params["change"], "髪を赤くして目を大きく")
            self.assertNotIn("image_ref", params)

    def test_vroid_edit_with_image_ref(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            base_vrm = out / "base.vrm"
            base_vrm.write_text("vrm", encoding="utf-8")
            img = out / "ref.png"
            img.write_text("png", encoding="utf-8")
            derive.emit_drop(
                out,
                "char_v3",
                "p",
                "pid",
                image_ref=img,
                vroid_edit=True,
                base_vrm=base_vrm,
                change="服を変える",
            )
            params = self._read_params(d, "char_v3")
            self.assertTrue(params["vroid_edit"])
            self.assertEqual(params["image_ref"], str(img.resolve()))
            self.assertEqual(params["change"], "服を変える")

    def test_non_vroid_edit_omits_fields(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            derive.emit_drop(out, "chair_v2", "金属脚の椅子", "parent-abc")
            params = self._read_params(d, "chair_v2")
            self.assertEqual(params, {"parent_id": "parent-abc"})
            self.assertNotIn("vroid_edit", params)
            self.assertNotIn("base_vrm", params)
            self.assertNotIn("change", params)


class ResolveBaseVrmTest(unittest.TestCase):
    def test_resolve_from_generation_params(self):
        with tempfile.TemporaryDirectory() as d:
            vrm = Path(d) / "model.vrm"
            vrm.write_text("vrm", encoding="utf-8")
            record = {
                "generation_params": json.dumps({"vrm_path": str(vrm)}),
                "asset_ref": "{}",
            }
            result = derive.resolve_base_vrm_from_record(record)
            self.assertEqual(result, vrm.resolve())

    def test_resolve_from_asset_ref_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            vrm = Path(d) / "model.vrm"
            vrm.write_text("vrm", encoding="utf-8")
            record = {
                "generation_params": "{}",
                "asset_ref": json.dumps({"vrm": str(vrm)}),
            }
            result = derive.resolve_base_vrm_from_record(record)
            self.assertEqual(result, vrm.resolve())

    def test_unresolvable_exits(self):
        record = {"generation_params": "{}", "asset_ref": "{}"}
        with self.assertRaises(SystemExit):
            derive.resolve_base_vrm_from_record(record)

    def test_malformed_json_exits(self):
        record = {"generation_params": "not json", "asset_ref": "also bad"}
        with self.assertRaises(SystemExit):
            derive.resolve_base_vrm_from_record(record)


if __name__ == "__main__":
    unittest.main()
