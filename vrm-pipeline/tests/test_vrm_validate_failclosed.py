"""
Fail-closed tests for render.vrm_validate (rulings A and B).

A. The Khronos glTF-Validator is MANDATORY.  Unresolvable binary, crash, abnormal
   exit, timeout, unparseable output, or numErrors > 0 must all resolve to an
   ERROR (cannot determine => block), never a silent pass.
B. VRM meta is a release concern: it WARNS, never blocks (ok stays True).
   Field coverage follows the SPEC.  VRM 1.0 (VRMC_vrm.meta.schema.json,
   required name/authors/licenseUrl): name = string minLength 1; authors =
   array minItems 1 of strings minLength 1; licenseUrl = string (empty allowed).
   Meta object required (VRMC_vrm.schema.json required: specVersion, meta,
   humanoid).  VRM 0.x has no required meta: no meta checks.

Hermetic: the validator is a stub script injected via GLTF_VALIDATOR_PATH and
PATH is pointed at an empty directory so a real installed gltf_validator is
never picked up.  No bpy, no network.
"""

import copy
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
for _p in (PIPELINE_ROOT, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from render import vrm_validate  # noqa: E402
from test_vrm_validate import write_glb, healthy_vrm_0x, healthy_vrm_10  # noqa: E402
import pipeline  # noqa: E402

CLEAN_REPORT = {
    "issues": {
        "numErrors": 0, "numWarnings": 0, "numInfos": 0, "numHints": 0,
        "messages": [], "truncated": False,
    }
}


def with_meta_10(**overrides) -> dict:
    gltf = healthy_vrm_10()
    meta = {"name": "Alice", "authors": ["Bob"], "licenseUrl": "https://example.com/license"}
    meta.update(overrides)
    gltf["extensions"]["VRMC_vrm"]["meta"] = meta
    return gltf


def with_meta_0x(**overrides) -> dict:
    gltf = healthy_vrm_0x()
    meta = {"title": "Alice", "author": "Bob", "licenseName": "CC0"}
    meta.update(overrides)
    gltf["extensions"]["VRM"]["meta"] = meta
    return gltf


class _Hermetic(unittest.TestCase):
    """Temp dir, empty PATH, no inherited GLTF_VALIDATOR_PATH."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.emptybin = self.tmp / "emptybin"
        self.emptybin.mkdir()
        env = patch.dict(os.environ, {"PATH": str(self.emptybin)})
        self.enterContext(env)
        os.environ.pop("GLTF_VALIDATOR_PATH", None)

    def stub(self, body: str, exit_code: int = 0, name: str = "stub_validator") -> Path:
        """Create a stub validator (python script) and point GLTF_VALIDATOR_PATH at it."""
        p = self.tmp / name
        p.write_text(f"#!{sys.executable}\nimport sys\n{body}\nsys.exit({exit_code})\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["GLTF_VALIDATOR_PATH"] = str(p)
        return p

    def clean_stub(self):
        return self.stub(f"print({json.dumps(json.dumps(CLEAN_REPORT))})")

    def vrm(self, gltf: dict, name: str = "a.vrm") -> Path:
        return write_glb(self.tmp / name, gltf)

    def assert_blocked(self, report, *needles):
        self.assertFalse(report["ok"], report)
        self.assertTrue(report["errors"], report)
        blob = " | ".join(report["errors"]).lower()
        for n in needles:
            self.assertIn(n.lower(), blob, f"{n!r} not named in errors: {report['errors']}")


class TestValidatorMandatory(_Hermetic):
    def _validate(self):
        return vrm_validate.validate_vrm(self.vrm(with_meta_10()))

    def test_control_clean_stub_report_is_ok(self):
        self.clean_stub()
        report = self._validate()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["errors"], [])

    def test_absent_validator_is_error(self):
        report = self._validate()  # no env var, empty PATH
        self.assert_blocked(report, "validator")

    def test_env_path_to_nonexistent_binary_is_error(self):
        os.environ["GLTF_VALIDATOR_PATH"] = str(self.tmp / "does-not-exist")
        self.assert_blocked(self._validate(), "validator")

    def test_nonexecutable_binary_is_error(self):
        p = self.tmp / "notexec"
        p.write_text("not a program")
        p.chmod(0o644)
        os.environ["GLTF_VALIDATOR_PATH"] = str(p)
        self.assert_blocked(self._validate(), "validator")

    def test_crash_nonzero_exit_without_output_is_error(self):
        self.stub("sys.stderr.write('segfault')", exit_code=139)
        self.assert_blocked(self._validate(), "validator")

    def test_nonzero_exit_with_clean_looking_report_is_error(self):
        # Abnormal exit must not be trusted even if stdout looks clean.
        self.stub(f"print({json.dumps(json.dumps(CLEAN_REPORT))})", exit_code=70)
        self.assert_blocked(self._validate(), "validator")

    def test_garbage_stdout_is_error(self):
        self.stub("print('this is not json at all')")
        self.assert_blocked(self._validate(), "validator")

    def test_empty_stdout_is_error(self):
        self.stub("pass")
        self.assert_blocked(self._validate(), "validator")

    def test_json_without_issues_is_error(self):
        self.stub("print('{\"foo\": 1}')")
        self.assert_blocked(self._validate(), "validator")

    def test_issues_not_a_dict_is_error(self):
        self.stub("print('{\"issues\": []}')")
        self.assert_blocked(self._validate(), "validator")

    def test_issues_without_numerrors_is_error(self):
        self.stub("print('{\"issues\": {\"numWarnings\": 0}}')")
        self.assert_blocked(self._validate(), "validator")

    def test_numerrors_wrong_type_is_error(self):
        self.stub("print('{\"issues\": {\"numErrors\": \"0\"}}')")
        self.assert_blocked(self._validate(), "validator")

    def test_timeout_is_error(self):
        self.clean_stub()
        real_run = subprocess.run

        def _timeout(cmd, *a, **k):
            if str(cmd[0]) == os.environ["GLTF_VALIDATOR_PATH"]:
                raise subprocess.TimeoutExpired(cmd, 120)
            return real_run(cmd, *a, **k)

        with patch("render.vrm_validate.subprocess.run", side_effect=_timeout):
            report = self._validate()
        self.assert_blocked(report, "validator")

    def test_numerrors_positive_is_error(self):
        rep = copy.deepcopy(CLEAN_REPORT)
        rep["issues"]["numErrors"] = 3
        self.stub(f"print({json.dumps(json.dumps(rep))})", exit_code=1)
        self.assert_blocked(self._validate(), "validator", "3")

    def test_numerrors_positive_with_exit_zero_is_error(self):
        rep = copy.deepcopy(CLEAN_REPORT)
        rep["issues"]["numErrors"] = 1
        self.stub(f"print({json.dumps(json.dumps(rep))})", exit_code=0)
        self.assert_blocked(self._validate(), "validator")

    def test_assert_valid_vrm_raises_when_validator_absent(self):
        p = self.vrm(with_meta_10())
        rp = self.tmp / "validation.json"
        with self.assertRaises(RuntimeError):
            vrm_validate.assert_valid_vrm(p, report_path=rp)
        self.assertFalse(json.loads(rp.read_text())["ok"])


class TestMetaV10(_Hermetic):
    def setUp(self):
        super().setUp()
        self.clean_stub()  # isolate: any error must come from meta

    # Policy: VRM meta is a release/publishing concern.  Missing or malformed
    # meta WARNS (named field in `warnings`) and never blocks (`errors` empty,
    # ok stays True).  The clean validator stub guarantees nothing else errs.
    def assert_warned(self, report, *needles):
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["errors"], [], report)
        blob = " | ".join(report["warnings"]).lower()
        self.assertTrue(report["warnings"], report)
        for n in needles:
            self.assertIn(n.lower(), blob, f"{n!r} not named in warnings: {report['warnings']}")

    def _check(self, gltf, field):
        self.assert_warned(vrm_validate.validate_vrm(self.vrm(gltf)), field)

    def test_complete_meta_no_error(self):
        report = vrm_validate.validate_vrm(self.vrm(with_meta_10()))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["version"], "1.0")

    def test_meta_object_absent(self):
        self._check(healthy_vrm_10(), "meta")

    def test_meta_not_an_object(self):
        gltf = healthy_vrm_10()
        gltf["extensions"]["VRMC_vrm"]["meta"] = "oops"
        self._check(gltf, "meta")

    def test_meta_empty_object_names_all_fields(self):
        gltf = healthy_vrm_10()
        gltf["extensions"]["VRMC_vrm"]["meta"] = {}
        self.assert_warned(vrm_validate.validate_vrm(self.vrm(gltf)),
                           "name", "authors", "licenseUrl")

    def test_name_missing(self):
        gltf = with_meta_10()
        del gltf["extensions"]["VRMC_vrm"]["meta"]["name"]
        self._check(gltf, "name")

    def test_name_empty(self):
        self._check(with_meta_10(name=""), "name")

    def test_name_wrong_type(self):
        self._check(with_meta_10(name=123), "name")

    def test_authors_missing(self):
        gltf = with_meta_10()
        del gltf["extensions"]["VRMC_vrm"]["meta"]["authors"]
        self._check(gltf, "authors")

    def test_authors_empty_array(self):
        self._check(with_meta_10(authors=[]), "authors")

    def test_authors_wrong_type_string(self):
        self._check(with_meta_10(authors="Bob"), "authors")

    def test_authors_contains_empty_string(self):
        self._check(with_meta_10(authors=["Bob", ""]), "authors")

    def test_authors_contains_non_string(self):
        self._check(with_meta_10(authors=[42]), "authors")

    def test_license_url_missing(self):
        gltf = with_meta_10()
        del gltf["extensions"]["VRMC_vrm"]["meta"]["licenseUrl"]
        self._check(gltf, "licenseUrl")

    def test_license_url_empty_string_allowed(self):
        # Schema: licenseUrl is {"type": "string"} with no minLength.
        report = vrm_validate.validate_vrm(self.vrm(with_meta_10(licenseUrl="")))
        self.assertTrue(report["ok"], report)

    def test_license_url_wrong_type(self):
        self._check(with_meta_10(licenseUrl=["https://x"]), "licenseUrl")


class TestMetaV0x(_Hermetic):
    """Spec (0.0 vrm.meta.schema.json) has no required meta: no meta errors."""

    def setUp(self):
        super().setUp()
        self.clean_stub()

    def test_complete_meta_no_error(self):
        report = vrm_validate.validate_vrm(self.vrm(with_meta_0x()))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["version"], "0.x")

    def test_meta_absent_no_meta_error(self):
        report = vrm_validate.validate_vrm(self.vrm(healthy_vrm_0x()))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["warnings"], [])

    def test_meta_empty_object_no_meta_error(self):
        gltf = healthy_vrm_0x()
        gltf["extensions"]["VRM"]["meta"] = {}
        report = vrm_validate.validate_vrm(self.vrm(gltf))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["warnings"], [])


class TestPipelineGateFailClosed(_Hermetic):
    def test_absent_validator_not_admitted_to_ledger(self):
        out = self.tmp / "out"
        out.mkdir()
        vrm_path = self.vrm(with_meta_10(), "model.vrm")  # otherwise perfect asset
        ran = []

        def _fake_run(cmd, **kwargs):  # would be `ledger insert`
            ran.append(cmd)
            res = MagicMock()
            res.returncode, res.stdout, res.stderr = 0, "rec-1", ""
            return res

        with patch("subprocess.run", side_effect=_fake_run):
            with self.assertRaises(RuntimeError):
                report = pipeline._validate_vrm_gate(vrm_path, out)
                pipeline._ledger_insert(Path("db"), "p", str(out), {"v": report})
        self.assertEqual(ran, [], "ledger insert must not run without a validator")
        vj = out / "validation.json"
        self.assertTrue(vj.exists())
        self.assertFalse(json.loads(vj.read_text())["ok"])

    def test_missing_meta_admitted_with_warning(self):
        # Meta is a release concern: admitted, warning recorded in validation.json.
        self.clean_stub()
        out = self.tmp / "out"
        out.mkdir()
        vrm_path = self.vrm(healthy_vrm_10(), "model.vrm")  # no meta
        report = pipeline._validate_vrm_gate(vrm_path, out)
        self.assertTrue(report["ok"], report)
        vj = json.loads((out / "validation.json").read_text())
        self.assertTrue(vj["ok"], vj)
        self.assertEqual(vj["errors"], [])
        self.assertIn("meta", " | ".join(vj["warnings"]).lower())

    def test_complete_asset_with_clean_validator_admitted(self):
        self.clean_stub()
        out = self.tmp / "out"
        out.mkdir()
        report = pipeline._validate_vrm_gate(self.vrm(with_meta_10(), "model.vrm"), out)
        self.assertTrue(report["ok"], report)


if __name__ == "__main__":
    unittest.main()
