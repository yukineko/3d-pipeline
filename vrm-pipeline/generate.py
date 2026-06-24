"""
generate.py — Gemini-driven Blender Python script generation with retry.

Usage:
    python generate.py \
        --prompt "中世風の木製椅子" \
        --output-dir ./watched/objects/ \
        [--max-retries 3] \
        [--blender-path blender] \
        [--model gemini-2.5-flash]
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile

import google.generativeai as genai

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


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a 3D GLB via Gemini-written Blender script."
    )
    parser.add_argument("--prompt", required=True, help="Description of the 3D object to generate.")
    parser.add_argument("--output-dir", required=True, help="Directory where the .glb file will be saved.")
    parser.add_argument("--max-retries", type=int, default=3, help="Maximum number of retry attempts (default: 3).")
    parser.add_argument("--blender-path", default="blender", help="Path to the blender executable (default: blender).")
    parser.add_argument("--model", default="gemini-2.5-flash", help="Gemini model name (default: gemini-2.5-flash).")
    return parser.parse_args()


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
    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    response = model.generate_content(user_prompt)
    return response.text.strip()


def generate_script_retry(client, model_name: str, user_prompt: str, stderr: str) -> str:
    retry_prompt = RETRY_PROMPT_TEMPLATE.format(stderr=stderr, prompt=user_prompt)
    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    response = model.generate_content(retry_prompt)
    return response.text.strip()


def main():
    args = parse_args()
    api_key = check_api_key()

    genai.configure(api_key=api_key)

    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

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
