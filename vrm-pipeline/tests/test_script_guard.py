"""
tests/test_script_guard.py
==========================
Unit tests for :mod:`script_guard`.

Run with::

    cd vrm-pipeline
    python -m unittest tests.test_script_guard
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure vrm-pipeline/ is on sys.path so ``import script_guard`` resolves.
HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

from script_guard import (
    GuardViolation,
    UntrustedScriptError,
    assert_script_safe,
    guard_script,
)


# ---------------------------------------------------------------------------
# Minimal benign bpy script used across several tests
# ---------------------------------------------------------------------------

_BENIGN_SCRIPT = """\
import bpy, math, sys
out = sys.argv[-1]
bpy.ops.export_scene.gltf(filepath=out, export_format='GLB')
"""


class TestGuardViolationDataclass(unittest.TestCase):
    """GuardViolation is a well-formed record."""

    def test_fields_accessible(self):
        v = GuardViolation(kind="import", detail="test", lineno=42)
        self.assertEqual(v.kind, "import")
        self.assertEqual(v.detail, "test")
        self.assertEqual(v.lineno, 42)

    def test_lineno_optional(self):
        v = GuardViolation(kind="syntax_error", detail="bad", lineno=None)
        self.assertIsNone(v.lineno)

    def test_str_contains_kind_and_lineno(self):
        v = GuardViolation(kind="import", detail="details here", lineno=7)
        s = str(v)
        self.assertIn("import", s)
        self.assertIn("7", s)

    def test_str_unknown_lineno(self):
        v = GuardViolation(kind="syntax_error", detail="bad", lineno=None)
        s = str(v)
        self.assertIn("unknown", s)

    def test_repr(self):
        v = GuardViolation(kind="dynamic_exec", detail="eval()", lineno=3)
        r = repr(v)
        self.assertIn("GuardViolation", r)
        self.assertIn("dynamic_exec", r)


# ---------------------------------------------------------------------------
# Dangerous import detection
# ---------------------------------------------------------------------------

class TestImportViolations(unittest.TestCase):
    """Imports of os, subprocess, socket (and variants) are flagged."""

    def _kinds(self, script, output_dir=None):
        return [v.kind for v in guard_script(script, output_dir)]

    def test_import_os(self):
        violations = guard_script("import os\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "import")
        self.assertIn("os", violations[0].detail)

    def test_import_subprocess(self):
        violations = guard_script("import subprocess\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "import")
        self.assertIn("subprocess", violations[0].detail)

    def test_import_socket(self):
        violations = guard_script("import socket\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "import")
        self.assertIn("socket", violations[0].detail)

    def test_import_os_path_submodule(self):
        """``import os.path`` counts as ``os``."""
        violations = guard_script("import os.path\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "import")

    def test_from_os_import_system(self):
        violations = guard_script("from os import system\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "import")
        self.assertIn("os", violations[0].detail)

    def test_from_subprocess_import_run(self):
        violations = guard_script("from subprocess import run\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "import")

    def test_from_socket_import_anything(self):
        violations = guard_script("from socket import create_connection\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "import")

    def test_from_os_path_import(self):
        """``from os.path import join`` — top-level is ``os``, flagged."""
        violations = guard_script("from os.path import join\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "import")

    def test_multiple_dangerous_imports(self):
        script = "import os\nimport subprocess\n"
        violations = guard_script(script)
        self.assertEqual(len(violations), 2)
        self.assertTrue(all(v.kind == "import" for v in violations))

    def test_safe_imports_not_flagged(self):
        """bpy, math, sys, json, pathlib, etc. must NOT be flagged."""
        script = "import bpy, math, sys\nimport json\nimport pathlib\n"
        violations = guard_script(script)
        self.assertEqual(violations, [])

    def test_lineno_captured(self):
        script = "x = 1\nimport os\n"
        violations = guard_script(script)
        self.assertEqual(violations[0].lineno, 2)


# ---------------------------------------------------------------------------
# Dynamic exec detection
# ---------------------------------------------------------------------------

class TestDynamicExecViolations(unittest.TestCase):
    """eval / exec / compile / __import__ are flagged."""

    def test_eval_call(self):
        violations = guard_script("eval('1+1')\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "dynamic_exec")
        self.assertIn("eval", violations[0].detail)

    def test_exec_call(self):
        violations = guard_script("exec('pass')\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "dynamic_exec")
        self.assertIn("exec", violations[0].detail)

    def test_compile_call(self):
        violations = guard_script("compile('pass', '<s>', 'exec')\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "dynamic_exec")
        self.assertIn("compile", violations[0].detail)

    def test_dunder_import_call(self):
        violations = guard_script("__import__('os')\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "dynamic_exec")
        self.assertIn("__import__", violations[0].detail)

    def test_exec_multiline(self):
        script = "x = 1\nexec('import os')\n"
        violations = guard_script(script)
        kinds = [v.kind for v in violations]
        self.assertIn("dynamic_exec", kinds)

    def test_lineno_dynamic_exec(self):
        script = "pass\neval('x')\n"
        violations = guard_script(script)
        self.assertEqual(violations[0].lineno, 2)


# ---------------------------------------------------------------------------
# Benign script passes with zero violations
# ---------------------------------------------------------------------------

class TestBenignScript(unittest.TestCase):
    """A minimal bpy export script must produce zero violations."""

    def test_benign_no_violations(self):
        violations = guard_script(_BENIGN_SCRIPT)
        self.assertEqual(violations, [])

    def test_benign_with_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            violations = guard_script(_BENIGN_SCRIPT, output_dir=tmp)
        self.assertEqual(violations, [])

    def test_import_bpy_not_flagged(self):
        violations = guard_script("import bpy\n")
        self.assertEqual(violations, [])

    def test_import_math_not_flagged(self):
        violations = guard_script("import math\n")
        self.assertEqual(violations, [])

    def test_import_sys_not_flagged(self):
        violations = guard_script("import sys\n")
        self.assertEqual(violations, [])


# ---------------------------------------------------------------------------
# File-write detection
# ---------------------------------------------------------------------------

class TestFileWriteViolations(unittest.TestCase):
    """open() in write/append/exclusive/update mode is governed by output_dir."""

    # --- writes inside output_dir are allowed ---------------------------------

    def test_write_inside_output_dir_allowed(self):
        """Literal path inside output_dir must NOT be flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.glb")
            script = f"open({path!r}, 'wb')\n"
            violations = guard_script(script, output_dir=tmp)
        self.assertEqual(violations, [])

    def test_write_inside_output_dir_w_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.txt")
            script = f"open({path!r}, 'w')\n"
            violations = guard_script(script, output_dir=tmp)
        self.assertEqual(violations, [])

    # --- writes outside output_dir are flagged --------------------------------

    def test_write_etc_passwd_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            violations = guard_script("open('/etc/passwd', 'w')\n", output_dir=tmp)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "file_write")

    def test_write_path_traversal_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            violations = guard_script("open('../../evil', 'w')\n", output_dir=tmp)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "file_write")

    def test_write_absolute_outside_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            violations = guard_script("open('/tmp/injected.sh', 'w')\n", output_dir=tmp)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "file_write")

    # --- non-literal path with output_dir → flagged ---------------------------

    def test_write_variable_path_flagged(self):
        """open(var, 'w') — path is not a literal; must be flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            script = "path = '/tmp/x'\nopen(path, 'w')\n"
            violations = guard_script(script, output_dir=tmp)
        kinds = [v.kind for v in violations]
        self.assertIn("file_write", kinds)

    def test_write_expression_path_flagged(self):
        """open(os.path.join(d, 'x'), 'wb') — path not literal; flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            script = f"import bpy\nopen(bpy.path.abspath('//x.glb'), 'wb')\n"
            violations = guard_script(script, output_dir=tmp)
        kinds = [v.kind for v in violations]
        self.assertIn("file_write", kinds)

    # --- output_dir=None → ALL write opens flagged ----------------------------

    def test_write_no_output_dir_flagged(self):
        violations = guard_script("open('/tmp/x', 'w')\n", output_dir=None)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "file_write")

    def test_write_benign_path_no_output_dir_still_flagged(self):
        """Even a 'safe-looking' path is flagged when output_dir=None."""
        violations = guard_script("open('output.glb', 'wb')\n", output_dir=None)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "file_write")

    def test_variable_path_no_output_dir_flagged(self):
        script = "p = 'x'\nopen(p, 'w')\n"
        violations = guard_script(script, output_dir=None)
        kinds = [v.kind for v in violations]
        self.assertIn("file_write", kinds)

    # --- read-only opens are always allowed -----------------------------------

    def test_read_only_default_mode_allowed(self):
        violations = guard_script("open('/etc/passwd')\n", output_dir=None)
        self.assertEqual(violations, [])

    def test_read_only_r_mode_allowed(self):
        violations = guard_script("open('/etc/passwd', 'r')\n", output_dir=None)
        self.assertEqual(violations, [])

    def test_read_only_rb_mode_allowed(self):
        violations = guard_script("open('/etc/passwd', 'rb')\n", output_dir=None)
        self.assertEqual(violations, [])

    # --- various write modes are flagged -------------------------------------

    def test_append_mode_flagged(self):
        violations = guard_script("open('/tmp/log', 'a')\n", output_dir=None)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "file_write")

    def test_exclusive_mode_flagged(self):
        violations = guard_script("open('/tmp/new', 'x')\n", output_dir=None)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "file_write")

    def test_update_mode_flagged(self):
        violations = guard_script("open('/tmp/f', 'r+')\n", output_dir=None)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "file_write")

    def test_write_binary_flagged(self):
        violations = guard_script("open('/tmp/f', 'wb')\n", output_dir=None)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "file_write")

    def test_write_keyword_mode_flagged(self):
        violations = guard_script("open('/tmp/f', mode='w')\n", output_dir=None)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "file_write")


# ---------------------------------------------------------------------------
# Syntax error
# ---------------------------------------------------------------------------

class TestSyntaxError(unittest.TestCase):
    """Unparseable scripts are rejected with kind='syntax_error'."""

    def test_syntax_error_detected(self):
        violations = guard_script("def broken(:\n    pass\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "syntax_error")

    def test_syntax_error_detail_mentions_parse(self):
        violations = guard_script("???")
        self.assertEqual(violations[0].kind, "syntax_error")
        self.assertIn("parsed", violations[0].detail)

    def test_syntax_error_returns_single_violation(self):
        """Only one violation is returned even for completely broken scripts."""
        violations = guard_script("import os\n???\nexec('x')")
        # Because parse fails, we only get the syntax_error, not import/exec.
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "syntax_error")


# ---------------------------------------------------------------------------
# assert_script_safe
# ---------------------------------------------------------------------------

class TestAssertScriptSafe(unittest.TestCase):
    """assert_script_safe raises UntrustedScriptError for dangerous scripts."""

    def test_raises_for_dangerous_script(self):
        with self.assertRaises(UntrustedScriptError) as ctx:
            assert_script_safe("import os\nos.system('rm -rf /')\n")
        exc = ctx.exception
        self.assertIsInstance(exc.violations, list)
        self.assertGreater(len(exc.violations), 0)

    def test_exception_message_lists_violations(self):
        with self.assertRaises(UntrustedScriptError) as ctx:
            assert_script_safe("import os\n")
        msg = str(ctx.exception)
        self.assertIn("violation", msg)

    def test_does_not_raise_for_benign(self):
        # Must not raise.
        assert_script_safe(_BENIGN_SCRIPT)

    def test_raises_for_eval(self):
        with self.assertRaises(UntrustedScriptError):
            assert_script_safe("eval('1+1')\n")

    def test_raises_for_syntax_error(self):
        with self.assertRaises(UntrustedScriptError):
            assert_script_safe("???")

    def test_violations_attribute_on_exception(self):
        try:
            assert_script_safe("import subprocess\nexec('x')\n")
        except UntrustedScriptError as exc:
            self.assertEqual(len(exc.violations), 2)
            kinds = {v.kind for v in exc.violations}
            self.assertIn("import", kinds)
            self.assertIn("dynamic_exec", kinds)
        else:
            self.fail("UntrustedScriptError not raised")

    def test_does_not_raise_with_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.glb")
            script = f"import bpy\nbpy.ops.export_scene.gltf(filepath={path!r})\n"
            assert_script_safe(script, output_dir=tmp)

    def test_raises_for_write_outside_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(UntrustedScriptError):
                assert_script_safe("open('/etc/passwd', 'w')\n", output_dir=tmp)


# ---------------------------------------------------------------------------
# Combined / edge cases
# ---------------------------------------------------------------------------

class TestCombinedViolations(unittest.TestCase):
    """Multiple violations are all reported."""

    def test_all_violations_collected(self):
        script = (
            "import os\n"
            "import subprocess\n"
            "eval('x')\n"
            "open('/etc/passwd', 'w')\n"
        )
        violations = guard_script(script, output_dir="/tmp")
        kinds = [v.kind for v in violations]
        self.assertIn("import", kinds)
        self.assertIn("dynamic_exec", kinds)
        self.assertIn("file_write", kinds)
        # Two import violations
        self.assertEqual(kinds.count("import"), 2)

    def test_empty_script_no_violations(self):
        violations = guard_script("")
        self.assertEqual(violations, [])

    def test_comment_only_script_no_violations(self):
        violations = guard_script("# import os\n# exec('x')\n")
        self.assertEqual(violations, [])

    def test_string_literal_os_not_flagged(self):
        """A string containing 'os' must NOT be flagged."""
        violations = guard_script("x = 'import os'\n")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
