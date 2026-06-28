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
    timeout = int(os.environ.get("VRM_SUBPROCESS_TIMEOUT", "600"))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ledger get for id '{parent_id}' timed out after {timeout}s"
        )
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


def resolve_image_from_record(record: dict) -> Path:
    """
    Resolve a render image path from a parent ledger record.

    Looks first in the r0_ref render directory for *.webp or *.png files,
    then falls back to asset_ref JSON fields. Raises SystemExit with a
    user-friendly message when no image can be resolved.
    """
    # 1. Try r0_ref: the render output directory
    r0_ref = (record.get("r0_ref") or "").strip()
    if r0_ref:
        r0_dir = Path(r0_ref)
        if r0_dir.is_dir():
            for ext in ("*.webp", "*.png"):
                candidates = sorted(r0_dir.glob(ext))
                if candidates:
                    return candidates[0].resolve()

    # 2. Try asset_ref: a JSON string that may contain image paths
    asset_ref_str = (record.get("asset_ref") or "").strip()
    if asset_ref_str and asset_ref_str != "{}":
        try:
            asset_data = json.loads(asset_ref_str)
            if isinstance(asset_data, dict):
                for key in ("image", "render", "thumbnail", "preview"):
                    val = asset_data.get(key)
                    if val:
                        p = Path(val)
                        if p.is_file():
                            return p.resolve()
        except json.JSONDecodeError:
            pass

    print(
        "Error: 親レコードから画像を解決できません。--image を指定してください。\n"
        f"  r0_ref={r0_ref!r}  asset_ref={asset_ref_str!r}",
        file=sys.stderr,
    )
    sys.exit(1)


def resolve_base_vrm_from_record(record: dict) -> Path:
    """
    Resolve the base VRM path from a parent ledger record.

    Looks first in ``generation_params`` (a JSON string) for a ``vrm_path``
    field, then falls back to ``asset_ref`` (a JSON string) for a VRM-like
    reference. Raises SystemExit with a user-friendly message when no VRM
    path can be resolved.
    """
    # 1. Try generation_params: a JSON string with a vrm_path field
    gen_params_str = (record.get("generation_params") or "").strip()
    if gen_params_str and gen_params_str != "{}":
        try:
            gen_data = json.loads(gen_params_str)
            if isinstance(gen_data, dict):
                vrm_path = gen_data.get("vrm_path")
                if vrm_path:
                    return Path(vrm_path).resolve()
        except json.JSONDecodeError:
            pass

    # 2. Try asset_ref: a JSON string that may contain a VRM reference
    asset_ref_str = (record.get("asset_ref") or "").strip()
    if asset_ref_str and asset_ref_str != "{}":
        try:
            asset_data = json.loads(asset_ref_str)
            if isinstance(asset_data, dict):
                for key in ("vrm_path", "vrm", "vrm_ref", "asset"):
                    val = asset_data.get(key)
                    if val:
                        return Path(val).resolve()
        except json.JSONDecodeError:
            pass

    print(
        "Error: 親レコードからベースVRMパスを解決できません。\n"
        "  generation_params.vrm_path もしくは asset_ref のVRM参照が見つかりません。\n"
        f"  generation_params={gen_params_str!r}  asset_ref={asset_ref_str!r}",
        file=sys.stderr,
    )
    sys.exit(1)


def emit_drop(
    out_dir: Path,
    name: str,
    prompt: str,
    parent_id: str,
    image_ref: Path | None = None,
    vroid_edit: bool = False,
    base_vrm: Path | None = None,
    change: str | None = None,
) -> None:
    """Write <name>.prompt and <name>.params.json into the drop directory.

    If *image_ref* is provided it is written as an absolute-path string under
    the ``image_ref`` key.  When omitted the key is not present (backwards
    compatible with callers that do not pass an image).

    When *vroid_edit* is True, the params additionally carry the VRoid-edit
    derivation fields ``vroid_edit`` (True), ``base_vrm`` (absolute path string
    of the parent's VRM) and ``change`` (the raw adjustment instruction). These
    keys are omitted entirely when *vroid_edit* is False (backwards compatible).
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(
            f"invalid --name '{name}': must be a plain file stem (no path separators)"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / f"{name}.prompt"
    params_path = out_dir / f"{name}.params.json"
    prompt_path.write_text(prompt + "\n", encoding="utf-8")
    params: dict = {"parent_id": parent_id}
    if vroid_edit:
        params["vroid_edit"] = True
        if base_vrm is not None:
            params["base_vrm"] = str(base_vrm.resolve())
        if change is not None:
            params["change"] = change
    if image_ref is not None:
        params["image_ref"] = str(image_ref.resolve())
    params_path.write_text(
        json.dumps(params, ensure_ascii=False) + "\n",
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
    parser.add_argument(
        "--vroid-edit",
        action="store_true",
        help=(
            "VRoid edit derivation mode. Resolves the parent record's base VRM "
            "and writes vroid_edit/base_vrm/change into the drop params instead "
            "of rewriting the prompt via Gemini."
        ),
    )
    parser.add_argument(
        "--image",
        type=Path,
        default=None,
        help=(
            "Path to a render image to include as image_ref in the drop params. "
            "When omitted with --emit-drop, the path is resolved from the parent "
            "record's r0_ref render directory automatically."
        ),
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

    if args.vroid_edit:
        # VRoid edit mode: do not rewrite the prompt via Gemini; the change is
        # carried verbatim as the adjustment instruction in the drop params.
        print(
            f"BASE: {base_prompt}\nVROID-EDIT CHANGE: {args.change}",
            file=sys.stderr,
        )
        derived = base_prompt
    elif args.dry_run:
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
        # Resolve image_ref for the drop params
        image_ref: Path | None = None
        if args.image is not None:
            # --image was explicitly supplied: validate it exists
            image_path = args.image.expanduser().resolve()
            if not image_path.is_file():
                print(
                    f"Error: --image file not found: '{image_path}'",
                    file=sys.stderr,
                )
                sys.exit(1)
            image_ref = image_path
        else:
            # Auto-resolve from the parent record. In vroid-edit mode an image
            # is optional (only included when --image is explicitly supplied),
            # so we skip auto-resolution there.
            if not args.vroid_edit:
                image_ref = resolve_image_from_record(record)

        if args.vroid_edit:
            base_vrm = resolve_base_vrm_from_record(record)
            emit_drop(
                args.emit_drop,
                args.name,
                derived,
                args.parent_id,
                image_ref=image_ref,
                vroid_edit=True,
                base_vrm=base_vrm,
                change=args.change,
            )
        else:
            emit_drop(args.emit_drop, args.name, derived, args.parent_id, image_ref)
    elif args.image is not None:
        # --image given but --emit-drop not given: validate existence for consistency
        image_path = args.image.expanduser().resolve()
        if not image_path.is_file():
            print(
                f"Error: --image file not found: '{image_path}'",
                file=sys.stderr,
            )
            sys.exit(1)

    print(derived)


if __name__ == "__main__":
    main()
