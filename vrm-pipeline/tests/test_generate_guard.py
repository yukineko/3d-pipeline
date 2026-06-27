"""
tests/test_generate_guard.py

Security tests for the untrusted-code gate in generate.py.

generate.py asks Gemini for a self-contained bpy script and then executes it
with Blender at host privilege. Before execution it must:
  1. statically guard the script (hard block on violations -> never run Blender),
  2. print a consent/warning banner to stderr when the guard passes,
  3. honor a kill switch (PIPELINE_BLOCK_UNTRUSTED_CODE) that refuses execution,
  4. append an audit record to <output_dir>/.code_audit.jsonl for every decision.

These tests are hermetic: the Gemini call and Blender subprocess are mocked, so
no network access or real Blender/API key is required.

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_generate_guard
"""

import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure vrm-pipeline/ is on sys.path so `import generate` works.
HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import generate  # noqa: E402


MALICIOUS_SCRIPT = "import os\nos.system('rm -rf /')\n"
CLEAN_SCRIPT = (
    "import bpy, sys\n"
    "output_path = sys.argv[sys.argv.index('--output') + 1]\n"
    "bpy.ops.export_scene.gltf(filepath=output_path, export_format='GLB')\n"
)


def _read_audit(output_dir):
    """Return the parsed JSONL records from the audit log (empty list if none)."""
    path = os.path.join(output_dir, ".code_audit.jsonl")
    if not os.path.isfile(path):
        return []
    records = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _argv(output_dir, prompt="a chair"):
    return ["generate.py", "--prompt", prompt, "--output-dir", output_dir]


def _fake_blender_creates_glb(blender_path, script_path, output_glb):
    """Simulate a successful Blender run: create the GLB and emit metadata JSON."""
    with open(output_glb, "wb") as fh:
        fh.write(b"glTF-binary-stub")
    return 0, '{"poly_count": 1, "dimensions": {"x": 1, "y": 1, "z": 1}}', ""


class _GuardTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.output_dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

        # main() does `import google.generativeai as genai; genai.configure(...)`.
        # google.generativeai is not installed in CI, so inject a fake module
        # exposing a no-op configure() so the import + call succeed offline.
        fake_genai = types.ModuleType("google.generativeai")
        fake_genai.configure = MagicMock()
        fake_genai.GenerativeModel = MagicMock()
        google_pkg = sys.modules.get("google") or types.ModuleType("google")
        patcher_pkg = patch.dict(sys.modules, {
            "google": google_pkg,
            "google.generativeai": fake_genai,
        })
        patcher_pkg.start()
        self.addCleanup(patcher_pkg.stop)
        patcher_attr = patch.object(google_pkg, "generativeai", fake_genai, create=True)
        patcher_attr.start()
        self.addCleanup(patcher_attr.stop)
        self.fake_genai = fake_genai


class TestViolationBlocksExecution(_GuardTestBase):
    """A guard violation must prevent Blender from ever running and exit non-zero."""

    def test_malicious_script_not_executed(self):
        with patch.object(generate, "generate_script", return_value=MALICIOUS_SCRIPT), \
                patch.object(generate, "generate_script_retry", return_value=MALICIOUS_SCRIPT), \
                patch.object(generate, "check_api_key", return_value="fake-key"), \
                patch.object(generate, "run_blender") as mock_blender, \
                patch.object(sys, "argv", _argv(self.output_dir)):
            with self.assertRaises(SystemExit) as cm:
                with redirect_stderr(io.StringIO()):
                    generate.main()

        # Non-zero exit
        self.assertNotEqual(cm.exception.code, 0)
        # Blender must NEVER be invoked on a violating script
        mock_blender.assert_not_called()

        records = _read_audit(self.output_dir)
        self.assertTrue(records, "expected at least one audit record")
        blocked = [r for r in records if r["decision"] == "blocked"]
        self.assertTrue(blocked, "expected a blocked audit record")
        self.assertFalse(blocked[-1]["guard_ok"])
        self.assertTrue(blocked[-1]["violations"], "violations should be listed")


class TestCleanScriptRunsWithConsent(_GuardTestBase):
    """A clean script must produce a warning banner, run Blender, and audit decision=run."""

    def test_clean_script_runs(self):
        stderr_buf = io.StringIO()
        with patch.object(generate, "generate_script", return_value=CLEAN_SCRIPT), \
                patch.object(generate, "check_api_key", return_value="fake-key"), \
                patch.object(generate, "run_blender",
                             side_effect=_fake_blender_creates_glb) as mock_blender, \
                patch.object(sys, "argv", _argv(self.output_dir)):
            with redirect_stderr(stderr_buf):
                with patch("sys.stdout", new=io.StringIO()):
                    generate.main()

        mock_blender.assert_called_once()

        sha = generate.script_hash(CLEAN_SCRIPT)
        stderr_text = stderr_buf.getvalue()
        self.assertIn("WARNING", stderr_text)
        self.assertIn(sha, stderr_text)

        records = _read_audit(self.output_dir)
        run_records = [r for r in records if r["decision"] == "run"]
        self.assertTrue(run_records, "expected a run audit record")
        rec = run_records[-1]
        self.assertTrue(rec["guard_ok"])
        self.assertEqual(rec["violations"], [])
        self.assertEqual(rec["script_hash"], sha)


class TestKillSwitch(_GuardTestBase):
    """When the kill switch is set, even a clean script must not run."""

    def test_killswitch_blocks_clean_script(self):
        with patch.object(generate, "generate_script", return_value=CLEAN_SCRIPT), \
                patch.object(generate, "check_api_key", return_value="fake-key"), \
                patch.object(generate, "run_blender") as mock_blender, \
                patch.dict(os.environ, {"PIPELINE_BLOCK_UNTRUSTED_CODE": "1"}), \
                patch.object(sys, "argv", _argv(self.output_dir)):
            with self.assertRaises(SystemExit) as cm:
                with redirect_stderr(io.StringIO()):
                    generate.main()

        self.assertNotEqual(cm.exception.code, 0)
        mock_blender.assert_not_called()

        records = _read_audit(self.output_dir)
        blocked = [r for r in records if r["decision"] == "blocked"]
        self.assertTrue(blocked, "expected a blocked audit record")
        self.assertTrue(blocked[-1]["guard_ok"], "guard passed; block was by policy")


class TestAuditLogJSONL(_GuardTestBase):
    """The audit log must be valid append-only JSONL with the required keys."""

    REQUIRED_KEYS = {
        "script_hash", "script_path", "output_glb",
        "guard_ok", "violations", "decision", "timestamp",
    }

    def test_audit_log_appends_and_is_valid_jsonl(self):
        # First run: clean script -> decision=run
        with patch.object(generate, "generate_script", return_value=CLEAN_SCRIPT), \
                patch.object(generate, "check_api_key", return_value="fake-key"), \
                patch.object(generate, "run_blender", side_effect=_fake_blender_creates_glb), \
                patch.object(sys, "argv", _argv(self.output_dir)):
            with redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()):
                generate.main()

        # Second run: malicious script -> decision=blocked (SystemExit)
        with patch.object(generate, "generate_script", return_value=MALICIOUS_SCRIPT), \
                patch.object(generate, "generate_script_retry", return_value=MALICIOUS_SCRIPT), \
                patch.object(generate, "check_api_key", return_value="fake-key"), \
                patch.object(generate, "run_blender"), \
                patch.object(sys, "argv", _argv(self.output_dir)):
            with self.assertRaises(SystemExit):
                with redirect_stderr(io.StringIO()):
                    generate.main()

        records = _read_audit(self.output_dir)
        self.assertGreaterEqual(len(records), 2, "audit log should append across runs")
        for rec in records:
            self.assertTrue(self.REQUIRED_KEYS.issubset(rec.keys()),
                            f"record missing keys: {self.REQUIRED_KEYS - set(rec.keys())}")
        decisions = {r["decision"] for r in records}
        self.assertIn("run", decisions)
        self.assertIn("blocked", decisions)

    def test_audit_log_helper_directly(self):
        generate.audit_log(self.output_dir, {
            "script_hash": "abc123",
            "script_path": "/tmp/gen_abc123.py",
            "output_glb": "/tmp/gen_abc123.glb",
            "guard_ok": True,
            "violations": [],
            "decision": "run",
        })
        records = _read_audit(self.output_dir)
        self.assertEqual(len(records), 1)
        self.assertIn("timestamp", records[0])
        self.assertEqual(records[0]["script_hash"], "abc123")


if __name__ == "__main__":
    unittest.main()
