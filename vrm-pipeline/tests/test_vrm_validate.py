"""
Unit tests for render.vrm_validate — the VRM semantic output-quality gate.

bpy-free and network-free: a ``.vrm`` is synthesised in-memory as a GLB with a
hand-built JSON chunk, so these exercise the real GLB parse + version-aware
humanoid/T-pose logic without Blender or any validator binary.
"""

import json
import struct
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

from render import vrm_validate  # noqa: E402

_CHUNK_TYPE_JSON = 0x4E4F534A


def make_glb(gltf: dict) -> bytes:
    """Build a minimal single-(JSON)-chunk GLB carrying *gltf*."""
    body = json.dumps(gltf).encode("utf-8")
    pad = (-len(body)) % 4
    body += b" " * pad  # JSON chunks are space-padded to 4 bytes
    chunk = struct.pack("<I", len(body)) + struct.pack("<I", _CHUNK_TYPE_JSON) + body
    total = 12 + len(chunk)
    header = b"glTF" + struct.pack("<I", 2) + struct.pack("<I", total)
    return header + chunk


def write_glb(path: Path, gltf: dict) -> Path:
    path.write_bytes(make_glb(gltf))
    return path


# --- glTF builders -----------------------------------------------------------

def _nodes(n: int) -> list:
    return [{"name": f"n{i}"} for i in range(n)]


def healthy_vrm_0x() -> dict:
    """A 0.x VRM with all 17 required bones wired to identity/unit nodes."""
    bones = list(vrm_validate._REQUIRED_BONES_0X)
    nodes = _nodes(len(bones))
    human_bones = [{"bone": b, "node": i} for i, b in enumerate(bones)]
    return {
        "nodes": nodes,
        "extensions": {"VRM": {"humanoid": {"humanBones": human_bones}}},
    }


def healthy_vrm_10() -> dict:
    """A 1.0 VRM with all 15 required bones wired to positive-scale nodes."""
    bones = list(vrm_validate._REQUIRED_BONES_10)
    nodes = _nodes(len(bones))
    human_bones = {b: {"node": i} for i, b in enumerate(bones)}
    return {
        "nodes": nodes,
        "extensions": {"VRMC_vrm": {"humanoid": {"humanBones": human_bones}}},
    }


class TestGlbParse(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))

    def test_reads_json_chunk(self):
        p = write_glb(self.tmp / "a.vrm", {"hello": "world"})
        self.assertEqual(vrm_validate._read_glb_json(p), {"hello": "world"})

    def test_bad_magic_raises(self):
        p = self.tmp / "bad.vrm"
        p.write_bytes(b"NOPE" + b"\x00" * 60)
        with self.assertRaises(RuntimeError):
            vrm_validate._read_glb_json(p)

    def test_no_json_chunk_raises(self):
        from tests.glb_fixtures import minimal_glb_bytes
        p = self.tmp / "nojson.vrm"
        p.write_bytes(minimal_glb_bytes(128))  # header-valid but zero-filled, no JSON chunk
        with self.assertRaises(RuntimeError):
            vrm_validate._read_glb_json(p)


class TestVersionAndBoneMap(unittest.TestCase):
    def test_detect_versions(self):
        self.assertEqual(vrm_validate.detect_vrm_version(healthy_vrm_0x()), "0.x")
        self.assertEqual(vrm_validate.detect_vrm_version(healthy_vrm_10()), "1.0")
        self.assertIsNone(vrm_validate.detect_vrm_version({"nodes": []}))

    def test_bone_map_0x_list_shape(self):
        m = vrm_validate.humanoid_bone_nodes(healthy_vrm_0x(), "0.x")
        self.assertEqual(m["hips"], 0)
        self.assertEqual(len(m), len(vrm_validate._REQUIRED_BONES_0X))

    def test_bone_map_10_object_shape(self):
        m = vrm_validate.humanoid_bone_nodes(healthy_vrm_10(), "1.0")
        self.assertIn("hips", m)
        self.assertEqual(len(m), len(vrm_validate._REQUIRED_BONES_10))


class TestCompleteness(unittest.TestCase):
    def test_healthy_has_no_errors(self):
        self.assertEqual(vrm_validate.check_humanoid_completeness(healthy_vrm_0x(), "0.x"), [])
        self.assertEqual(vrm_validate.check_humanoid_completeness(healthy_vrm_10(), "1.0"), [])

    def test_missing_bone_is_error(self):
        gltf = healthy_vrm_0x()
        gltf["extensions"]["VRM"]["humanoid"]["humanBones"] = [
            b for b in gltf["extensions"]["VRM"]["humanoid"]["humanBones"]
            if b["bone"] != "leftHand"
        ]
        errors = vrm_validate.check_humanoid_completeness(gltf, "0.x")
        self.assertTrue(any("leftHand" in e for e in errors))

    def test_node_index_out_of_range_is_error(self):
        gltf = healthy_vrm_10()
        # point hips at a non-existent node
        gltf["extensions"]["VRMC_vrm"]["humanoid"]["humanBones"]["hips"]["node"] = 999
        errors = vrm_validate.check_humanoid_completeness(gltf, "1.0")
        self.assertTrue(any("hips" in e and "out of range" in e for e in errors))


class TestTpose(unittest.TestCase):
    def test_0x_rotation_deviation_is_warning_not_error(self):
        gltf = healthy_vrm_0x()
        gltf["nodes"][0]["rotation"] = [0.7, 0.0, 0.0, 0.7]  # hips tilted
        warnings = vrm_validate.check_tpose(gltf, "0.x")
        self.assertTrue(any("rotation" in w for w in warnings))
        # completeness (the blocking check) is unaffected
        self.assertEqual(vrm_validate.check_humanoid_completeness(gltf, "0.x"), [])

    def test_0x_nonunit_scale_is_warning(self):
        gltf = healthy_vrm_0x()
        gltf["nodes"][0]["scale"] = [1.2, 1.2, 1.2]  # height_scale-style edit
        warnings = vrm_validate.check_tpose(gltf, "0.x")
        self.assertTrue(any("scale" in w for w in warnings))

    def test_10_nonpositive_scale_is_warning(self):
        gltf = healthy_vrm_10()
        gltf["nodes"][0]["scale"] = [0.0, 1.0, 1.0]
        warnings = vrm_validate.check_tpose(gltf, "1.0")
        self.assertTrue(any("non-positive scale" in w for w in warnings))

    def test_10_positive_scale_no_warning(self):
        # A non-unit but positive scale is fine for 1.0 (raw bones may be scaled)
        gltf = healthy_vrm_10()
        gltf["nodes"][0]["scale"] = [1.5, 1.5, 1.5]
        self.assertEqual(vrm_validate.check_tpose(gltf, "1.0"), [])


class TestValidateVrm(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))

    def test_healthy_is_ok(self):
        p = write_glb(self.tmp / "ok.vrm", healthy_vrm_0x())
        report = vrm_validate.validate_vrm(p)
        self.assertTrue(report["ok"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["version"], "0.x")

    def test_no_vrm_extension_not_ok(self):
        p = write_glb(self.tmp / "plain.vrm", {"nodes": _nodes(3)})
        report = vrm_validate.validate_vrm(p)
        self.assertFalse(report["ok"])
        self.assertIsNone(report["version"])
        self.assertFalse(report["vrm_present"])

    def test_warnings_only_stays_ok(self):
        gltf = healthy_vrm_0x()
        gltf["nodes"][0]["scale"] = [1.2, 1.2, 1.2]  # T-pose warning, not an error
        p = write_glb(self.tmp / "warn.vrm", gltf)
        report = vrm_validate.validate_vrm(p)
        self.assertTrue(report["ok"])
        self.assertTrue(report["warnings"])

    def test_validator_absent_does_not_raise(self):
        # No GLTF_VALIDATOR_PATH and (assumed) no gltf_validator on PATH:
        # run_gltf_validator returns None and validate_vrm still succeeds.
        import os
        os.environ.pop("GLTF_VALIDATOR_PATH", None)
        self.assertIsNone(vrm_validate.run_gltf_validator(self.tmp / "missing.vrm")
                          if not os.environ.get("GLTF_VALIDATOR_PATH") else {})
        p = write_glb(self.tmp / "ok2.vrm", healthy_vrm_10())
        report = vrm_validate.validate_vrm(p)
        self.assertTrue(report["ok"])


class TestAssertValidVrm(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))

    def test_writes_report_and_returns_on_success(self):
        p = write_glb(self.tmp / "ok.vrm", healthy_vrm_10())
        report_path = self.tmp / "validation.json"
        report = vrm_validate.assert_valid_vrm(p, report_path=report_path)
        self.assertTrue(report["ok"])
        self.assertTrue(report_path.exists())
        on_disk = json.loads(report_path.read_text())
        self.assertEqual(on_disk["version"], "1.0")

    def test_raises_on_errors_and_still_writes_report(self):
        gltf = healthy_vrm_0x()
        gltf["extensions"]["VRM"]["humanoid"]["humanBones"] = []  # strip all bones
        p = write_glb(self.tmp / "broken.vrm", gltf)
        report_path = self.tmp / "validation.json"
        with self.assertRaises(RuntimeError):
            vrm_validate.assert_valid_vrm(p, report_path=report_path)
        # report is still written for the aesthetically-unaware user to inspect
        self.assertTrue(report_path.exists())
        on_disk = json.loads(report_path.read_text())
        self.assertFalse(on_disk["ok"])


if __name__ == "__main__":
    unittest.main()
