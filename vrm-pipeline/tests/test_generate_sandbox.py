"""
tests/test_generate_sandbox.py

Unit tests for the opt-in, best-effort OS sandbox around untrusted Blender
execution in generate.py.

Design:
  - These tests NEVER launch Blender. They exercise the PURE command builder
    (build_blender_cmd) and the platform probe (sandbox_available) directly,
    mocking sys.platform and shutil.which as referenced inside generate.py.
  - One test mocks subprocess.run to confirm run_blender(sandbox=True) on a
    non-sandbox platform still runs the plain command.

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_generate_sandbox
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure vrm-pipeline/ is on sys.path so `import generate` works.
HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import generate


_BLENDER = "blender"
_SCRIPT = "/tmp/gen_abc.py"
_OUTPUT = "/some/out/dir/gen_abc.glb"

_ORIGINAL_CMD = [
    "blender",
    "--background",
    "--python", "/tmp/gen_abc.py",
    "--",
    "--output", "/some/out/dir/gen_abc.glb",
]


class BuildBlenderCmdNoSandboxTest(unittest.TestCase):
    def test_sandbox_false_returns_exact_original_cmd(self):
        cmd = generate.build_blender_cmd(_BLENDER, _SCRIPT, _OUTPUT, sandbox=False)
        self.assertEqual(cmd, _ORIGINAL_CMD)
        self.assertEqual(len(cmd), 7)

    def test_default_is_no_sandbox(self):
        # sandbox defaults to False.
        cmd = generate.build_blender_cmd(_BLENDER, _SCRIPT, _OUTPUT)
        self.assertEqual(cmd, _ORIGINAL_CMD)


class BuildBlenderCmdDarwinSandboxTest(unittest.TestCase):
    def test_sandbox_true_on_darwin_with_sandbox_exec(self):
        with patch.object(generate.sys, "platform", "darwin"), \
                patch.object(generate.shutil, "which", return_value="/usr/bin/sandbox-exec"):
            cmd = generate.build_blender_cmd(_BLENDER, _SCRIPT, _OUTPUT, sandbox=True)

        self.assertEqual(cmd[0], "sandbox-exec")
        self.assertEqual(cmd[1], "-p")

        profile = cmd[2]
        # Default-deny present (network denial: omitting allow + explicit deny).
        self.assertIn("(deny default)", profile)
        self.assertIn("(deny network*)", profile)
        # Writes confined to the absolute output directory.
        expected_dir = os.path.dirname(os.path.abspath(_OUTPUT))
        self.assertIn("file-write*", profile)
        self.assertIn(expected_dir, profile)
        # Network allow must NOT be granted.
        self.assertNotIn("(allow network", profile)

        # The original blender argv appears (in order) after the profile.
        self.assertEqual(cmd[3:], _ORIGINAL_CMD)

    def test_sandbox_available_true_on_darwin(self):
        with patch.object(generate.sys, "platform", "darwin"), \
                patch.object(generate.shutil, "which", return_value="/usr/bin/sandbox-exec"):
            self.assertTrue(generate.sandbox_available())


class BuildBlenderCmdFallbackTest(unittest.TestCase):
    def test_sandbox_true_on_linux_falls_back_to_plain_cmd(self):
        with patch.object(generate.sys, "platform", "linux"), \
                patch.object(generate.shutil, "which", return_value="/usr/bin/sandbox-exec"):
            cmd = generate.build_blender_cmd(_BLENDER, _SCRIPT, _OUTPUT, sandbox=True)
        self.assertEqual(cmd, _ORIGINAL_CMD)
        self.assertNotIn("sandbox-exec", cmd)

    def test_sandbox_true_no_sandbox_exec_falls_back(self):
        with patch.object(generate.sys, "platform", "darwin"), \
                patch.object(generate.shutil, "which", return_value=None):
            cmd = generate.build_blender_cmd(_BLENDER, _SCRIPT, _OUTPUT, sandbox=True)
        self.assertEqual(cmd, _ORIGINAL_CMD)

    def test_sandbox_available_false_on_linux(self):
        with patch.object(generate.sys, "platform", "linux"), \
                patch.object(generate.shutil, "which", return_value="/usr/bin/sandbox-exec"):
            self.assertFalse(generate.sandbox_available())

    def test_sandbox_available_false_without_sandbox_exec(self):
        with patch.object(generate.sys, "platform", "darwin"), \
                patch.object(generate.shutil, "which", return_value=None):
            self.assertFalse(generate.sandbox_available())


class RunBlenderTest(unittest.TestCase):
    def test_run_blender_sandbox_true_non_darwin_uses_plain_cmd(self):
        class _Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        with patch.object(generate.sys, "platform", "linux"), \
                patch.object(generate.shutil, "which", return_value=None), \
                patch.object(generate.subprocess, "run", return_value=_Result()) as mock_run:
            generate.run_blender(_BLENDER, _SCRIPT, _OUTPUT, sandbox=True)

        called_cmd = mock_run.call_args[0][0]
        self.assertEqual(called_cmd, _ORIGINAL_CMD)
        self.assertNotIn("sandbox-exec", called_cmd)


if __name__ == "__main__":
    unittest.main()
