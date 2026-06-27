"""
tests/test_generate_ssrf.py

Security tests for the SSRF / API-key-exfiltration guard on the Hyper3D (Rodin)
download path in generate.py.

generate_via_hyper3d() submits a job to an env-overridable endpoint
(HYPER3D_ENDPOINT), then downloads a GLB from a `glb_url` that comes straight
out of the API's JSON response. A compromised or malicious endpoint can return
an attacker-controlled URL; blindly attaching `Authorization: Bearer <api_key>`
to it would leak the key. The guard must:

  * only send credentials to the configured endpoint host (allow-list),
  * reject cross-origin hosts and non-http(s) schemes *before* any request,
  * allow legitimate extra hosts via HYPER3D_DOWNLOAD_HOSTS,
  * re-validate redirect targets.

These tests are hermetic: no real network is touched. Either validation raises
before any send, or the opener/urlopen is mocked.

Run with:
    cd vrm-pipeline
    python -m unittest tests.test_generate_ssrf
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure vrm-pipeline/ is on sys.path so `import generate` works.
HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import generate  # noqa: E402

ENDPOINT = "https://hyperhuman.deemos.com/api/v2/rodin"


class TestAssertUrlAllowed(unittest.TestCase):
    def test_same_host_passes(self):
        # Should not raise.
        generate._assert_url_allowed(
            "https://hyperhuman.deemos.com/files/model.glb",
            {"hyperhuman.deemos.com"},
        )

    def test_cross_origin_host_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            generate._assert_url_allowed(
                "https://evil.attacker.example/steal.glb",
                {"hyperhuman.deemos.com"},
            )
        self.assertIn("cross-origin", str(ctx.exception))

    def test_non_http_scheme_rejected(self):
        for url in (
            "file:///etc/passwd",
            "ftp://hyperhuman.deemos.com/x.glb",
            "gopher://hyperhuman.deemos.com/",
        ):
            with self.assertRaises(RuntimeError) as ctx:
                generate._assert_url_allowed(url, {"hyperhuman.deemos.com"})
            self.assertIn("non-http", str(ctx.exception))

    def test_missing_host_rejected(self):
        with self.assertRaises(RuntimeError):
            generate._assert_url_allowed("https:///no-host.glb", set())


class TestAllowedDownloadHosts(unittest.TestCase):
    def test_derives_host_from_endpoint(self):
        hosts = generate._allowed_download_hosts(ENDPOINT)
        self.assertIn("hyperhuman.deemos.com", hosts)

    def test_env_extends_allow_list(self):
        with patch.dict(
            "os.environ",
            {"HYPER3D_DOWNLOAD_HOSTS": "cdn.example.com, files.example.net"},
        ):
            hosts = generate._allowed_download_hosts(ENDPOINT)
        self.assertIn("hyperhuman.deemos.com", hosts)
        self.assertIn("cdn.example.com", hosts)
        self.assertIn("files.example.net", hosts)


class TestHttpGetAllowlist(unittest.TestCase):
    def test_allowed_host_downloads(self):
        allowed = {"hyperhuman.deemos.com"}
        fake_resp = MagicMock()
        fake_resp.read.return_value = b"GLB-BYTES"
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False
        fake_opener = MagicMock()
        fake_opener.open.return_value = fake_resp

        with patch("urllib.request.build_opener", return_value=fake_opener) as bo:
            out = generate._http_get(
                "https://hyperhuman.deemos.com/files/model.glb",
                headers={"Authorization": "Bearer secret"},
                allowed_hosts=allowed,
            )
        self.assertEqual(out, b"GLB-BYTES")
        bo.assert_called_once()
        fake_opener.open.assert_called_once()

    def test_cross_origin_rejected_before_any_send(self):
        allowed = {"hyperhuman.deemos.com"}
        # If validation works, build_opener and urlopen must never be reached,
        # so the Bearer token is never transmitted anywhere.
        with patch("urllib.request.build_opener") as bo, patch(
            "urllib.request.urlopen"
        ) as uo:
            with self.assertRaises(RuntimeError) as ctx:
                generate._http_get(
                    "https://evil.attacker.example/steal.glb",
                    headers={"Authorization": "Bearer secret"},
                    allowed_hosts=allowed,
                )
        self.assertIn("cross-origin", str(ctx.exception))
        bo.assert_not_called()
        uo.assert_not_called()

    def test_legacy_unrestricted_path_still_works(self):
        # allowed_hosts=None preserves the original (unrestricted) behavior.
        fake_resp = MagicMock()
        fake_resp.read.return_value = b"DATA"
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False
        with patch("urllib.request.urlopen", return_value=fake_resp) as uo:
            out = generate._http_get("https://anywhere.example/x", headers={})
        self.assertEqual(out, b"DATA")
        uo.assert_called_once()


class TestRedirectHandler(unittest.TestCase):
    def test_redirect_to_disallowed_host_rejected(self):
        handler = generate._HostAllowlistRedirectHandler({"hyperhuman.deemos.com"})
        with self.assertRaises(RuntimeError):
            handler.redirect_request(
                req=MagicMock(),
                fp=None,
                code=302,
                msg="Found",
                headers={},
                newurl="https://evil.attacker.example/steal.glb",
            )


if __name__ == "__main__":
    unittest.main()
