"""
derive.py — Derive a new generation prompt from an existing ledger record.

Takes a parent record as the starting point, applies a change instruction via
Gemini, and emits a modified prompt. The new generation is linked back to the
parent (lineage) so `ledger tree` can show the derivation history.

Usage:
    # print the derived prompt to stdout
    python derive.py \\
        --parent-id <RECORD_ID> \\
        --change "脚を金属製にして座面を赤に" \\
        [--db-path ~/.vrm-pipeline/ledger.db] \\
        [--model gemini-2.5-flash] \\
        [--dry-run]

    # write a drop-zone pair (<name>.prompt + <name>.params.json with parent_id)
    python derive.py \\
        --parent-id <RECORD_ID> \\
        --change "脚を金属製に" \\
        --emit-drop ./drop --name chair_v2

stdout: derived prompt text (one line)
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SYSTEM_INSTRUCTION = """\
You are a prompt editor for 3D asset generation.
You will be given:
- A BASE prompt that produced an existing 3D asset
- A CHANGE instruction describing how to modify it

Your task: Rewrite the BASE prompt into a new prompt that keeps everything the
base described EXCEPT where the CHANGE instruction overrides it. Apply the change
faithfully and keep the rest intact.
Rules:
- Preserve the parts of the base prompt the change does not touch
- Apply the requested change concretely (materials, colors, proportions, parts)
- Return ONLY the new prompt as plain text, no explanation, no quotes\
"""

USER_PROMPT_TEMPLATE = """\
BASE prompt:
{base}

CHANGE instruction:
{change}

Write the new prompt:\
"""

DEFAULT_DB_PATH = Path.home() / ".vrm-pipeline" / "ledger.db"


def get_parent_record(db_path: Path, parent_id: str) -> dict:
    """Fetch the parent record via the `ledger get` CLI."""
    cmd = ["ledger", "--db", str(db_path), "get", "--id", parent_id]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ledger get failed for id '{parent_id}':\n{result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse ledger get output: {exc}")


def derive_prompt(base: str, change: str, model_name: str) -> str:
    """Call Gemini to produce a modified prompt from base + change."""
    import google.generativeai as genai

    user_message = USER_PROMPT_TEMPLATE.format(base=base, change=change)
    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    response = model.generate_content(user_message)
    return response.text.strip()


def emit_drop(out_dir: Path, name: str, prompt: str, parent_id: str) -> None:
    """Write <name>.prompt and <name>.params.json into the drop directory."""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(
            f"invalid --name '{name}': must be a plain file stem (no path separators)"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / f"{name}.prompt"
    params_path = out_dir / f"{name}.params.json"
    prompt_path.write_text(prompt + "\n", encoding="utf-8")
    params_path.write_text(
        json.dumps({"parent_id": parent_id}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {prompt_path} and {params_path}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Derive a new generation prompt from an existing ledger record."
    )
    parser.add_argument("--parent-id", required=True, help="Record ID to derive from.")
    parser.add_argument("--change", required=True, help="Change instruction to apply.")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to ledger SQLite DB (default: {DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--model",
        default="gemini-2.5-flash",
        help="Gemini model name (default: gemini-2.5-flash).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Gemini call; print base prompt + change to stderr and base prompt to stdout.",
    )
    parser.add_argument(
        "--emit-drop",
        type=Path,
        help="Directory to write a drop-zone <name>.prompt + <name>.params.json pair.",
    )
    parser.add_argument(
        "--name",
        help="Stem for the emitted drop files (required with --emit-drop).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.emit_drop and not args.name:
        print("Error: --name is required when --emit-drop is given.", file=sys.stderr)
        sys.exit(1)

    db_path: Path = args.db_path.expanduser().resolve()
    if not db_path.exists():
        print(f"Error: ledger DB not found at '{db_path}'.", file=sys.stderr)
        sys.exit(1)

    record = get_parent_record(db_path, args.parent_id)
    base_prompt = (record.get("prompt") or "").strip()
    if not base_prompt:
        print(
            f"Error: parent record '{args.parent_id}' has an empty prompt; nothing to derive from.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.dry_run:
        print(
            f"BASE: {base_prompt}\nCHANGE: {args.change}",
            file=sys.stderr,
        )
        derived = base_prompt
    else:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print(
                "Error: GEMINI_API_KEY environment variable is not set.\n"
                "Please export your Gemini API key before running this tool:\n"
                "  export GEMINI_API_KEY=your_api_key_here",
                file=sys.stderr,
            )
            sys.exit(1)
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        derived = derive_prompt(base_prompt, args.change, args.model)

    if args.emit_drop:
        emit_drop(args.emit_drop, args.name, derived, args.parent_id)

    print(derived)


if __name__ == "__main__":
    main()
