"""
vroid_params.py — Infer VRM character adjustment values from a text prompt
                  (and optionally an image) using Gemini.

Public API:
    infer_vrm_adjustments(prompt, image_path=None, model="gemini-2.5-flash") -> dict

The returned dict always conforms to the following schema (values clamped/defaulted):

    {
        "expressions": {
            "happy":     0..1,
            "angry":     0..1,
            "sad":       0..1,
            "relaxed":   0..1,
            "surprised": 0..1,
            "blink":     0..1,
        },
        "materials": {
            # keys are optional; each value is [r, g, b, a] with components 0..1
            "hair":   [r, g, b, a],
            "skin":   [r, g, b, a],
            "eye":    [r, g, b, a],
            "outfit": [r, g, b, a],
        },
        "height_scale": float,  # clamped to 0.5..2.0, default 1.0
    }

Environment:
    GEMINI_API_KEY — must be set; raises RuntimeError if missing.

Internal seam for testing:
    _call_gemini(prompt, image_path, model) -> str
        Returns the raw text response from Gemini. Patch this in tests to avoid
        real API calls.
"""

import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

EXPRESSION_KEYS = ("happy", "angry", "sad", "relaxed", "surprised", "blink")
MATERIAL_KEYS = ("hair", "skin", "eye", "outfit")

SYSTEM_INSTRUCTION = """\
You are a VRM character parameter assistant.
Given a text description (and optionally an image) of a character or scene mood,
output a JSON object with the following exact structure — no extra keys, no explanation:

{
  "expressions": {
    "happy": <0.0-1.0>,
    "angry": <0.0-1.0>,
    "sad": <0.0-1.0>,
    "relaxed": <0.0-1.0>,
    "surprised": <0.0-1.0>,
    "blink": <0.0-1.0>
  },
  "materials": {
    "hair": [<r>, <g>, <b>, <a>],
    "skin": [<r>, <g>, <b>, <a>],
    "eye":  [<r>, <g>, <b>, <a>],
    "outfit": [<r>, <g>, <b>, <a>]
  },
  "height_scale": <0.5-2.0>
}

All color components (r, g, b, a) must be floats in the range 0.0 to 1.0.
height_scale must be a float in the range 0.5 to 2.0.
Expression weights must be floats in the range 0.0 to 1.0.
Return ONLY valid JSON. Do not include markdown fences or any explanation.\
"""

USER_PROMPT_TEMPLATE = """\
Character / mood description:
{prompt}

Output the VRM adjustment JSON:\
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> str:
    """Strip optional ```json ... ``` fences and return the inner JSON string."""
    # Try to find a fenced block first
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()
    return text.strip()


def _clamp(value, lo, hi):
    """Clamp *value* to [lo, hi]."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def _normalize_color(raw) -> list:
    """
    Normalize a color value to a list of 4 floats in [0, 1].
    Accepts lists/tuples of 3 or 4 elements; missing alpha defaults to 1.0.
    """
    if not isinstance(raw, (list, tuple)):
        return [0.0, 0.0, 0.0, 1.0]
    components = list(raw)
    # Pad alpha if only rgb provided
    while len(components) < 4:
        components.append(1.0)
    # Take first 4 and clamp each
    return [_clamp(c, 0.0, 1.0) for c in components[:4]]


def _normalize_result(raw: dict) -> dict:
    """
    Validate and normalize the raw parsed dict from Gemini.

    - expression weights: clamped to 0..1; missing keys default to 0.0
    - material colors: each component clamped to 0..1; missing material keys
      are omitted (caller may add defaults if needed)
    - height_scale: clamped to 0.5..2.0; missing defaults to 1.0
    """
    # --- expressions ---
    raw_expr = raw.get("expressions", {})
    if not isinstance(raw_expr, dict):
        raw_expr = {}
    expressions = {}
    for key in EXPRESSION_KEYS:
        expressions[key] = _clamp(raw_expr.get(key, 0.0), 0.0, 1.0)

    # --- materials ---
    raw_mat = raw.get("materials", {})
    if not isinstance(raw_mat, dict):
        raw_mat = {}
    materials = {}
    for key in MATERIAL_KEYS:
        if key in raw_mat:
            materials[key] = _normalize_color(raw_mat[key])

    # --- height_scale ---
    height_scale = _clamp(raw.get("height_scale", 1.0), 0.5, 2.0)

    return {
        "expressions": expressions,
        "materials": materials,
        "height_scale": height_scale,
    }


# ---------------------------------------------------------------------------
# Gemini call (seam for testing)
# ---------------------------------------------------------------------------

def _call_gemini(prompt: str, image_path: str | None, model: str) -> str:
    """
    Call Gemini with the given prompt (and optional image) and return the
    raw response text.

    This function is the single seam that unit tests mock out via
    ``unittest.mock.patch("vroid_params._call_gemini", ...)``.

    Raises:
        RuntimeError: If GEMINI_API_KEY is not set.
        RuntimeError: If the Gemini API call fails.
    """
    import google.generativeai as genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is not set.\n"
            "Please export your Gemini API key before using this module:\n"
            "  export GEMINI_API_KEY=your_api_key_here"
        )

    genai.configure(api_key=api_key)

    gemini_model = genai.GenerativeModel(
        model_name=model,
        system_instruction=SYSTEM_INSTRUCTION,
    )

    user_message = USER_PROMPT_TEMPLATE.format(prompt=prompt)

    if image_path is not None:
        # Include image in the request
        image_path_obj = Path(image_path)
        if not image_path_obj.is_file():
            raise RuntimeError(f"Image file not found: {image_path}")

        import PIL.Image
        pil_image = PIL.Image.open(str(image_path_obj))
        contents = [user_message, pil_image]
    else:
        contents = user_message

    response = gemini_model.generate_content(contents)
    return response.text.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def infer_vrm_adjustments(
    prompt: str,
    image_path: str | None = None,
    model: str = "gemini-2.5-flash",
) -> dict:
    """
    Infer VRM character adjustment values from a text prompt and optional image.

    Args:
        prompt:     Natural language description of the character or mood.
        image_path: Optional path to a reference image (PNG, JPEG, WEBP, etc.).
        model:      Gemini model name (default: "gemini-2.5-flash").

    Returns:
        A dict conforming to the VRM adjustment schema:
        {
            "expressions": {"happy": 0..1, "angry": 0..1, "sad": 0..1,
                            "relaxed": 0..1, "surprised": 0..1, "blink": 0..1},
            "materials": {
                # subset of: "hair", "skin", "eye", "outfit"
                # each value is [r, g, b, a] with components in 0..1
            },
            "height_scale": float  # 0.5..2.0
        }

    Raises:
        RuntimeError: If GEMINI_API_KEY is not set or the API call fails.
        ValueError:   If the Gemini response cannot be parsed as JSON.
    """
    raw_text = _call_gemini(prompt, image_path, model)

    json_text = _extract_json(raw_text)
    try:
        raw_dict = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Gemini response is not valid JSON.\n"
            f"Response text: {raw_text!r}\n"
            f"Parse error: {exc}"
        ) from exc

    return _normalize_result(raw_dict)
