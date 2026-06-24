"""
tag.py - Extract structured visual tags from VRM character renders using Claude vision API.

Usage:
    python tag.py --render-dir <dir> [--output <path>] [--model <model>] [--faces <faces>]
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

PROMPT = """You are analyzing a VRM 3D anime character render. Extract visual attributes as structured JSON.

Return ONLY valid JSON with these fields:
- hair_color: string (e.g. "silver", "black", "blonde")
- hair_style: string (e.g. "twin-tail", "ponytail", "short", "long-straight")
- hair_length: string ("short"|"medium"|"long")
- eye_color: string
- eye_style: string (e.g. "large", "sharp", "round")
- outfit_style: string (e.g. "school_uniform", "casual", "fantasy", "gothic_lolita")
- outfit_color: string (dominant color)
- body_type: string ("slim"|"average"|"athletic")
- skin_tone: string ("fair"|"medium"|"dark")
- accessories: array of strings
- expressions: string (dominant expression from render)
- overall_style: string (brief descriptor)

Return only the JSON object, no markdown."""


def encode_image(img_path: Path) -> str:
    return base64.standard_b64encode(img_path.read_bytes()).decode("utf-8")


def build_content(image_paths: list[Path]) -> list[dict]:
    content = []
    for img_path in image_paths:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/webp",
                "data": encode_image(img_path),
            },
        })
    content.append({
        "type": "text",
        "text": PROMPT,
    })
    return content


def extract_tags(render_dir: Path, model: str, faces: list[str]) -> dict:
    import anthropic

    client = anthropic.Anthropic()

    # Collect existing image files
    image_paths = []
    for face in faces:
        img_path = render_dir / f"{face}.webp"
        if img_path.exists():
            image_paths.append(img_path)
        else:
            print(f"WARNING: {img_path} not found, skipping.", file=sys.stderr)

    if not image_paths:
        print("ERROR: No image files found in render directory.", file=sys.stderr)
        sys.exit(1)

    message = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": build_content(image_paths),
        }],
    )

    raw_response = message.content[0].text

    # Attempt to parse JSON from response
    try:
        tags = json.loads(raw_response)
    except json.JSONDecodeError:
        print("WARNING: Claude response was not valid JSON. Storing raw response only.", file=sys.stderr)
        tags = {}

    return {
        "model": model,
        "tags": tags,
        "raw_response": raw_response,
    }


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Extract structured visual tags from VRM character renders using Claude vision API."
    )
    parser.add_argument(
        "--render-dir",
        required=True,
        type=Path,
        help="Directory containing render outputs (face_front.webp, body_front.webp, etc.)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON path (default: <render-dir>/tags.json)",
    )
    parser.add_argument(
        "--model",
        default="claude-haiku-4-5-20251001",
        help="Claude model to use (default: claude-haiku-4-5-20251001)",
    )
    parser.add_argument(
        "--faces",
        default="face_front,body_front",
        help="Comma-separated list of face names to use (default: face_front,body_front)",
    )

    args = parser.parse_args()

    render_dir: Path = args.render_dir.resolve()
    if not render_dir.is_dir():
        print(f"ERROR: render-dir '{render_dir}' does not exist or is not a directory.", file=sys.stderr)
        sys.exit(1)

    output: Path = args.output if args.output is not None else render_dir / "tags.json"
    faces: list[str] = [f.strip() for f in args.faces.split(",") if f.strip()]

    result = extract_tags(render_dir, args.model, faces)

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Tags written to {output}")


if __name__ == "__main__":
    main()
