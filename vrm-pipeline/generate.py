"""
generate.py — Gemini-driven Blender Python script generation with retry,
              with optional Hyper3D (Rodin) image-to-3D backend.

Usage (Gemini/Blender backend, original):
    python generate.py \
        --prompt "中世風の木製椅子" \
        --output-dir ./watched/objects/ \
        [--max-retries 3] \
        [--blender-path blender] \
        [--model gemini-2.5-flash]

Usage (Hyper3D/Rodin image-to-3D backend):
    export RODIN_API_KEY=<your_key>
    python generate.py \
        --image ./photo.png \
        --output-dir ./watched/objects/ \
        [--prompt "optional extra prompt hint"] \
        [--hyper3d-mode MAIN_SITE|FAL_AI] \
        [--hyper3d-endpoint https://hyperhuman.deemos.com/api/v2/rodin]

Environment variables for Hyper3D backend:
    RODIN_API_KEY      (or HYPER3D_API_KEY) — required when --image is used
    HYPER3D_MODE       — MAIN_SITE (default) or FAL_AI
    HYPER3D_ENDPOINT   — override the default API endpoint URL
"""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import script_guard

SYSTEM_INSTRUCTION = """\
You are a Blender 4.2 Python script generator.
Write a COMPLETE, SELF-CONTAINED bpy script that:
1. Creates the described 3D object using Blender Python API (bpy)
2. Applies appropriate Principled BSDF materials with realistic colors
3. Exports to the GLB path from sys.argv: the script must read the output path as:
   import sys; output_path = sys.argv[sys.argv.index('--output') + 1]
   then call: bpy.ops.export_scene.gltf(filepath=output_path, export_format='GLB')
4. Prints to stdout one line of JSON: {"poly_count": N, "dimensions": {"x": X, "y": Y, "z": Z}}
   where dimensions are in meters from the bounding box.
5. Do NOT call bpy.ops.wm.quit_blender() - Blender exits automatically.
Return ONLY the Python script. No markdown, no explanation, no ```python blocks.\
"""

RETRY_PROMPT_TEMPLATE = """\
The previous script failed. Error output:
<error>
{stderr}
</error>
Please fix the script and return a corrected version.
Original request: {prompt}
Return ONLY the corrected Python script.\
"""

# ---------------------------------------------------------------------------
# Hyper3D (Rodin) default endpoints
# TODO: confirm endpoint shape with official Rodin API documentation
# ---------------------------------------------------------------------------
_RODIN_MAIN_SITE_DEFAULT = "https://hyperhuman.deemos.com/api/v2/rodin"
_RODIN_FAL_AI_DEFAULT = "https://fal.run/fal-ai/hyper3d-rodin"


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Generate a 3D GLB via Gemini-written Blender script (default) "
            "or Hyper3D/Rodin image-to-3D backend (--image)."
        )
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help=(
            "Description of the 3D object to generate. "
            "Required when --image is not used; optional hint when --image is used."
        ),
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where the .glb file will be saved.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum number of retry attempts for the Gemini/Blender backend (default: 3).",
    )
    parser.add_argument(
        "--blender-path",
        default="blender",
        help="Path to the blender executable (default: blender).",
    )
    parser.add_argument(
        "--model",
        default="gemini-2.5-flash",
        help="Gemini model name (default: gemini-2.5-flash).",
    )
    # --- Hyper3D / Rodin image-to-3D options ---
    parser.add_argument(
        "--image",
        default=None,
        metavar="PATH",
        help=(
            "Path to an input image for Hyper3D (Rodin) image-to-3D generation. "
            "When specified, RODIN_API_KEY (or HYPER3D_API_KEY) must be set. "
            "Skips the Gemini/Blender pipeline."
        ),
    )
    parser.add_argument(
        "--hyper3d-mode",
        default=None,
        choices=["MAIN_SITE", "FAL_AI"],
        metavar="MODE",
        help=(
            "Hyper3D backend mode: MAIN_SITE (default) or FAL_AI. "
            "Can also be set via HYPER3D_MODE environment variable."
        ),
    )
    parser.add_argument(
        "--hyper3d-endpoint",
        default=None,
        metavar="URL",
        help=(
            "Override Hyper3D/Rodin API endpoint URL. "
            "Can also be set via HYPER3D_ENDPOINT environment variable."
        ),
    )
    parser.add_argument(
        "--hyper3d-poll-interval",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Polling interval (seconds) for Hyper3D job status (default: 5.0).",
    )
    parser.add_argument(
        "--hyper3d-timeout",
        type=float,
        default=600.0,
        metavar="SECONDS",
        help="Maximum wait time (seconds) for Hyper3D job completion (default: 600).",
    )

    args = parser.parse_args()

    # Validate: --prompt required when --image is not used
    if args.image is None and args.prompt is None:
        parser.error("--prompt is required when --image is not specified.")

    return args


# ---------------------------------------------------------------------------
# Hyper3D / Rodin backend
# ---------------------------------------------------------------------------

def _get_hyper3d_config(args):
    """
    Resolve Hyper3D configuration from CLI args and environment variables.
    Returns (api_key, mode, endpoint).
    Raises SystemExit(1) if RODIN_API_KEY / HYPER3D_API_KEY is not set.
    """
    api_key = (
        os.environ.get("RODIN_API_KEY")
        or os.environ.get("HYPER3D_API_KEY")
    )
    if not api_key:
        print(
            "Error: Hyper3D backend not configured: set RODIN_API_KEY (or HYPER3D_API_KEY).\n"
            "  export RODIN_API_KEY=your_api_key_here\n"
            "\n"
            "Optional environment variables:\n"
            "  HYPER3D_MODE      MAIN_SITE (default) or FAL_AI\n"
            "  HYPER3D_ENDPOINT  override the API endpoint URL",
            file=sys.stderr,
        )
        sys.exit(1)

    mode = (
        args.hyper3d_mode
        or os.environ.get("HYPER3D_MODE", "MAIN_SITE")
    )
    if mode not in ("MAIN_SITE", "FAL_AI"):
        print(
            f"Error: Unknown HYPER3D_MODE '{mode}'. Must be MAIN_SITE or FAL_AI.",
            file=sys.stderr,
        )
        sys.exit(1)

    endpoint = (
        args.hyper3d_endpoint
        or os.environ.get("HYPER3D_ENDPOINT")
    )
    if not endpoint:
        endpoint = (
            _RODIN_MAIN_SITE_DEFAULT
            if mode == "MAIN_SITE"
            else _RODIN_FAL_AI_DEFAULT
        )

    return api_key, mode, endpoint


def _http_post_json(url: str, headers: dict, body: dict) -> dict:
    """POST JSON body, return parsed JSON response. Raises RuntimeError on failure."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Hyper3D API HTTP error {e.code} at {url}: {body_text}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Hyper3D API connection error at {url}: {e.reason}"
        ) from e


def _http_post_multipart(url: str, headers: dict, fields: dict, files: dict) -> dict:
    """
    POST multipart/form-data.  `files` is {field_name: (filename, bytes, content_type)}.
    Returns parsed JSON response. Raises RuntimeError on failure.
    """
    boundary = hashlib.sha256(os.urandom(16)).hexdigest()[:32]
    body_parts = []

    for name, value in fields.items():
        body_parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'
        )

    for name, (filename, data, ctype) in files.items():
        body_parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f'Content-Type: {ctype}\r\n\r\n'
        )
        # We'll encode the whole body as bytes below

    # Encode properly
    encoded = b""
    for name, value in fields.items():
        encoded += (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'
        ).encode("utf-8")
    for name, (filename, data, ctype) in files.items():
        encoded += (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f'Content-Type: {ctype}\r\n\r\n'
        ).encode("utf-8")
        encoded += data
        encoded += b"\r\n"
    encoded += f"--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        url,
        data=encoded,
        headers={
            **headers,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Hyper3D API HTTP error {e.code} at {url}: {body_text}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Hyper3D API connection error at {url}: {e.reason}"
        ) from e


def _http_get(url: str, headers: dict) -> bytes:
    """GET request, returns raw bytes. Raises RuntimeError on failure."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP GET error {e.code} at {url}: {body_text}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"HTTP GET connection error at {url}: {e.reason}"
        ) from e


def generate_via_hyper3d(
    image_path: str,
    output_dir: str,
    api_key: str,
    mode: str = "MAIN_SITE",
    endpoint: str = _RODIN_MAIN_SITE_DEFAULT,
    prompt: str = "",
    poll_interval: float = 5.0,
    timeout: float = 600.0,
) -> str:
    """
    Submit an image-to-3D job to Hyper3D (Rodin), poll until done, download GLB.

    Args:
        image_path:    Path to the input image file.
        output_dir:    Directory to save the downloaded GLB.
        api_key:       Rodin API key.
        mode:          "MAIN_SITE" or "FAL_AI".
        endpoint:      API endpoint URL (override via HYPER3D_ENDPOINT).
        prompt:        Optional text prompt hint.
        poll_interval: Seconds between status polls.
        timeout:       Max seconds to wait for job completion.

    Returns:
        Absolute path to the downloaded GLB file.

    Raises:
        RuntimeError: On API errors or unexpected response shapes.
        TimeoutError: If the job does not complete within `timeout` seconds.

    # TODO: confirm endpoint shape with official Rodin API documentation at
    #       https://hyperhuman.deemos.com/docs  (MAIN_SITE mode)
    #       https://fal.ai/models/fal-ai/hyper3d-rodin (FAL_AI mode)
    """
    if not os.path.isfile(image_path):
        raise RuntimeError(f"Input image not found: {image_path}")

    image_name = os.path.basename(image_path)
    with open(image_path, "rb") as fh:
        image_bytes = fh.read()

    # Detect MIME type from extension
    ext = os.path.splitext(image_name)[1].lower()
    mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    image_mime = mime_map.get(ext, "application/octet-stream")

    auth_headers = {"Authorization": f"Bearer {api_key}"}

    if mode == "MAIN_SITE":
        # --- MAIN_SITE: multipart submit → poll status → download asset ---
        # TODO: confirm exact field names with Rodin MAIN_SITE API documentation
        submit_url = endpoint  # e.g. https://hyperhuman.deemos.com/api/v2/rodin
        fields = {}
        if prompt:
            fields["prompt"] = prompt

        print(f"[Hyper3D] Submitting job to {submit_url} ...", file=sys.stderr)
        submit_resp = _http_post_multipart(
            url=submit_url,
            headers=auth_headers,
            fields=fields,
            files={"image": (image_name, image_bytes, image_mime)},
        )

        # TODO: confirm response key names from Rodin API documentation
        job_id = submit_resp.get("uuid") or submit_resp.get("job_id") or submit_resp.get("id")
        if not job_id:
            raise RuntimeError(
                f"Hyper3D submit response missing job ID. Response: {submit_resp}"
            )

        # Status polling
        # TODO: confirm status endpoint path from Rodin API documentation
        status_url = f"{endpoint.rstrip('/')}/jobs/status"
        print(f"[Hyper3D] Job submitted (id={job_id}). Polling status ...", file=sys.stderr)

        deadline = time.time() + timeout
        while True:
            if time.time() > deadline:
                raise TimeoutError(
                    f"Hyper3D job {job_id} did not complete within {timeout}s."
                )

            status_resp = _http_post_json(
                url=status_url,
                headers=auth_headers,
                body={"task_uuid": job_id},
            )

            # TODO: confirm status field names from Rodin API documentation
            status = (
                status_resp.get("status")
                or status_resp.get("state")
                or ""
            ).upper()

            print(f"[Hyper3D] Job status: {status}", file=sys.stderr)

            if status in ("DONE", "SUCCEEDED", "COMPLETED", "SUCCESS"):
                break
            if status in ("FAILED", "ERROR", "CANCELLED"):
                raise RuntimeError(
                    f"Hyper3D job {job_id} failed with status '{status}'. "
                    f"Response: {status_resp}"
                )

            time.sleep(poll_interval)

        # Download GLB asset
        # TODO: confirm download endpoint path from Rodin API documentation
        download_url_base = f"{endpoint.rstrip('/')}/jobs/download_subscriptions"
        download_resp = _http_post_json(
            url=download_url_base,
            headers=auth_headers,
            body={"task_uuid": job_id},
        )

        # TODO: confirm download URL key from Rodin API documentation
        glb_url = (
            download_resp.get("glb_url")
            or download_resp.get("download_url")
            or download_resp.get("url")
        )
        if not glb_url:
            raise RuntimeError(
                f"Hyper3D download response missing GLB URL. Response: {download_resp}"
            )

    elif mode == "FAL_AI":
        # --- FAL_AI: JSON submit with base64 image → poll output URL ---
        # TODO: confirm exact field names with fal.ai Hyper3D Rodin API documentation
        import base64
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        image_data_uri = f"data:{image_mime};base64,{image_b64}"

        submit_payload = {"image_url": image_data_uri}
        if prompt:
            submit_payload["prompt"] = prompt

        print(f"[Hyper3D/FAL_AI] Submitting job to {endpoint} ...", file=sys.stderr)
        submit_resp = _http_post_json(
            url=endpoint,
            headers=auth_headers,
            body=submit_payload,
        )

        # TODO: confirm response key from fal.ai API documentation
        job_id = submit_resp.get("request_id") or submit_resp.get("id")
        if not job_id:
            # FAL_AI may return the result directly if synchronous
            glb_url = (
                submit_resp.get("glb_url")
                or submit_resp.get("model_url")
                or submit_resp.get("url")
            )
            if not glb_url:
                raise RuntimeError(
                    f"Hyper3D/FAL_AI response missing both job ID and GLB URL. "
                    f"Response: {submit_resp}"
                )
            job_id = None
        else:
            glb_url = None

        if job_id and not glb_url:
            # Async: poll status endpoint
            # TODO: confirm FAL_AI status endpoint path
            status_base = endpoint.rstrip("/").rsplit("/", 1)[0]
            deadline = time.time() + timeout
            print(f"[Hyper3D/FAL_AI] Job submitted (id={job_id}). Polling ...", file=sys.stderr)

            while True:
                if time.time() > deadline:
                    raise TimeoutError(
                        f"Hyper3D/FAL_AI job {job_id} did not complete within {timeout}s."
                    )

                status_url = f"{status_base}/requests/{job_id}/status"
                status_resp = _http_post_json(
                    url=status_url,
                    headers=auth_headers,
                    body={},
                )
                status = (
                    status_resp.get("status") or status_resp.get("state") or ""
                ).upper()

                print(f"[Hyper3D/FAL_AI] Job status: {status}", file=sys.stderr)

                if status in ("COMPLETED", "DONE", "SUCCEEDED", "SUCCESS"):
                    glb_url = (
                        status_resp.get("output", {}).get("glb_url")
                        or status_resp.get("glb_url")
                        or status_resp.get("url")
                    )
                    if not glb_url:
                        raise RuntimeError(
                            f"Hyper3D/FAL_AI job {job_id} completed but no GLB URL found. "
                            f"Response: {status_resp}"
                        )
                    break
                if status in ("FAILED", "ERROR", "CANCELLED"):
                    raise RuntimeError(
                        f"Hyper3D/FAL_AI job {job_id} failed. Response: {status_resp}"
                    )

                time.sleep(poll_interval)
    else:
        raise RuntimeError(f"Unknown Hyper3D mode: {mode}")

    # Download the GLB file
    print(f"[Hyper3D] Downloading GLB from {glb_url} ...", file=sys.stderr)
    glb_bytes = _http_get(glb_url, headers=auth_headers)

    image_stem = os.path.splitext(image_name)[0]
    glb_filename = f"hyper3d_{image_stem}_{job_id or 'result'}.glb"
    glb_path = os.path.join(output_dir, glb_filename)
    with open(glb_path, "wb") as fh:
        fh.write(glb_bytes)

    print(f"[Hyper3D] GLB saved to {glb_path}", file=sys.stderr)
    return glb_path


# ---------------------------------------------------------------------------
# Gemini / Blender backend (original)
# ---------------------------------------------------------------------------

def check_api_key():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(
            "Error: GEMINI_API_KEY environment variable is not set.\n"
            "Please export your Gemini API key before running this tool:\n"
            "  export GEMINI_API_KEY=your_api_key_here",
            file=sys.stderr,
        )
        sys.exit(1)
    return api_key


def script_hash(script_text: str) -> str:
    return hashlib.sha256(script_text.encode("utf-8")).hexdigest()[:12]


def save_script(script_text: str, sha: str) -> str:
    script_path = os.path.join(tempfile.gettempdir(), f"gen_{sha}.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_text)
    return script_path


_KILLSWITCH_ENV = "PIPELINE_BLOCK_UNTRUSTED_CODE"
_TRUTHY = frozenset({"1", "true", "yes"})


def _killswitch_enabled() -> bool:
    """Return True when the policy kill switch env var is set to a truthy value."""
    val = os.environ.get(_KILLSWITCH_ENV, "")
    return val.strip().lower() in _TRUTHY


def audit_log(output_dir: str, record: dict) -> None:
    """Append a single JSON line describing a code-execution decision.

    Writes to ``<output_dir>/.code_audit.jsonl`` in append-only mode (the file
    is created if missing and never truncated).  A ``timestamp`` (ISO-8601 UTC)
    is added when not already present.  Audit logging must never crash the
    pipeline, so I/O errors are swallowed (best-effort to stderr).
    """
    rec = dict(record)
    rec.setdefault(
        "timestamp",
        datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    audit_path = os.path.join(output_dir, ".code_audit.jsonl")
    try:
        with open(audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover - best effort
        print(f"Warning: failed to write audit log {audit_path}: {exc}", file=sys.stderr)


def run_blender(blender_path: str, script_path: str, output_glb: str):
    """Run blender headless and return (exit_code, stdout, stderr)."""
    cmd = [
        blender_path,
        "--background",
        "--python", script_path,
        "--",
        "--output", output_glb,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def parse_metadata(stdout: str) -> dict:
    """Extract the first valid JSON line from blender stdout."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            return data
        except Exception:
            continue
    return {}


def generate_script(client, model_name: str, user_prompt: str) -> str:
    import google.generativeai as genai
    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    response = model.generate_content(user_prompt)
    return response.text.strip()


def generate_script_retry(client, model_name: str, user_prompt: str, stderr: str) -> str:
    import google.generativeai as genai
    retry_prompt = RETRY_PROMPT_TEMPLATE.format(stderr=stderr, prompt=user_prompt)
    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    response = model.generate_content(retry_prompt)
    return response.text.strip()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # --- Hyper3D image-to-3D path ---
    if args.image is not None:
        api_key, mode, endpoint = _get_hyper3d_config(args)

        try:
            glb_path = generate_via_hyper3d(
                image_path=os.path.abspath(args.image),
                output_dir=output_dir,
                api_key=api_key,
                mode=mode,
                endpoint=endpoint,
                prompt=args.prompt or "",
                poll_interval=args.hyper3d_poll_interval,
                timeout=args.hyper3d_timeout,
            )
        except (RuntimeError, TimeoutError) as exc:
            print(f"Error: Hyper3D generation failed: {exc}", file=sys.stderr)
            sys.exit(1)

        result = {
            "output_glb": glb_path,
            "model": f"hyper3d/{mode}",
            "prompt": args.prompt or "",
            "image": os.path.abspath(args.image),
            "hyper3d_mode": mode,
            "hyper3d_endpoint": endpoint,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # --- Gemini / Blender path (original) ---
    import google.generativeai as genai

    api_key = check_api_key()
    genai.configure(api_key=api_key)

    last_stderr = ""
    script_text = None
    sha = None
    script_path = None

    for attempt in range(args.max_retries + 1):
        if attempt == 0:
            script_text = generate_script(None, args.model, args.prompt)
        else:
            script_text = generate_script_retry(None, args.model, args.prompt, last_stderr)

        sha = script_hash(script_text)
        script_path = save_script(script_text, sha)
        output_glb = os.path.join(output_dir, f"gen_{sha}.glb")

        # --- Untrusted code gate: static guard + consent + kill switch + audit ---
        # The script returned by Gemini is UNTRUSTED arbitrary code that we are
        # about to hand to Blender for execution at host privilege.  Inspect it
        # statically first; never run a script that fails the guard.
        violations = script_guard.guard_script(script_text, output_dir=output_dir)
        violation_strs = [str(v) for v in violations]

        if violations:
            print(
                f"SECURITY: refusing to execute generated script {sha}: "
                f"{len(violations)} guard violation(s) detected.",
                file=sys.stderr,
            )
            for v in violations:
                print(
                    f"  [{v.kind}] line {v.lineno}: {v.detail}",
                    file=sys.stderr,
                )
            print(f"  script_hash={sha} script_path={script_path}", file=sys.stderr)
            audit_log(output_dir, {
                "script_hash": sha,
                "script_path": script_path,
                "output_glb": output_glb,
                "guard_ok": False,
                "violations": violation_strs,
                "decision": "blocked",
                "reason": "guard_violation",
            })
            # Retrying an injected/malicious script is pointless: fail hard.
            sys.exit(1)

        # Kill switch: refuse execution by policy even when the guard passes.
        if _killswitch_enabled():
            print(
                f"SECURITY: execution of generated script {sha} blocked by policy "
                f"({_KILLSWITCH_ENV} is set). Not running Blender.",
                file=sys.stderr,
            )
            audit_log(output_dir, {
                "script_hash": sha,
                "script_path": script_path,
                "output_glb": output_glb,
                "guard_ok": True,
                "violations": [],
                "decision": "blocked",
                "reason": "policy_killswitch",
            })
            sys.exit(1)

        # Consent banner: make the untrusted-code execution visible on stderr.
        print(
            "============================================================\n"
            "WARNING: about to execute LLM-GENERATED, UNVERIFIED Python code\n"
            "         with Blender at host privilege. This code was produced\n"
            "         by a language model and has only passed a static guard.\n"
            f"         script_hash={sha}\n"
            f"         script_path={script_path}\n"
            "============================================================",
            file=sys.stderr,
        )

        audit_log(output_dir, {
            "script_hash": sha,
            "script_path": script_path,
            "output_glb": output_glb,
            "guard_ok": True,
            "violations": [],
            "decision": "run",
        })

        exit_code, stdout, stderr = run_blender(args.blender_path, script_path, output_glb)

        glb_exists = os.path.isfile(output_glb)

        if exit_code == 0 and glb_exists:
            metadata = parse_metadata(stdout)
            result = {
                "script_hash": sha,
                "script_path": script_path,
                "model": args.model,
                "prompt": args.prompt,
                "output_glb": output_glb,
                "blender_exit_code": exit_code,
                "retries": attempt,
                "metadata": metadata,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        # Build error summary for feedback
        error_parts = []
        if exit_code != 0:
            error_parts.append(f"Blender exited with code {exit_code}.")
        if not glb_exists:
            error_parts.append(f"Output file was not created: {output_glb}")
        if stderr.strip():
            error_parts.append(stderr.strip())

        last_stderr = "\n".join(error_parts)

        if attempt < args.max_retries:
            print(
                f"Attempt {attempt + 1}/{args.max_retries + 1} failed. Retrying...\n{last_stderr}",
                file=sys.stderr,
            )
        else:
            print(
                f"All {args.max_retries + 1} attempts failed.\nLast error:\n{last_stderr}",
                file=sys.stderr,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
