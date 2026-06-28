"""
tests/test_host_cli.py

Verifies the plain-Python host CLIs (`_host_main`) for the dual-mode Blender
scripts ``render/vrm_edit.py`` and ``render/vrm_convert.py``:

  * ``_host_main`` parses argv, loads the adjustments JSON (edit only), and
    delegates to the host wrapper (`edit_vrm` / `glb_to_vrm`) with the parsed
    arguments — without importing ``bpy``.
  * The accompanying Claude Code slash-command files exist with frontmatter.

No live Blender is required: the host wrappers are patched in each module.
Modules must remain importable without ``bpy``.

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_host_cli
    # or
    python -m unittest discover -s tests
"""

import json
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


class TestVrmEditHostMain(unittest.TestCase):
    """render.vrm_edit._host_main parses argv + adjustments JSON and calls edit_vrm."""

    def test_calls_edit_vrm_with_parsed_args(self):
        from render import vrm_edit

        adjustments = {
            "expressions": {"happy": 0.8, "blink": 0.2},
            "materials": {"hair": [0.1, 0.05, 0.02, 1.0]},
            "height_scale": 1.1,
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as af:
            json.dump(adjustments, af)
            adj_path = af.name

        argv = [
            "vrm_edit.py",
            "--in", "a.vrm",
            "--out", "b.vrm",
            "--adjustments-file", adj_path,
            "--blender", "/bin/blender",
        ]

        with patch.object(vrm_edit, "edit_vrm", return_value="/out/b.vrm") as m, \
                patch.object(sys, "argv", argv):
            vrm_edit._host_main()

        m.assert_called_once()
        args, kwargs = m.call_args
        # in_vrm / out passed positionally; adjustments is the parsed JSON dict.
        self.assertEqual(args[0], "a.vrm")
        self.assertEqual(args[1], "b.vrm")
        self.assertEqual(args[2], adjustments)
        self.assertEqual(kwargs["blender_path"], "/bin/blender")

    def test_apply_log_dir_defaults_to_none(self):
        from render import vrm_edit

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as af:
            json.dump({}, af)
            adj_path = af.name

        argv = [
            "vrm_edit.py",
            "--in", "a.vrm",
            "--out", "b.vrm",
            "--adjustments-file", adj_path,
        ]
        with patch.object(vrm_edit, "edit_vrm", return_value="/out/b.vrm") as m, \
                patch.object(sys, "argv", argv):
            vrm_edit._host_main()

        _, kwargs = m.call_args
        self.assertIsNone(kwargs["blender_path"])
        self.assertIsNone(kwargs["apply_log_dir"])


class TestVrmConvertHostMain(unittest.TestCase):
    """render.vrm_convert._host_main parses argv and calls glb_to_vrm."""

    def test_calls_glb_to_vrm_with_parsed_args(self):
        from render import vrm_convert

        argv = [
            "vrm_convert.py",
            "--glb", "m.glb",
            "--out", "m.vrm",
            "--blender", "/bin/blender",
        ]
        with patch.object(vrm_convert, "glb_to_vrm", return_value="/out/m.vrm") as m, \
                patch.object(sys, "argv", argv):
            vrm_convert._host_main()

        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertEqual(args[0], "m.glb")
        self.assertEqual(args[1], "m.vrm")
        self.assertEqual(kwargs["blender_path"], "/bin/blender")


class TestModulesImportableWithoutBpy(unittest.TestCase):
    """Both modules import in a bare (bpy-free) environment."""

    def test_bpy_absent(self):
        self.assertNotIn("bpy", sys.modules)

    def test_imports_and_host_main_present(self):
        import render.vrm_edit as ve
        import render.vrm_convert as vc

        self.assertFalse(ve._INSIDE_BLENDER)
        self.assertFalse(vc._INSIDE_BLENDER)
        self.assertTrue(callable(ve._host_main))
        self.assertTrue(callable(vc._host_main))


class TestSlashCommandFiles(unittest.TestCase):
    """The edit/convert slash-command files exist and carry frontmatter."""

    COMMANDS_DIR = PIPELINE_ROOT / "claude-plugin" / "commands"

    def _assert_command(self, name):
        path = self.COMMANDS_DIR / name
        self.assertTrue(path.exists(), f"missing command file: {path}")
        text = path.read_text(encoding="utf-8")
        self.assertIn("description:", text)
        self.assertIn("allowed-tools:", text)

    def test_edit_command(self):
        self._assert_command("edit.md")

    def test_convert_command(self):
        self._assert_command("convert.md")


if __name__ == "__main__":
    unittest.main()
