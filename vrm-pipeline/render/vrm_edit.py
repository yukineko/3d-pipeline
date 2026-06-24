"""
render/vrm_edit.py - Apply adjustments to a VRM file via Blender headless + VRM addon.

Python-side wrapper
-------------------
    from render.vrm_edit import edit_vrm
    out_path = edit_vrm("input.vrm", "output.vrm", {"expressions": {"happy": 1.0}})

Blender script mode
-------------------
When run directly by Blender (--python render/vrm_edit.py -- ...) the module
executes as a bpy script: it imports the VRM file, applies adjustments, exports
VRM, and exits with code 0 on success or 1 on failure.

Environment / arguments
------------------------
  BLENDER_PATH  Override Blender binary location (default: "blender").

  edit_vrm(in_vrm, out_vrm, adjustments, blender_path=None)
      in_vrm:       Path to the input .vrm file.
      out_vrm:      Destination path for the output .vrm file.
      adjustments:  dict with optional keys:
                      expressions: {happy/angry/sad/relaxed/surprised/blink: 0..1}
                      materials:   {hair/skin/eye/outfit: [r, g, b, a]}
                      height_scale: float (uniform scale applied to root/armature)
      blender_path: explicit binary; falls back to env BLENDER_PATH then "blender".
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Blender-script helpers (only called from within Blender, after bpy is available)
# ---------------------------------------------------------------------------

def _check_vrm_addon_or_raise() -> None:
    """
    Verify that the VRM addon import operator is registered.
    Raises RuntimeError with an actionable message if not found.
    """
    import bpy

    # Try each known import operator name in order
    _VRM_IMPORT_OPS = [
        ("import_scene", "vrm"),
    ]

    for namespace, name in _VRM_IMPORT_OPS:
        ns = getattr(bpy.ops, namespace, None)
        if ns is None:
            continue
        op = getattr(ns, name, None)
        if op is None:
            continue
        try:
            op.poll()
            return  # found a working operator
        except Exception:
            return  # registered but poll raised — still counts as present

    raise RuntimeError(
        "VRM addon not available: install VRM_Addon_for_Blender "
        "(https://vrm-addon-for-blender.info) and enable it in Blender preferences. "
        "Tried operators: import_scene.vrm."
    )


def _run_vrm_export(vrm_path: str):
    """
    Attempt VRM export via the first available operator.
    Returns the operator result set, or raises RuntimeError if no operator found.
    """
    import bpy

    _VRM_EXPORT_OPS = [
        ("export_scene", "vrm"),
        ("export_scene", "vrm1"),
        ("vrm", "export_scene"),
    ]

    last_exc: Exception | None = None
    for namespace, name in _VRM_EXPORT_OPS:
        ns = getattr(bpy.ops, namespace, None)
        if ns is None:
            continue
        op = getattr(ns, name, None)
        if op is None:
            continue
        try:
            return op(filepath=vrm_path)
        except Exception as exc:
            last_exc = exc
            continue

    if last_exc is not None:
        raise RuntimeError(
            f"VRM addon not available: install VRM_Addon_for_Blender "
            f"(https://vrm-addon-for-blender.info) and enable it in Blender "
            f"preferences. Last error: {last_exc}"
        )
    raise RuntimeError(
        "VRM addon not available: install VRM_Addon_for_Blender "
        "(https://vrm-addon-for-blender.info) and enable it in Blender preferences."
    )


def _apply_expressions(armature, adjustments: dict, spec_version: str) -> None:
    """
    Apply expression weights from adjustments['expressions'] to the VRM armature.
    Safe-skips any expression key that doesn't exist in the model.
    """
    expressions = adjustments.get("expressions", {})
    if not expressions:
        return

    ext = getattr(armature.data, "vrm_addon_extension", None)
    if ext is None:
        print("[vrm_edit] WARNING: armature has no vrm_addon_extension, skipping expressions",
              file=sys.stderr)
        return

    # VRM 1.x
    if spec_version.startswith("1"):
        vrm1 = getattr(ext, "vrm1", None)
        if vrm1 is None:
            print("[vrm_edit] WARNING: vrm1 not found in extension, skipping expressions",
                  file=sys.stderr)
            return
        exprs = getattr(vrm1, "expressions", None)
        if exprs is None:
            print("[vrm_edit] WARNING: vrm1.expressions not found, skipping expressions",
                  file=sys.stderr)
            return
        preset = getattr(exprs, "preset", None)
        if preset is None:
            print("[vrm_edit] WARNING: vrm1.expressions.preset not found, skipping expressions",
                  file=sys.stderr)
            return

        for key, value in expressions.items():
            expr_obj = getattr(preset, key, None)
            if expr_obj is None:
                print(f"[vrm_edit] INFO: expression '{key}' not found in VRM1 presets, skipping",
                      file=sys.stderr)
                continue
            try:
                if hasattr(expr_obj, "preview"):
                    expr_obj.preview = float(value)
                elif hasattr(expr_obj, "morph_target_binds"):
                    # Set preview through weight property if available
                    if hasattr(expr_obj, "weight"):
                        expr_obj.weight = float(value)
                print(f"[vrm_edit] INFO: set VRM1 expression '{key}' = {value}", file=sys.stderr)
            except Exception as exc:
                print(f"[vrm_edit] WARNING: failed to set expression '{key}': {exc}",
                      file=sys.stderr)

    else:
        # VRM 0.x - uses blend_shape_master / blend_shape_groups
        vrm0 = getattr(ext, "vrm0", None)
        if vrm0 is None:
            # Some versions expose it directly
            vrm0 = ext

        blend_shape_master = getattr(vrm0, "blend_shape_master", None)
        if blend_shape_master is None:
            print("[vrm_edit] WARNING: blend_shape_master not found, skipping expressions",
                  file=sys.stderr)
            return

        blend_shape_groups = getattr(blend_shape_master, "blend_shape_groups", None)
        if blend_shape_groups is None:
            print("[vrm_edit] WARNING: blend_shape_groups not found, skipping expressions",
                  file=sys.stderr)
            return

        # Map common expression names to VRM0 preset names
        vrm0_preset_map = {
            "happy": ["joy", "happy"],
            "angry": ["angry", "anger"],
            "sad": ["sorrow", "sad"],
            "relaxed": ["relaxed", "fun"],
            "surprised": ["surprised", "surprise"],
            "blink": ["blink", "blink_l"],
        }

        for key, value in expressions.items():
            lookup_names = vrm0_preset_map.get(key, [key])
            found = False
            for group in blend_shape_groups:
                group_name = getattr(group, "name", "").lower()
                group_preset = getattr(group, "preset_name", "").lower()
                if any(n in group_name or n == group_preset for n in lookup_names):
                    try:
                        if hasattr(group, "preview"):
                            group.preview = float(value)
                        print(f"[vrm_edit] INFO: set VRM0 blend_shape '{group.name}' = {value}",
                              file=sys.stderr)
                        found = True
                    except Exception as exc:
                        print(f"[vrm_edit] WARNING: failed to set blend_shape '{key}': {exc}",
                              file=sys.stderr)
                    break
            if not found:
                print(f"[vrm_edit] INFO: expression '{key}' not found in VRM0 blend shapes, skipping",
                      file=sys.stderr)


def _material_matches_category(mat_name: str, category: str) -> bool:
    """
    Heuristic: does the material name suggest it belongs to the given category?
    """
    name_lower = mat_name.lower()
    category_keywords = {
        "hair": ["hair", "hairs", "fur"],
        "skin": ["skin", "body", "face", "flesh"],
        "eye": ["eye", "eyes", "iris", "pupil", "sclera", "cornea"],
        "outfit": ["outfit", "cloth", "clothes", "clothing", "dress", "shirt",
                   "pants", "jacket", "uniform", "costume", "wear"],
    }
    keywords = category_keywords.get(category, [category])
    return any(kw in name_lower for kw in keywords)


def _set_material_base_color(mat, rgba: list, spec_version: str) -> None:
    """
    Set the base color of a VRM material.
    Tries VRM1 MToon path, then VRM0 path, then falls back to Blender's
    principled BSDF if available.
    """
    import bpy

    ext = getattr(mat, "vrm_addon_extension", None)
    applied = False

    if ext is not None:
        # VRM 1.x MToon path
        mtoon1 = getattr(ext, "mtoon1", None)
        if mtoon1 is not None:
            pbr = getattr(mtoon1, "pbr_metallic_roughness", None)
            if pbr is not None:
                bcf = getattr(pbr, "base_color_factor", None)
                if bcf is not None:
                    try:
                        bcf[0] = rgba[0]
                        bcf[1] = rgba[1]
                        bcf[2] = rgba[2]
                        if len(rgba) > 3:
                            bcf[3] = rgba[3]
                        applied = True
                        print(f"[vrm_edit] INFO: set VRM1 MToon base_color on '{mat.name}'",
                              file=sys.stderr)
                    except Exception as exc:
                        print(f"[vrm_edit] WARNING: VRM1 MToon base_color failed on '{mat.name}': {exc}",
                              file=sys.stderr)

        if not applied:
            # VRM 0.x path
            vrm0 = getattr(ext, "vrm0", None) or ext
            mtoon0 = getattr(vrm0, "mtoon", None)
            if mtoon0 is not None:
                color_prop = getattr(mtoon0, "lit_color", None) or getattr(mtoon0, "color", None)
                if color_prop is not None:
                    try:
                        color_prop[0] = rgba[0]
                        color_prop[1] = rgba[1]
                        color_prop[2] = rgba[2]
                        if len(rgba) > 3 and len(color_prop) > 3:
                            color_prop[3] = rgba[3]
                        applied = True
                        print(f"[vrm_edit] INFO: set VRM0 MToon color on '{mat.name}'",
                              file=sys.stderr)
                    except Exception as exc:
                        print(f"[vrm_edit] WARNING: VRM0 MToon color failed on '{mat.name}': {exc}",
                              file=sys.stderr)

    # Fallback: Blender Principled BSDF
    if not applied and mat.use_nodes and mat.node_tree:
        for node in mat.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                try:
                    base_color_input = node.inputs.get("Base Color")
                    if base_color_input is not None:
                        base_color_input.default_value = (
                            rgba[0], rgba[1], rgba[2],
                            rgba[3] if len(rgba) > 3 else 1.0,
                        )
                        applied = True
                        print(f"[vrm_edit] INFO: set Principled BSDF base_color on '{mat.name}'",
                              file=sys.stderr)
                except Exception as exc:
                    print(f"[vrm_edit] WARNING: BSDF base_color failed on '{mat.name}': {exc}",
                          file=sys.stderr)
                break

    if not applied:
        print(f"[vrm_edit] WARNING: could not set base_color on '{mat.name}' — no known property path",
              file=sys.stderr)


def _apply_materials(adjustments: dict, spec_version: str) -> None:
    """
    Apply material color adjustments from adjustments['materials'].
    Matches materials by name heuristic, skips unmatched.
    """
    import bpy

    materials_adj = adjustments.get("materials", {})
    if not materials_adj:
        return

    for category, rgba in materials_adj.items():
        matched = False
        for mat in bpy.data.materials:
            if mat is None:
                continue
            if _material_matches_category(mat.name, category):
                _set_material_base_color(mat, rgba, spec_version)
                matched = True
        if not matched:
            print(f"[vrm_edit] INFO: no material matched category '{category}', skipping",
                  file=sys.stderr)


def _apply_height_scale(adjustments: dict) -> None:
    """
    Apply uniform height_scale to the root armature (or root objects).
    """
    import bpy

    height_scale = adjustments.get("height_scale")
    if height_scale is None:
        return

    scale = float(height_scale)
    scaled = False

    # Try armature objects first
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE":
            obj.scale = (scale, scale, scale)
            scaled = True
            print(f"[vrm_edit] INFO: set armature '{obj.name}' scale = {scale}", file=sys.stderr)
            break

    # Fallback: root-level empty or mesh
    if not scaled:
        for obj in bpy.data.objects:
            if obj.parent is None and obj.type in ("EMPTY", "MESH"):
                obj.scale = (scale, scale, scale)
                scaled = True
                print(f"[vrm_edit] INFO: set root object '{obj.name}' scale = {scale}",
                      file=sys.stderr)
                break

    if not scaled:
        print("[vrm_edit] WARNING: no armature or root object found for height_scale",
              file=sys.stderr)


def _bpy_main() -> None:
    """
    Entry point when this file is executed as a Blender --python script.
    Parses args after '--', imports VRM, applies adjustments, exports VRM,
    exits non-zero on error.
    """
    import bpy  # noqa: F401  (only available inside Blender)

    argv = sys.argv
    try:
        sep = argv.index("--")
        script_args = argv[sep + 1:]
    except ValueError:
        script_args = []

    parser = argparse.ArgumentParser(
        description="Blender script: import VRM, apply adjustments, export VRM via VRM addon."
    )
    parser.add_argument("--in", dest="in_vrm", required=True,
                        help="Path to input .vrm file")
    parser.add_argument("--out", required=True, help="Path for output .vrm file")
    parser.add_argument("--adjustments-file", required=True,
                        help="Path to JSON file containing adjustments dict")
    args = parser.parse_args(script_args)

    in_vrm = os.path.abspath(args.in_vrm)
    out_vrm = os.path.abspath(args.out)

    # Load adjustments from temp JSON file
    with open(args.adjustments_file, "r", encoding="utf-8") as f:
        adjustments = json.load(f)

    # Ensure output directory exists
    os.makedirs(os.path.dirname(out_vrm) or ".", exist_ok=True)

    # -----------------------------------------------------------------
    # Check VRM addon availability
    # -----------------------------------------------------------------
    _check_vrm_addon_or_raise()

    # -----------------------------------------------------------------
    # Clear default scene and import VRM (not GLB — preserves VRM data)
    # -----------------------------------------------------------------
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    print(f"[vrm_edit] Importing VRM: {in_vrm}", file=sys.stderr)

    import_op = getattr(bpy.ops.import_scene, "vrm", None)
    if import_op is None:
        print("[vrm_edit] ERROR: import_scene.vrm operator not found (VRM addon not installed?)",
              file=sys.stderr)
        sys.exit(1)

    try:
        result = import_op(filepath=in_vrm)
    except Exception as exc:
        print(f"[vrm_edit] ERROR: VRM import failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if result != {"FINISHED"}:
        print(f"[vrm_edit] ERROR: VRM import returned: {result}", file=sys.stderr)
        sys.exit(1)

    # -----------------------------------------------------------------
    # Detect VRM spec version from armature
    # -----------------------------------------------------------------
    armature = None
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE":
            armature = obj
            break

    spec_version = "0"
    if armature is not None:
        ext = getattr(armature.data, "vrm_addon_extension", None)
        if ext is not None:
            sv = getattr(ext, "spec_version", None)
            if sv is not None:
                spec_version = str(sv)

    print(f"[vrm_edit] VRM spec_version: {spec_version!r}", file=sys.stderr)

    # -----------------------------------------------------------------
    # Apply adjustments (each section is safe-skip on error)
    # -----------------------------------------------------------------
    if armature is not None:
        try:
            _apply_expressions(armature, adjustments, spec_version)
        except Exception as exc:
            print(f"[vrm_edit] WARNING: expressions apply failed: {exc}", file=sys.stderr)
    else:
        print("[vrm_edit] WARNING: no armature found, skipping expression adjustments",
              file=sys.stderr)

    try:
        _apply_materials(adjustments, spec_version)
    except Exception as exc:
        print(f"[vrm_edit] WARNING: materials apply failed: {exc}", file=sys.stderr)

    try:
        _apply_height_scale(adjustments)
    except Exception as exc:
        print(f"[vrm_edit] WARNING: height_scale apply failed: {exc}", file=sys.stderr)

    # -----------------------------------------------------------------
    # Export VRM
    # -----------------------------------------------------------------
    print(f"[vrm_edit] Exporting VRM: {out_vrm}", file=sys.stderr)
    try:
        result = _run_vrm_export(out_vrm)
    except RuntimeError as exc:
        print(f"[vrm_edit] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"[vrm_edit] ERROR: VRM export failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if result != {"FINISHED"}:
        print(f"[vrm_edit] ERROR: VRM export returned: {result}", file=sys.stderr)
        sys.exit(1)

    print(f"[vrm_edit] VRM written: {out_vrm}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Python-side wrapper (runs in normal Python, without bpy)
# ---------------------------------------------------------------------------

def edit_vrm(
    in_vrm: str | Path,
    out_vrm: str | Path,
    adjustments: dict,
    blender_path: str | None = None,
) -> str:
    """
    Apply adjustments to a VRM file by launching Blender headless.

    Parameters
    ----------
    in_vrm:
        Path to the input .vrm file.
    out_vrm:
        Destination path for the output .vrm file.
    adjustments:
        Dictionary of adjustments to apply.  Recognized keys:

        expressions : dict, optional
            Mapping of expression name to weight (0.0–1.0).
            Supported names: happy, angry, sad, relaxed, surprised, blink.
        materials : dict, optional
            Mapping of material category to RGBA list ([r, g, b, a]).
            Supported categories: hair, skin, eye, outfit.
        height_scale : float, optional
            Uniform scale to apply to the root armature (1.0 = no change).

        Unknown keys and missing targets are silently skipped.
    blender_path:
        Blender binary path.  When *None*, the value of the ``BLENDER_PATH``
        environment variable is used; if that is also unset, ``"blender"`` is
        tried (must be on PATH).

    Returns
    -------
    str
        Absolute path to the written .vrm file.

    Raises
    ------
    FileNotFoundError
        When the input VRM file does not exist.
    RuntimeError
        When Blender exits with a non-zero return code (which includes the
        cases where the VRM addon is absent — the bpy script prints an
        actionable error message before exiting).
    """
    in_vrm = Path(in_vrm).resolve()
    out_vrm = Path(out_vrm).resolve()

    if not in_vrm.exists():
        raise FileNotFoundError(f"Input VRM not found: {in_vrm}")

    if blender_path is None:
        blender_path = os.environ.get("BLENDER_PATH", "blender")

    # This script file is the bpy script Blender will execute
    script_path = Path(__file__).resolve()

    # Write adjustments to a temporary JSON file to avoid command-line length limits
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        json.dump(adjustments, tmp)
        adjustments_file = tmp.name

    try:
        cmd = [
            blender_path,
            "--background",
            "--python", str(script_path),
            "--",
            "--in", str(in_vrm),
            "--out", str(out_vrm),
            "--adjustments-file", adjustments_file,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        # Always clean up the temp file
        try:
            os.unlink(adjustments_file)
        except OSError:
            pass

    if result.returncode != 0:
        stderr_tail = result.stderr[-2000:] if result.stderr else ""
        stdout_tail = result.stdout[-1000:] if result.stdout else ""
        raise RuntimeError(
            f"Blender exited with code {result.returncode} during VRM edit.\n"
            f"--- stderr (tail) ---\n{stderr_tail}\n"
            f"--- stdout (tail) ---\n{stdout_tail}"
        )

    return str(out_vrm)


# ---------------------------------------------------------------------------
# When executed inside Blender as a --python script
# ---------------------------------------------------------------------------

# Blender always has `bpy` importable; plain Python does not.
try:
    import bpy as _bpy_probe  # noqa: F401
    _INSIDE_BLENDER = True
except ImportError:
    _INSIDE_BLENDER = False

if _INSIDE_BLENDER and __name__ == "__main__":
    _bpy_main()
