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
from types import SimpleNamespace
from unittest import mock

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


class TestPopImageRef(unittest.TestCase):
    def test_pops_and_removes_image_ref(self):
        params = {"image_ref": "/abs/img.webp", "asset_type": "object"}
        ref = pipeline._pop_image_ref(params)
        self.assertEqual(ref, "/abs/img.webp")
        self.assertNotIn("image_ref", params, "image_ref must not leak into generation_params")

    def test_absent_image_ref_returns_none(self):
        params = {"asset_type": "object"}
        self.assertIsNone(pipeline._pop_image_ref(params))


class TestLedgerInsertImageRef(unittest.TestCase):
    def test_insert_cmd_carries_parent_and_image_ref(self):
        captured = {}

        def fake_run(cmd, label):
            captured["cmd"] = cmd
            return ("rec-123", "", 0)

        with mock.patch.object(pipeline, "_run", side_effect=fake_run):
            rid = pipeline._ledger_insert(
                Path("/tmp/db"), "prompt", "/tmp/r0", {"asset_type": "vrm"},
                parent_id="parent-9", image_ref="/abs/img.webp",
            )
        self.assertEqual(rid, "rec-123")
        cmd = captured["cmd"]
        self.assertIn("--parent-id", cmd)
        self.assertEqual(cmd[cmd.index("--parent-id") + 1], "parent-9")
        self.assertIn("--image-ref", cmd)
        self.assertEqual(cmd[cmd.index("--image-ref") + 1], "/abs/img.webp")

    def test_insert_cmd_omits_image_ref_when_none(self):
        with mock.patch.object(pipeline, "_run", return_value=("rec-1", "", 0)):
            with mock.patch.object(pipeline, "_run") as m:
                m.return_value = ("rec-1", "", 0)
                pipeline._ledger_insert(Path("/tmp/db"), "p", "/tmp/r0", {})
                cmd = m.call_args[0][0]
        self.assertNotIn("--image-ref", cmd)
        self.assertNotIn("--parent-id", cmd)


class TestHandlePromptImageFlow(unittest.TestCase):
    def test_image_ref_drives_vrm_flow_and_lineage(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            prompt_path = d / "char_v2.prompt"
            prompt_path.write_text("赤いローブの少女\n", encoding="utf-8")
            (d / "char_v2.params.json").write_text(
                json.dumps({"parent_id": "parent-7", "image_ref": "/abs/ref.webp"}),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                output_base=str(d / "out"),
                blender_path="blender",
                db_path=Path("/tmp/db"),
                gen_retries=1,
                gen_model="gemini-2.5-flash",
            )

            gen_calls = {}

            def fake_generate(prompt, output_dir, a, image_ref=None):
                gen_calls["image_ref"] = image_ref
                glb = Path(output_dir) / "generated" / "g.glb"
                glb.parent.mkdir(parents=True, exist_ok=True)
                glb.write_bytes(b"GLB")
                return {"output_glb": str(glb)}

            insert_calls = {}

            def fake_insert(db_path, prompt, r0, params, parent_id=None, image_ref=None):
                insert_calls["parent_id"] = parent_id
                insert_calls["image_ref"] = image_ref
                insert_calls["asset_type"] = params.get("asset_type")
                return "rec-xyz"

            with mock.patch.object(pipeline, "_generate", side_effect=fake_generate), \
                 mock.patch.object(pipeline, "_render_vrm", return_value={"blender_version": "4.x", "render_sha256": "abc"}), \
                 mock.patch.object(pipeline, "_enrich_prompt", side_effect=lambda p, a: p), \
                 mock.patch.object(pipeline, "_ledger_insert", side_effect=fake_insert), \
                 mock.patch.object(pipeline, "_post_process"), \
                 mock.patch("render.vrm_convert.glb_to_vrm", return_value="/tmp/out.vrm") as conv:
                rid = pipeline.handle_prompt(prompt_path, args)

            self.assertEqual(rid, "rec-xyz")
            # image_ref propagated into _generate (Hyper3D path)
            self.assertEqual(gen_calls["image_ref"], "/abs/ref.webp")
            # GLB→VRM conversion was invoked
            self.assertTrue(conv.called, "glb_to_vrm must be called for the image flow")
            # lineage + image_ref reach the ledger; asset_type is vrm
            self.assertEqual(insert_calls["parent_id"], "parent-7")
            self.assertEqual(insert_calls["image_ref"], "/abs/ref.webp")
            self.assertEqual(insert_calls["asset_type"], "vrm")


if __name__ == "__main__":
    unittest.main()
