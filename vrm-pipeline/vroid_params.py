"""
vroid_params.py — Infer VRM character adjustment values from a text prompt
                  (and optionally an image) using Gemini.

Public API:
    infer_vrm_adjustments(prompt, image_path=None, model="gemini-2.5-flash") -> dict
        Deprecated; kept for backward compatibility. Returns a dict with ALL keys
        present (expressions filled with 0.0 for missing, height_scale defaulted to 1.0).

    infer_vrm_overrides(prompt, image_path=None, model="gemini-2.5-flash") -> dict
        Extract ONLY the keys Gemini explicitly emitted (sparse dict). Missing keys
        are omitted entirely, not defaulted.

    resolve_vrm_adjustments(prompt, preset_name=None, image_path=None, model="gemini-2.5-flash") -> dict
        New main entry: layer Gemini overrides onto a vetted preset baseline.
        Returns a complete adjusted dict guaranteed to match the preset's style
        when Gemini omits sections.

Schemas:

    infer_vrm_adjustments returns (always complete):
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

    infer_vrm_overrides returns (sparse, only keys Gemini emitted):
    {
        # Any subset of:
        "expressions": { subset of 6 keys },
        "materials": { subset of 4 keys },
        "height_scale": float,
    }

    resolve_vrm_adjustments returns (complete via preset + overrides merge):
    {
        "expressions": {6 keys from preset, overridden by Gemini},
        "materials": {4 keys from preset, overridden by Gemini},
        "height_scale": float,
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


def infer_vrm_overrides(
    prompt: str,
    image_path: str | None = None,
    model: str = "gemini-2.5-flash",
) -> dict:
    """
    Extract ONLY the keys Gemini explicitly emitted from a prompt/image.

    This is the sparse variant: missing keys are omitted entirely, NOT defaulted.
    The returned dict may contain any subset of the full schema:
    {
        "expressions": {subset of 6 keys},  # omitted if empty
        "materials": {subset of 4 keys},     # omitted if empty
        "height_scale": float,               # omitted if not present
    }

    Args:
        prompt:     Natural language description of the character or mood.
        image_path: Optional path to a reference image (PNG, JPEG, WEBP, etc.).
        model:      Gemini model name (default: "gemini-2.5-flash").

    Returns:
        A sparse dict containing only keys Gemini returned.

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

    # Build a sparse dict with only keys Gemini actually emitted
    result = {}

    # --- expressions (sparse) ---
    raw_expr = raw_dict.get("expressions", {})
    if isinstance(raw_expr, dict) and raw_expr:
        expressions = {}
        for key in EXPRESSION_KEYS:
            if key in raw_expr:
                expressions[key] = _clamp(raw_expr[key], 0.0, 1.0)
        if expressions:
            result["expressions"] = expressions

    # --- materials (sparse) ---
    raw_mat = raw_dict.get("materials", {})
    if isinstance(raw_mat, dict) and raw_mat:
        materials = {}
        for key in MATERIAL_KEYS:
            if key in raw_mat:
                materials[key] = _normalize_color(raw_mat[key])
        if materials:
            result["materials"] = materials

    # --- height_scale (sparse) ---
    if "height_scale" in raw_dict:
        result["height_scale"] = _clamp(raw_dict["height_scale"], 0.5, 2.0)

    return result


def resolve_vrm_adjustments(
    prompt: str,
    preset_name: str | None = None,
    image_path: str | None = None,
    model: str = "gemini-2.5-flash",
) -> dict:
    """
    Resolve VRM adjustments by layering Gemini overrides onto a preset baseline.

    The preset provides a vetted, intentional style (all 6 expressions, all 4 materials,
    height_scale). Gemini's response is treated as a sparse set of overrides that are
    layered ON TOP via preset-aware merge semantics.

    This ensures that a vague or empty prompt still produces a styled character:
    the preset's non-zero expression weights and material colors survive when
    Gemini omits those sections.

    Args:
        prompt:        Natural language description of the character or mood.
        preset_name:   Name of the preset to use as baseline. If None, uses
                       presets.DEFAULT_PRESET_NAME.
        image_path:    Optional path to a reference image (PNG, JPEG, WEBP, etc.).
        model:         Gemini model name (default: "gemini-2.5-flash").

    Returns:
        A complete adjustments dict guaranteed to have all 6 expressions,
        all 4 materials, and height_scale (all from preset with Gemini's
        explicitly-emitted keys overridden).

    Raises:
        RuntimeError: If GEMINI_API_KEY is not set or the API call fails.
        ValueError:   If the Gemini response cannot be parsed as JSON.
        KeyError:     If preset_name is not in the registry.
    """
    import presets  # lazy import (works for both direct runs and tests)

    # Load the preset baseline
    base = presets.get_preset(preset_name or presets.DEFAULT_PRESET_NAME)

    # Extract only what Gemini explicitly emitted
    overrides = infer_vrm_overrides(prompt, image_path=image_path, model=model)

    # Layer overrides onto preset (override-not-replace semantics)
    return presets.merge_adjustments(base, overrides)
