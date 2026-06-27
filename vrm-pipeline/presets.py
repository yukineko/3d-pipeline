"""
presets.py — Versioned registry of vetted VRM character adjustment presets
            + override-not-replace merge logic.

Public API:
    PRESETS_VERSION         str — semver of this preset collection.
    DEFAULT_PRESET_NAME     str — name of the default preset (must be in registry).
    get_preset(name)        -> dict (deep copy of named preset)
    list_presets()          -> list[str] of registered preset names
    merge_adjustments(base, overrides) -> dict (pure, no mutation of inputs)

Schema each preset conforms to:
    {
        "expressions": {
            "happy": 0..1, "angry": 0..1, "sad": 0..1,
            "relaxed": 0..1, "surprised": 0..1, "blink": 0..1,
        },
        "materials": {
            "hair":   [r, g, b, a],   # all components 0..1
            "skin":   [r, g, b, a],
            "eye":    [r, g, b, a],
            "outfit": [r, g, b, a],
        },
        "height_scale": float,   # 0.5..2.0
    }

merge_adjustments guarantee (override-not-replace):
    - expressions: per-key — keys present in `overrides` replace; absent keys keep base value.
    - materials:   per-key — keys present in `overrides` replace; absent keys keep base value.
    - height_scale: if present in `overrides`, replace; else keep base value.
    - `overrides` may be sparse at any level.
    - Neither `base` nor `overrides` is mutated.
"""

import copy

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

PRESETS_VERSION: str = "1.0.0"

# ---------------------------------------------------------------------------
# Preset registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, dict] = {
    # ---- cheerful -----------------------------------------------------------
    # Upbeat, warm, friendly character. High happiness, golden-brown hair,
    # warm skin tones, amber eyes, coral outfit.
    "cheerful": {
        "expressions": {
            "happy":     0.80,
            "angry":     0.00,
            "sad":       0.00,
            "relaxed":   0.30,
            "surprised": 0.20,
            "blink":     0.10,
        },
        "materials": {
            "hair":   [0.55, 0.35, 0.10, 1.0],   # warm golden-brown
            "skin":   [0.95, 0.82, 0.72, 1.0],   # sun-kissed peach
            "eye":    [0.85, 0.60, 0.20, 1.0],   # amber
            "outfit": [0.90, 0.40, 0.35, 1.0],   # coral-red
        },
        "height_scale": 1.00,
    },

    # ---- cool ---------------------------------------------------------------
    # Composed, elegant character. Restrained expression, dark blue hair,
    # pale fair skin, steel-blue eyes, deep navy outfit. Slightly taller.
    "cool": {
        "expressions": {
            "happy":     0.10,
            "angry":     0.00,
            "sad":       0.00,
            "relaxed":   0.70,
            "surprised": 0.00,
            "blink":     0.15,
        },
        "materials": {
            "hair":   [0.10, 0.12, 0.30, 1.0],   # midnight blue-black
            "skin":   [0.95, 0.88, 0.85, 1.0],   # fair porcelain
            "eye":    [0.30, 0.50, 0.80, 1.0],   # steel blue
            "outfit": [0.10, 0.15, 0.35, 1.0],   # deep navy
        },
        "height_scale": 1.05,
    },

    # ---- cute ---------------------------------------------------------------
    # Youthful, soft, bubbly character. Mild happy+surprised blend,
    # pastel-pink hair, soft skin, violet eyes, lavender outfit. Slightly shorter.
    "cute": {
        "expressions": {
            "happy":     0.50,
            "angry":     0.00,
            "sad":       0.00,
            "relaxed":   0.20,
            "surprised": 0.35,
            "blink":     0.05,
        },
        "materials": {
            "hair":   [0.95, 0.60, 0.75, 1.0],   # pastel pink
            "skin":   [0.98, 0.85, 0.80, 1.0],   # soft rose-peach
            "eye":    [0.60, 0.30, 0.85, 1.0],   # purple-violet
            "outfit": [0.80, 0.70, 0.95, 1.0],   # pastel lavender
        },
        "height_scale": 0.95,
    },
}

# ---------------------------------------------------------------------------
# Default preset
# ---------------------------------------------------------------------------

DEFAULT_PRESET_NAME: str = "cheerful"

# Guard: DEFAULT_PRESET_NAME must be in the registry at import time.
assert DEFAULT_PRESET_NAME in _REGISTRY, (
    f"DEFAULT_PRESET_NAME {DEFAULT_PRESET_NAME!r} is not in the preset registry."
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_preset(name: str) -> dict:
    """Return a deep copy of the named preset dict.

    Mutating the returned dict will NOT affect the registry.

    Raises:
        KeyError: If *name* is not in the registry.
    """
    if name not in _REGISTRY:
        raise KeyError(
            f"Unknown preset {name!r}. Available presets: {list(_REGISTRY.keys())}"
        )
    return copy.deepcopy(_REGISTRY[name])


def list_presets() -> list[str]:
    """Return a list of all registered preset names."""
    return list(_REGISTRY.keys())


def merge_adjustments(base: dict, overrides: dict) -> dict:
    """Layer *overrides* onto *base* without mutating either input.

    Merge semantics (override-not-replace):

    expressions
        Per-key: each key present in ``overrides["expressions"]`` replaces the
        corresponding key in *base*. Keys absent from overrides keep the base value.

    materials
        Per-key: each key present in ``overrides["materials"]`` replaces the
        corresponding key in *base*. Keys absent from overrides keep the base value.

    height_scale
        If ``"height_scale"`` is present in *overrides*, it replaces the base value;
        otherwise the base value is kept.

    *overrides* may be sparse at any level — e.g. ``{"expressions": {"happy": 0.9}}``
    is valid and changes only ``expressions.happy`` while keeping everything else.

    Returns:
        A complete, freshly-allocated adjustments dict. Neither *base* nor
        *overrides* is mutated.
    """
    result = copy.deepcopy(base)

    # --- expressions (per-key merge) ---
    if "expressions" in overrides and isinstance(overrides["expressions"], dict):
        result_expr = result.setdefault("expressions", {})
        for key, val in overrides["expressions"].items():
            result_expr[key] = val

    # --- materials (per-key merge) ---
    if "materials" in overrides and isinstance(overrides["materials"], dict):
        result_mat = result.setdefault("materials", {})
        for key, val in overrides["materials"].items():
            result_mat[key] = copy.deepcopy(val)

    # --- height_scale ---
    if "height_scale" in overrides:
        result["height_scale"] = overrides["height_scale"]

    return result
