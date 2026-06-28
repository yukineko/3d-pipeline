"""
render/body_params.py — Infer MakeHuman/MPFB2 body-morph values from a text
                        prompt (and optionally an image) using Gemini.

This is the body/morph analogue of the top-level ``vroid_params.py`` module
(which handles VRM expression / material params). Where vroid_params drives a
VRM avatar's face and materials, this module drives the *base body shape* via
MakeHuman macro modifiers (gender / age / height / weight / muscle / ...).

It is PURE PYTHON: it MUST NOT import bpy and must be importable in a bare
environment (no Blender, no google-generativeai installed at import time).

Public API:
    infer_body_overrides(prompt, image_path=None, model="gemini-2.5-flash") -> dict
        Extract ONLY the body-morph keys Gemini explicitly emitted (sparse
        dict), each clamped to 0.0..1.0. Unknown keys dropped. Unparseable /
        invalid JSON yields an empty dict ``{}`` (never raises on bad JSON).

    resolve_body_morphs(prompt, preset_name=None, image_path=None,
                        model="gemini-2.5-flash") -> dict
        MAIN entry: layer Gemini's sparse overrides onto a vetted preset
        baseline. Returns a COMPLETE dict with all 8 BODY_MORPH_KEYS. Preset
        values survive wherever Gemini omits a key, so even a garbage/empty
        prompt yields a reasonable adult body (the "neutral_adult" baseline).

Schemas:

    BODY_MORPH_KEYS — the 8 canonical morph keys. Every value is a float
    normalized to 0.0..1.0 where 0.5 == the mid / neutral point:

        gender       0.0 = female,  0.5 = androgynous, 1.0 = male
        age          0.0 = young,   0.5 = adult,        1.0 = old
        height       0.0 = short,                       1.0 = tall
        weight       0.0 = thin,    0.5 = average,       1.0 = heavy
        muscle       0.0 = soft,                         1.0 = muscular
        proportions  0.0..1.0  body proportion balance (0.5 = default balance)
        bodyfat      0.0 = lean,                         1.0 = high body fat
        head_size    0.0 = small,   0.5 = default,       1.0 = large

    These are CANONICAL names that map onto MakeHuman macro modifiers. The
    actual MPFB2 target strings (e.g. "macrodetails/Gender",
    "macrodetails-height/Height", ...) are resolved later in
    ``generate_body.py``'s MODIFIER_MAP — NOT here. This module deals only in
    the normalized 0..1 canonical space so it stays bpy-free and testable.

    infer_body_overrides returns (sparse, only keys Gemini emitted):
    {
        # any subset of BODY_MORPH_KEYS, each a float in 0.0..1.0
        "gender": 0.0, "age": 0.5, ...
    }

    resolve_body_morphs returns (complete via preset + overrides merge):
    {
        # ALL 8 BODY_MORPH_KEYS present, preset values overridden by Gemini
        "gender": ..., "age": ..., "height": ..., "weight": ...,
        "muscle": ..., "proportions": ..., "bodyfat": ..., "head_size": ...,
    }

Environment:
    GEMINI_API_KEY — must be set; ``_call_gemini`` raises RuntimeError if missing.

Internal seam for testing:
    _call_gemini(prompt, image_path, model) -> str
        Returns the raw text response from Gemini. Patch this in tests
        (``unittest.mock.patch("render.body_params._call_gemini", ...)``) to
        avoid real API calls.
"""

import json
import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

# CANONICAL body-morph keys → mapped to MakeHuman macro modifiers downstream.
# The concrete MPFB2 target strings live in generate_body.py's MODIFIER_MAP,
# NOT here; this module only produces normalized 0..1 values per key.
BODY_MORPH_KEYS = (
    "gender",
    "age",
    "height",
    "weight",
    "muscle",
    "proportions",
    "bodyfat",
    "head_size",
)

SYSTEM_INSTRUCTION = """\
You are a 3D character body-shape assistant for MakeHuman-style base meshes.
Given a text description (and optionally an image) of a character's body,
output a JSON object describing the body's macro proportions — no extra keys,
no explanation.

Every value is a float in the range 0.0 to 1.0 where 0.5 is the neutral / mid
point:

{
  "gender": <0.0=female .. 0.5=androgynous .. 1.0=male>,
  "age": <0.0=young .. 0.5=adult .. 1.0=old>,
  "height": <0.0=short .. 1.0=tall>,
  "weight": <0.0=thin .. 0.5=average .. 1.0=heavy>,
  "muscle": <0.0=soft .. 1.0=muscular>,
  "proportions": <0.0..1.0 body proportion balance>,
  "bodyfat": <0.0=lean .. 1.0=high>,
  "head_size": <0.0=small .. 0.5=default .. 1.0=large>
}

Only include keys you are confident about; omit the rest. All values must be
floats in 0.0..1.0. Return ONLY valid JSON. Do not include markdown fences or
any explanation.\
"""

USER_PROMPT_TEMPLATE = """\
Character body description:
{prompt}

Output the body-morph JSON:\
"""

# ---------------------------------------------------------------------------
# Good-default presets
# ---------------------------------------------------------------------------
#
# Premise: the user may have LOW aesthetic taste, so an empty or garbage prompt
# must still yield a reasonable adult body. "neutral_adult" (all keys = 0.5) is
# the default baseline used by resolve_body_morphs when no preset is named.

BODY_PRESETS: dict = {
    # The safe default: a perfectly neutral, average adult.
    "neutral_adult": {key: 0.5 for key in BODY_MORPH_KEYS},

    # A slim younger build.
    "slim_youth": {
        "gender": 0.5,
        "age": 0.25,
        "height": 0.45,
        "weight": 0.3,
        "muscle": 0.35,
        "proportions": 0.5,
        "bodyfat": 0.3,
        "head_size": 0.55,
    },

    # A tall, athletic, muscular build.
    "tall_athletic": {
        "gender": 0.6,
        "age": 0.5,
        "height": 0.8,
        "weight": 0.6,
        "muscle": 0.8,
        "proportions": 0.5,
        "bodyfat": 0.3,
        "head_size": 0.45,
    },
}

# The preset used when the caller passes preset_name=None.
DEFAULT_PRESET_NAME = "neutral_adult"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> str:
    """Strip optional ```json ... ``` fences and return the inner JSON string."""
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()
    return text.strip()


def _clamp01(v) -> float:
    """Coerce *v* to float and clamp it to 0.0..1.0.

    Raises:
        ValueError / TypeError: if *v* is not numeric. Callers that treat
        non-numeric input as "missing" should guard with this (see
        infer_body_overrides, which skips keys that fail to clamp).
    """
    f = float(v)  # raises TypeError/ValueError for non-numeric -> caller skips
    return max(0.0, min(1.0, f))


# ---------------------------------------------------------------------------
# Gemini call (seam for testing)
# ---------------------------------------------------------------------------

def _call_gemini(prompt: str, image_path, model: str) -> str:
    """
    Call Gemini with the given prompt (and optional image) and return the raw
    response text.

    This is the single seam unit tests mock out via
    ``unittest.mock.patch("render.body_params._call_gemini", ...)`` so no real
    API call (or network) happens in tests.

    Raises:
        RuntimeError: If GEMINI_API_KEY is not set, or the API call fails.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is not set.\n"
            "Please export your Gemini API key before using this module:\n"
            "  export GEMINI_API_KEY=your_api_key_here"
        )

    import google.generativeai as genai

    genai.configure(api_key=api_key)

    gemini_model = genai.GenerativeModel(
        model_name=model,
        system_instruction=SYSTEM_INSTRUCTION,
    )

    user_message = USER_PROMPT_TEMPLATE.format(prompt=prompt)

    if image_path is not None:
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

def infer_body_overrides(
    prompt: str,
    image_path=None,
    model: str = "gemini-2.5-flash",
) -> dict:
    """
    Extract ONLY the body-morph keys Gemini explicitly emitted (sparse dict).

    Each emitted value is clamped to 0.0..1.0. Keys outside BODY_MORPH_KEYS are
    dropped. Values that cannot be coerced to a float are skipped (treated as
    missing). If the response cannot be parsed as JSON (or is not a JSON
    object), an empty dict ``{}`` is returned — this function never raises on
    bad model output, so resolve_body_morphs can always fall back to the preset.

    Args:
        prompt:     Natural-language description of the character's body.
        image_path: Optional path to a reference image.
        model:      Gemini model name (default: "gemini-2.5-flash").

    Returns:
        A sparse dict containing only recognized keys Gemini returned.

    Raises:
        RuntimeError: If GEMINI_API_KEY is not set or the API call fails.
    """
    raw_text = _call_gemini(prompt, image_path, model)

    json_text = _extract_json(raw_text)
    try:
        raw_dict = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return {}

    if not isinstance(raw_dict, dict):
        return {}

    result = {}
    for key in BODY_MORPH_KEYS:
        if key not in raw_dict:
            continue
        try:
            result[key] = _clamp01(raw_dict[key])
        except (TypeError, ValueError):
            # Non-numeric value for a known key → treat as missing.
            continue

    return result


def resolve_body_morphs(
    prompt: str,
    preset_name=None,
    image_path=None,
    model: str = "gemini-2.5-flash",
) -> dict:
    """
    Resolve body morphs by layering Gemini's sparse overrides onto a preset.

    Starts from ``BODY_PRESETS[preset_name or DEFAULT_PRESET_NAME]`` (copied),
    then overlays the sparse result of ``infer_body_overrides(...)``. The
    returned dict is COMPLETE: it always contains all 8 BODY_MORPH_KEYS. Preset
    values remain wherever Gemini omitted a key, so a vague or empty prompt
    still yields a sensible body (the neutral_adult baseline by default).

    Args:
        prompt:      Natural-language description of the character's body.
        preset_name: Baseline preset name. If None, DEFAULT_PRESET_NAME is used.
        image_path:  Optional path to a reference image.
        model:       Gemini model name (default: "gemini-2.5-flash").

    Returns:
        A complete dict with all 8 BODY_MORPH_KEYS.

    Raises:
        RuntimeError: If GEMINI_API_KEY is not set or the API call fails.
        KeyError:     If preset_name is given but not present in BODY_PRESETS.
    """
    base = dict(BODY_PRESETS[preset_name or DEFAULT_PRESET_NAME])  # copy

    overrides = infer_body_overrides(prompt, image_path=image_path, model=model)
    base.update(overrides)

    return base
