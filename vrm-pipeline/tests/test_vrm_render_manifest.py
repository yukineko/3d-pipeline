"""Integration regression test for render/vrm.py manifest fidelity.

Exercises two fixes that only manifest under a real Blender (so this is an
integration test, skipped in the stdlib-only CI):

  1. get_vrm_addon_version() reports the version of an *extension*-installed
     VRM add-on (bl_ext.user_default.vrm), not null.
  2. Material setup no longer raises on Blender 4.3+/5.x where the legacy
     Material.shadow_method / blend_method properties were removed.

It runs only when both a Blender binary and a VRM fixture are available:
  - Blender: env BLENDER, else /Applications/Blender.app/Contents/MacOS/Blender,
    else `blender` on PATH.
  - VRM fixture: env VRM_FIXTURE pointing at a .vrm file.
Otherwise the whole case is skipped (CI has neither).
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
VRM_SCRIPT = REPO / "render" / "vrm.py"


def _find_blender():
    env = os.environ.get("BLENDER")
    if env and Path(env).exists():
        return env
    mac = "/Applications/Blender.app/Contents/MacOS/Blender"
    if Path(mac).exists():
        return mac
    return shutil.which("blender")


def _find_vrm_fixture():
    fixture = os.environ.get("VRM_FIXTURE")
    if fixture and Path(fixture).exists():
        return fixture
    return None


BLENDER = _find_blender()
VRM_FIXTURE = _find_vrm_fixture()


@unittest.skipUnless(BLENDER and VRM_FIXTURE,
                     "needs Blender + VRM_FIXTURE (integration only; skipped in stdlib CI)")
class VrmRenderManifestTest(unittest.TestCase):
    def test_manifest_reports_addon_version_and_no_material_error(self):
        with tempfile.TemporaryDirectory() as out:
            cmd = [
                BLENDER, "--background", "--python", str(VRM_SCRIPT), "--",
                "--vrm", VRM_FIXTURE, "--output", out, "--resolution", "256",
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)

            manifest_path = Path(out) / "manifest.json"
            self.assertTrue(manifest_path.exists(), "render did not write manifest.json")
            manifest = json.loads(manifest_path.read_text())

            # Fix 1: extension-installed add-on version is detected (non-null).
            self.assertIsNotNone(
                manifest.get("vrm_addon_version"),
                "vrm_addon_version is null — extension version detection regressed",
            )

            # Fix 2: no legacy shadow_method/blend_method AttributeError.
            errors = manifest.get("errors") or []
            self.assertFalse(
                any("shadow_method" in e or "blend_method" in e for e in errors),
                f"material setup regressed on EEVEE-Next: {errors}",
            )

            # The canonical capture is 3 body + 4 face views.
            self.assertEqual(len(manifest.get("faces") or []), 7,
                             "expected 3 body + 4 face views")


if __name__ == "__main__":
    unittest.main()
