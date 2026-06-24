"""
suggest.py — Enrich a user prompt using adopted records from the ledger DB.

Usage:
    python suggest.py \\
        --prompt "中世風の木製椅子" \\
        [--db-path ~/.vrm-pipeline/ledger.db] \\
        [--top-k 5] \\
        [--model gemini-2.5-flash] \\
        [--dry-run]

stdout: enriched prompt text (one line)
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import google.generativeai as genai

SYSTEM_INSTRUCTION = """\
You are a prompt enrichment assistant for 3D asset generation.
You will be given:
- A user's original prompt
- A list of previously ADOPTED (successful) prompts and their visual attribute tags

Your task: Rewrite the user's prompt to be more specific and likely to succeed,
by incorporating patterns from the successful examples.
Rules:
- Keep the core intent of the original prompt
- Add specific visual details (materials, style, proportions) inspired by successful patterns
- Return ONLY the enriched prompt as plain text, no explanation, no quotes\
"""

USER_PROMPT_TEMPLATE = """\
Original prompt: {prompt}

Successful examples:
{examples_json}

Write an enriched version of the original prompt:\
"""

DEFAULT_DB_PATH = Path.home() / ".vrm-pipeline" / "ledger.db"


def get_adopted_records(db_path: Path, top_k: int) -> list[tuple[str, str]]:
    """Fetch adopted records from the ledger DB."""
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT prompt, derived FROM records WHERE outcome LIKE '%\"adopted\": true%' ORDER BY timestamp DESC LIMIT ?",
        (top_k * 3,),
    ).fetchall()
    conn.close()
    return rows


def extract_tags(derived_json_str: str) -> dict:
    """Extract visual tags from the derived JSON string."""
    try:
        d = json.loads(derived_json_str or "{}")
        tag = d.get("tag", {})
        # tag.py output: {"tags": {"hair_color": ..., ...}, ...}
        if "tags" in tag:
            return tag["tags"]
        return tag
    except Exception:
        return {}


def build_examples(rows: list[tuple[str, str]], top_k: int) -> list[dict]:
    """Build a list of example dicts from DB rows, limited to top_k."""
    examples = []
    for prompt, derived in rows:
        tags = extract_tags(derived)
        if tags or prompt:
            examples.append({"prompt": prompt, "tags": tags})
        if len(examples) >= top_k:
            break
    return examples


def enrich_prompt(prompt: str, examples: list[dict], model_name: str) -> str:
    """Call Gemini to produce an enriched prompt."""
    user_message = USER_PROMPT_TEMPLATE.format(
        prompt=prompt,
        examples_json=json.dumps(examples, ensure_ascii=False, indent=2),
    )
    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    response = model.generate_content(user_message)
    return response.text.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich a user prompt using adopted records from the ledger DB."
    )
    parser.add_argument("--prompt", required=True, help="Original prompt to enrich.")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to ledger SQLite DB (default: {DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of adopted examples to use (default: 5).",
    )
    parser.add_argument(
        "--model",
        default="gemini-2.5-flash",
        help="Gemini model name (default: gemini-2.5-flash).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Gemini call; print adopted examples to stderr and original prompt to stdout.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # --dry-run does not need the API key
    if not args.dry_run:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print(
                "Error: GEMINI_API_KEY environment variable is not set.\n"
                "Please export your Gemini API key before running this tool:\n"
                "  export GEMINI_API_KEY=your_api_key_here",
                file=sys.stderr,
            )
            sys.exit(1)
        genai.configure(api_key=api_key)

    # Resolve DB path (expand ~)
    db_path: Path = args.db_path.expanduser().resolve()

    # Fetch adopted records — fall back to original prompt if DB is absent
    if not db_path.exists():
        print(
            f"WARNING: ledger DB not found at '{db_path}'. Returning original prompt as-is.",
            file=sys.stderr,
        )
        print(args.prompt)
        return

    rows = get_adopted_records(db_path, args.top_k)
    examples = build_examples(rows, args.top_k)

    if not examples:
        print(
            "WARNING: No adopted records found in the ledger DB. Returning original prompt as-is.",
            file=sys.stderr,
        )
        print(args.prompt)
        return

    # --dry-run: show examples and echo original prompt
    if args.dry_run:
        print(
            "Adopted examples (dry-run):\n"
            + json.dumps(examples, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        print(args.prompt)
        return

    enriched = enrich_prompt(args.prompt, examples, args.model)
    print(enriched)


if __name__ == "__main__":
    main()
