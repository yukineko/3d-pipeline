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
import math
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


def _as_mesh_object(obj):
    """
    Coerce a bind reference into a Blender MESH object that carries shape keys.

    The VRM addon exposes a bind's mesh target through varying wrappers across
    versions (the object directly, or via ``mesh_object`` / ``bpy_object`` /
    ``value``). We accept the first candidate whose ``.data.shape_keys`` is
    present, which is exactly what bake needs. Returns *None* when nothing in the
    chain looks like a shape-key-bearing mesh.
    """
    if obj is None:
        return None
    for sub in (None, "mesh_object", "bpy_object", "value"):
        candidate = obj if sub is None else getattr(obj, sub, None)
        if candidate is None:
            continue
        data = getattr(candidate, "data", None)
        if data is not None and getattr(data, "shape_keys", None) is not None:
            return candidate
    return None


def _resolve_bind_mesh(bind):
    """
    Resolve the Blender MESH object that an expression/blend-shape *bind* drives.

    VRM1 ``morph_target_binds`` reference the mesh via ``node``; VRM0 group
    ``binds`` use ``mesh``. Attribute names vary by addon version, so we probe a
    defensive set of candidates and, as a last resort, resolve a mesh *name*
    through ``bpy.data.objects``. Returns *None* (caller skips the bind) when no
    shape-key-bearing mesh can be found — never guesses.
    """
    candidates = []
    for attr in ("node", "mesh", "mesh_object", "target"):
        c = getattr(bind, attr, None)
        if c is not None:
            candidates.append(c)

    for c in candidates:
        obj = _as_mesh_object(c)
        if obj is not None:
            return obj

    # Name-based fallback (real addon stores e.g. node.mesh_object_name). Only
    # reachable inside Blender; in plain Python `import bpy` fails and we bail.
    try:
        import bpy
    except Exception:
        return None
    for c in candidates:
        name = getattr(c, "mesh_object_name", None) or getattr(c, "name", None)
        if isinstance(name, str) and name:
            try:
                obj = bpy.data.objects.get(name)
            except Exception:
                obj = None
            mesh = _as_mesh_object(obj)
            if mesh is not None:
                return mesh
    return None


def _bake_binds(value, binds) -> bool:
    """
    Drive the shape keys named by *binds* to ``value * bind.weight`` and bake the
    resulting mix into each affected mesh's basis via
    ``mesh.shape_key_add(from_mix=True)``.

    This is the only way an expression survives VRM export: the exporter zeroes
    every ``expression.preview`` before writing, so a preview assignment is a
    silent no-op. Baking folds the deformation into the mesh geometry instead.

    Returns *True* iff at least one mesh was actually baked, so the caller can
    count it as ``applied`` only when a real, export-surviving change happened.
    """
    if binds is None:
        return False

    affected = {}  # id(mesh) -> mesh object (dedupe meshes touched by >1 bind)
    for bind in binds:
        mesh_obj = _resolve_bind_mesh(bind)
        if mesh_obj is None:
            print("[vrm_edit] INFO: expression bind has no resolvable mesh, skipping bind",
                  file=sys.stderr)
            continue

        index = getattr(bind, "index", None)
        if index is None:
            print("[vrm_edit] INFO: expression bind has no shape-key index, skipping bind",
                  file=sys.stderr)
            continue

        bind_weight = getattr(bind, "weight", 1.0)
        try:
            bind_weight = float(bind_weight)
        except (TypeError, ValueError):
            bind_weight = 1.0

        data = getattr(mesh_obj, "data", None)
        shape_keys = getattr(data, "shape_keys", None) if data is not None else None
        key_blocks = getattr(shape_keys, "key_blocks", None) if shape_keys is not None else None
        if key_blocks is None:
            print("[vrm_edit] INFO: mesh has no shape keys to drive, skipping bind",
                  file=sys.stderr)
            continue

        try:
            key_blocks[index].value = float(value) * bind_weight
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            print(f"[vrm_edit] INFO: could not set shape key [{index}] on "
                  f"'{getattr(mesh_obj, 'name', '?')}' ({exc}), skipping bind",
                  file=sys.stderr)
            continue

        affected[id(mesh_obj)] = mesh_obj

    baked = False
    for mesh_obj in affected.values():
        try:
            mesh_obj.shape_key_add(name="_vrm_edit_baked", from_mix=True)
            baked = True
            print(f"[vrm_edit] INFO: baked expression mix into basis of "
                  f"'{getattr(mesh_obj, 'name', '?')}'", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - shape_key_add API varies by build
            print(f"[vrm_edit] WARNING: shape_key_add(from_mix=True) failed on "
                  f"'{getattr(mesh_obj, 'name', '?')}': {exc}", file=sys.stderr)
    return baked


def _apply_expressions(armature, adjustments: dict, spec_version: str) -> dict:
    """
    Apply expression weights from adjustments['expressions'] to the VRM armature
    by *baking* the driven shape keys into the mesh basis.

    The VRM exporter clears every ``expression.preview`` / ``blend_shape_group.
    preview`` right before writing, so assigning ``preview`` does nothing in the
    exported file. Instead we follow each expression's binds to the shape keys it
    drives (VRM1 ``morph_target_binds`` / VRM0 group ``binds``), set their values,
    and fold the mix into the basis with ``shape_key_add(from_mix=True)``.

    Returns an accounting dict ``{"expressions": {"requested": N, "applied": M}}``.
    *applied* counts only expressions that were genuinely baked into geometry —
    a preview-only fallback is **not** counted, so a silently-ineffective edit is
    still reported as applied == 0 (consistent with ``_assert_adjustments_applied``).
    """
    expressions = adjustments.get("expressions", {})
    requested = len(expressions)
    applied = 0

    def _account():
        return {"expressions": {"requested": requested, "applied": applied}}

    if not expressions:
        return _account()

    ext = getattr(armature.data, "vrm_addon_extension", None)
    if ext is None:
        print("[vrm_edit] WARNING: armature has no vrm_addon_extension, skipping expressions",
              file=sys.stderr)
        return _account()

    # VRM 1.x
    if spec_version.startswith("1"):
        vrm1 = getattr(ext, "vrm1", None)
        if vrm1 is None:
            print("[vrm_edit] WARNING: vrm1 not found in extension, skipping expressions",
                  file=sys.stderr)
            return _account()
        exprs = getattr(vrm1, "expressions", None)
        if exprs is None:
            print("[vrm_edit] WARNING: vrm1.expressions not found, skipping expressions",
                  file=sys.stderr)
            return _account()
        preset = getattr(exprs, "preset", None)
        if preset is None:
            print("[vrm_edit] WARNING: vrm1.expressions.preset not found, skipping expressions",
                  file=sys.stderr)
            return _account()

        for key, value in expressions.items():
            expr_obj = getattr(preset, key, None)
            if expr_obj is None:
                print(f"[vrm_edit] INFO: expression '{key}' not found in VRM1 presets, skipping",
                      file=sys.stderr)
                continue

            binds = getattr(expr_obj, "morph_target_binds", None)
            if _bake_binds(value, binds):
                applied += 1
                print(f"[vrm_edit] INFO: baked VRM1 expression '{key}' = {value}",
                      file=sys.stderr)
                continue

            # Preview fallback: keeps the in-Blender weight visible but does NOT
            # survive export, so it is intentionally not counted as applied.
            if hasattr(expr_obj, "preview"):
                try:
                    expr_obj.preview = float(value)
                except Exception as exc:
                    print(f"[vrm_edit] WARNING: preview fallback for '{key}' failed: {exc}",
                          file=sys.stderr)
            print(f"[vrm_edit] INFO: expression '{key}' had no bakeable binds; "
                  f"preview set as fallback (not counted as applied)", file=sys.stderr)

        return _account()

    # VRM 0.x - uses blend_shape_master / blend_shape_groups
    vrm0 = getattr(ext, "vrm0", None)
    if vrm0 is None:
        # Some versions expose it directly
        vrm0 = ext

    blend_shape_master = getattr(vrm0, "blend_shape_master", None)
    if blend_shape_master is None:
        print("[vrm_edit] WARNING: blend_shape_master not found, skipping expressions",
              file=sys.stderr)
        return _account()

    blend_shape_groups = getattr(blend_shape_master, "blend_shape_groups", None)
    if blend_shape_groups is None:
        print("[vrm_edit] WARNING: blend_shape_groups not found, skipping expressions",
              file=sys.stderr)
        return _account()

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
        matched_group = None
        for group in blend_shape_groups:
            group_name = getattr(group, "name", "").lower()
            group_preset = getattr(group, "preset_name", "").lower()
            if any(n in group_name or n == group_preset for n in lookup_names):
                matched_group = group
                break

        if matched_group is None:
            print(f"[vrm_edit] INFO: expression '{key}' not found in VRM0 blend shapes, skipping",
                  file=sys.stderr)
            continue

        binds = getattr(matched_group, "binds", None)
        if _bake_binds(value, binds):
            applied += 1
            print(f"[vrm_edit] INFO: baked VRM0 blend_shape "
                  f"'{getattr(matched_group, 'name', key)}' = {value}", file=sys.stderr)
            continue

        # Preview fallback (not counted as applied — does not survive export).
        if hasattr(matched_group, "preview"):
            try:
                matched_group.preview = float(value)
            except Exception as exc:
                print(f"[vrm_edit] WARNING: preview fallback for '{key}' failed: {exc}",
                      file=sys.stderr)
        print(f"[vrm_edit] INFO: blend_shape '{key}' had no bakeable binds; "
              f"preview set as fallback (not counted as applied)", file=sys.stderr)

    return _account()


# Token sets for material classification. Lowercased substring match against the
# material name (and, when available, MToon/texture image node names). These cover
# both VRoid Studio coded names (e.g. ``N00_000_00_Body_00_SKIN``) and ad-hoc
# English names, since the trailing MToon suffix (SKIN/HAIR/CLOTH/EYE/FACE) is itself
# a substring (e.g. "skin" in "...SKIN").
_CATEGORY_TOKENS = {
    "eye": ["eye", "eyes", "iris", "pupil", "sclera", "cornea", "eyeline",
            "eyelash", "eyelashout", "eyebrow", "highlight"],
    "hair": ["hair", "hairs", "fur"],
    "outfit": ["cloth", "clothes", "clothing", "outfit", "costume", "dress",
               "shirt", "pants", "jacket", "uniform", "wear", "tops", "bottoms",
               "onepiece", "shoes", "accessory"],
    "skin": ["skin", "body", "face", "flesh"],
}

# Japanese / hand-named VRM tokens. Substring match on the raw (non-lowercased) name.
_CATEGORY_TOKENS_JP = {
    "eye": ["目", "瞳", "まつ", "睫", "眉"],
    "hair": ["髪"],
    "outfit": ["服", "衣装", "靴"],
    "skin": ["肌", "顔", "体"],
}

# Precedence order. Evaluated first-wins so that eye-region parts whose VRoid suffix
# is FACE (Eyeline/Eyelash/Eyebrow) classify as eye rather than skin(face).
_CATEGORY_PRECEDENCE = ("eye", "hair", "outfit", "skin")


def _gather_texture_names(mat) -> list:
    """
    Best-effort collection of MToon/texture image node names from a Blender
    material object. Every structure access is defensively guarded so a missing
    or garbage node tree never raises — on any failure an empty list is returned.
    """
    names = []
    try:
        node_tree = getattr(mat, "node_tree", None)
        nodes = getattr(node_tree, "nodes", None)
        if nodes is not None:
            for node in nodes:
                try:
                    n = getattr(node, "name", None)
                    if n:
                        names.append(str(n))
                    img = getattr(node, "image", None)
                    img_name = getattr(img, "name", None)
                    if img_name:
                        names.append(str(img_name))
                except Exception:
                    continue
    except Exception:
        return names
    # Texture slots (legacy materials).
    try:
        slots = getattr(mat, "texture_slots", None)
        if slots is not None:
            for slot in slots:
                try:
                    sn = getattr(slot, "name", None)
                    if sn:
                        names.append(str(sn))
                except Exception:
                    continue
    except Exception:
        pass
    return names


def _classify_material(mat_name: str, mat=None):
    """
    Classify a material into ``'hair' | 'skin' | 'eye' | 'outfit' | None``.

    VRoid Studio exports coded names like ``N00_000_00_Body_00_SKIN`` and
    ``N00_000_00_Eyeline_00_FACE``; Japanese/hand-named VRMs use ``髪``/``肌``/``瞳``
    etc. Classification is evaluated in precedence order **eye → hair → outfit →
    skin** so that eye-region parts carrying a ``FACE`` suffix still resolve to eye.

    Signal sources, in order:
      a. VRoid/English part & suffix tokens (case-insensitive substring on name).
      b. Japanese tokens (substring on the raw name).
      c. MToon/texture image node names on ``mat`` (same token sets), used as a
         secondary hint when the name alone is ambiguous.
    Returns None if nothing matches.
    """
    name_lower = (mat_name or "").lower()

    # (a) English / VRoid coded tokens.
    for category in _CATEGORY_PRECEDENCE:
        if any(tok in name_lower for tok in _CATEGORY_TOKENS[category]):
            return category

    # (b) Japanese tokens (raw name).
    raw = mat_name or ""
    for category in _CATEGORY_PRECEDENCE:
        if any(tok in raw for tok in _CATEGORY_TOKENS_JP[category]):
            return category

    # (c) Secondary hint from MToon/texture image names.
    if mat is not None:
        for tex_name in _gather_texture_names(mat):
            tl = tex_name.lower()
            for category in _CATEGORY_PRECEDENCE:
                if any(tok in tl for tok in _CATEGORY_TOKENS[category]):
                    return category
            for category in _CATEGORY_PRECEDENCE:
                if any(tok in tex_name for tok in _CATEGORY_TOKENS_JP[category]):
                    return category

    return None


def _material_matches_category(mat_name: str, category: str, mat=None) -> bool:
    """
    Does the material name (and optional material object) suggest it belongs to
    the given category? Backward-compatible 2-arg form is preserved.
    """
    return _classify_material(mat_name, mat) == category


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


def _apply_materials(adjustments: dict, spec_version: str) -> dict:
    """
    Apply material color adjustments from adjustments['materials'].
    Matches materials by name heuristic, skips unmatched.

    Returns ``{"materials": {"requested": N, "applied": M}}`` where *applied*
    counts requested categories that matched at least one material — so a color
    instruction that hit no material is detectable as a silent no-op.
    """
    import bpy

    materials_adj = adjustments.get("materials", {})
    requested = len(materials_adj)
    applied = 0
    if not materials_adj:
        return {"materials": {"requested": 0, "applied": 0}}

    for category, rgba in materials_adj.items():
        matched = False
        for mat in bpy.data.materials:
            if mat is None:
                continue
            if _material_matches_category(mat.name, category, mat):
                _set_material_base_color(mat, rgba, spec_version)
                matched = True
        if matched:
            applied += 1
        else:
            available = [m.name for m in bpy.data.materials if m]
            if available:
                print(f"[vrm_edit] WARNING: no material matched category '{category}', "
                      f"skipping. Available materials: {available}", file=sys.stderr)
            else:
                print(f"[vrm_edit] WARNING: no material matched category '{category}', "
                      f"skipping. no materials in scene", file=sys.stderr)

    return {"materials": {"requested": requested, "applied": applied}}


# Safe bounds for a uniform height scale, kept in sync with the inference-layer
# clamp in vroid_params (0.5..2.0). edit_vrm() is a public API that can be called
# with arbitrary adjustments, so the bpy layer must guard independently.
_HEIGHT_SCALE_MIN = 0.5
_HEIGHT_SCALE_MAX = 2.0


def _sanitize_height_scale(height_scale) -> float:
    """
    Coerce a requested height_scale into a safe, applicable factor.

    Rejects non-finite (NaN/inf) and non-positive values with ValueError — a
    scale of 0, a negative number, or NaN would collapse or invert the avatar.
    Finite positive values are clamped to [_HEIGHT_SCALE_MIN, _HEIGHT_SCALE_MAX].
    """
    scale = float(height_scale)
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError(
            f"height_scale must be a finite positive number, got {height_scale!r}"
        )
    return max(_HEIGHT_SCALE_MIN, min(_HEIGHT_SCALE_MAX, scale))


def _child_meshes_of(bpy, armature) -> list:
    """
    Return MESH objects driven by ``armature``: either parented to it, or bound
    via an ARMATURE modifier that targets it. These must be baked together with
    the armature so the geometry is scaled, not just the bone transforms.
    """
    children = []
    for obj in bpy.data.objects:
        if getattr(obj, "type", None) != "MESH":
            continue
        if getattr(obj, "parent", None) is armature:
            children.append(obj)
            continue
        for mod in getattr(obj, "modifiers", []) or []:
            if getattr(mod, "type", None) == "ARMATURE" and getattr(mod, "object", None) is armature:
                children.append(obj)
                break
    return children


def _bake_scale_into_geometry(bpy, primary, objects) -> None:
    """
    Apply the pending object scale of ``objects`` into their geometry via
    ``transform_apply(scale=True)``, with ``primary`` as the active object.

    VRM export does not reliably carry a non-unit root/armature node scale
    (VRM0 bakes it into bone world translation; VRM1 may drop a non-unit root
    TRS), so the scale must be baked into the mesh data itself. Raises on
    failure; the caller keeps the object-scale fallback.
    """
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = primary
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)


def _apply_height_scale(adjustments: dict) -> dict:
    """
    Apply uniform height_scale to the root armature (or root objects).

    The requested scale is set on the object and then baked into geometry with
    ``transform_apply(scale=True)`` so the resize survives VRM0/VRM1 export
    (where a non-unit node scale is otherwise dropped or normalized).

    Returns ``{"height_scale": {"requested": N, "applied": M}}`` (N/M are 0 or 1)
    so an unscalable model is not reported as a successful resize.
    """
    import bpy

    height_scale = adjustments.get("height_scale")
    if height_scale is None:
        return {"height_scale": {"requested": 0, "applied": 0}}

    scale = _sanitize_height_scale(height_scale)
    scaled = False
    bake_targets = None
    bake_primary = None

    # Try armature objects first: scale the armature and bake it together with
    # the meshes it drives so the geometry (not just bones) is resized.
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE":
            obj.scale = (scale, scale, scale)
            scaled = True
            bake_primary = obj
            bake_targets = [obj] + _child_meshes_of(bpy, obj)
            print(f"[vrm_edit] INFO: set armature '{obj.name}' scale = {scale}", file=sys.stderr)
            break

    # Fallback: root-level empty or mesh
    if not scaled:
        for obj in bpy.data.objects:
            if obj.parent is None and obj.type in ("EMPTY", "MESH"):
                obj.scale = (scale, scale, scale)
                scaled = True
                bake_primary = obj
                bake_targets = [obj]
                print(f"[vrm_edit] INFO: set root object '{obj.name}' scale = {scale}",
                      file=sys.stderr)
                break

    if not scaled:
        print("[vrm_edit] WARNING: no armature or root object found for height_scale",
              file=sys.stderr)
        return {"height_scale": {"requested": 1, "applied": 0}}

    # Bake the object scale into geometry. If the op fails (e.g. an unexpected
    # headless context), keep the object-scale fallback rather than aborting.
    try:
        _bake_scale_into_geometry(bpy, bake_primary, bake_targets)
        print(f"[vrm_edit] INFO: baked height_scale into geometry of "
              f"{len(bake_targets)} object(s)", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - op failures vary by Blender build
        print(f"[vrm_edit] WARNING: transform_apply(scale) failed ({exc!r}); "
              f"keeping object-scale fallback", file=sys.stderr)

    return {"height_scale": {"requested": 1, "applied": 1 if scaled else 0}}


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
    parser.add_argument("--report-file", default=None,
                        help="Optional path to write an applied-vs-requested JSON report")
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
    # Apply adjustments (each section is safe-skip on error) and record an
    # applied-vs-requested account so the wrapper can reject silent no-ops.
    # -----------------------------------------------------------------
    report: dict = {}
    report["spec_version"] = spec_version

    if armature is not None:
        try:
            report.update(_apply_expressions(armature, adjustments, spec_version))
        except Exception as exc:
            print(f"[vrm_edit] WARNING: expressions apply failed: {exc}", file=sys.stderr)
            report["expressions"] = {
                "requested": len(adjustments.get("expressions", {})), "applied": 0}
    else:
        print("[vrm_edit] WARNING: no armature found, skipping expression adjustments",
              file=sys.stderr)
        report["expressions"] = {
            "requested": len(adjustments.get("expressions", {})), "applied": 0}

    try:
        report.update(_apply_materials(adjustments, spec_version))
    except Exception as exc:
        print(f"[vrm_edit] WARNING: materials apply failed: {exc}", file=sys.stderr)
        report["materials"] = {
            "requested": len(adjustments.get("materials", {})), "applied": 0}

    try:
        report.update(_apply_height_scale(adjustments))
    except Exception as exc:
        print(f"[vrm_edit] WARNING: height_scale apply failed: {exc}", file=sys.stderr)
        report["height_scale"] = {
            "requested": 0 if adjustments.get("height_scale") is None else 1, "applied": 0}

    if args.report_file:
        try:
            with open(args.report_file, "w", encoding="utf-8") as rf:
                json.dump(report, rf)
        except OSError as exc:
            print(f"[vrm_edit] WARNING: could not write report file: {exc}", file=sys.stderr)

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
    apply_log_dir: str | Path | None = None,
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
    apply_log_dir:
        Optional directory to persist the apply report as ``apply_log.json``
        (best-effort).  When *None*, no report is persisted.  Persistence
        failures are logged as warnings and never fail the edit.

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

    # Blender writes an applied-vs-requested report here; we read it back to
    # reject edits that matched zero targets (silent no-ops).
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".report.json",
        delete=False,
        encoding="utf-8",
    ) as rtmp:
        report_file = rtmp.name

    try:
        cmd = [
            blender_path,
            "--background",
            "--python", str(script_path),
            "--",
            "--in", str(in_vrm),
            "--out", str(out_vrm),
            "--adjustments-file", adjustments_file,
            "--report-file", report_file,
        ]

        timeout = int(os.environ.get("VRM_SUBPROCESS_TIMEOUT", "600"))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Blender timed out after {timeout}s during VRM edit."
            )

        if result.returncode != 0:
            stderr_tail = result.stderr[-2000:] if result.stderr else ""
            stdout_tail = result.stdout[-1000:] if result.stdout else ""
            raise RuntimeError(
                f"Blender exited with code {result.returncode} during VRM edit.\n"
                f"--- stderr (tail) ---\n{stderr_tail}\n"
                f"--- stdout (tail) ---\n{stdout_tail}"
            )

        # Output guarantee: a clean Blender exit does not prove a valid VRM was
        # written.  Reject a missing/empty/corrupt file before reporting success.
        from render.vrm_utils import assert_valid_glb

        assert_valid_glb(out_vrm)

        # Reject silent no-ops: an explicitly requested adjustment that matched
        # zero targets means the user's instruction had no effect.
        _assert_adjustments_applied(report_file)

        # Best-effort: persist the apply report next to the ledger record so a
        # non-expert can later inspect requested-vs-applied counts. Persistence
        # failures must never fail the edit itself.
        if apply_log_dir is not None:
            try:
                with open(report_file, "r", encoding="utf-8") as rf:
                    report_dict = json.load(rf)
                from render import obs
                obs.write_apply_log(apply_log_dir, report_dict)
            except Exception as exc:  # noqa: BLE001 - best-effort persistence
                try:
                    from render import obs
                    obs.get_logger("vrm_edit").warning(
                        "could not persist apply_log.json: %s", exc)
                except Exception:
                    print(f"[vrm_edit] WARNING: could not persist apply_log.json: {exc}",
                          file=sys.stderr)
    finally:
        # Always clean up the temp files
        for _f in (adjustments_file, report_file):
            try:
                os.unlink(_f)
            except OSError:
                pass

    return str(out_vrm)


def _assert_adjustments_applied(report_file: str) -> None:
    """
    Raise ``RuntimeError`` when the Blender-side report shows a requested
    adjustment section matched zero targets (requested > 0, applied == 0).

    A missing/empty/unparseable report is treated as "no accounting available"
    and skipped — this keeps the guard backward-compatible with callers/mocks
    that do not produce a report.
    """
    try:
        with open(report_file, "r", encoding="utf-8") as rf:
            report = json.load(rf)
    except (OSError, ValueError):
        return

    if not isinstance(report, dict):
        return

    empties = [
        section
        for section, counts in report.items()
        if isinstance(counts, dict)
        and counts.get("requested", 0) > 0
        and counts.get("applied", 0) == 0
    ]
    if empties:
        raise RuntimeError(
            "VRM edit applied no changes for requested section(s): "
            f"{', '.join(sorted(empties))}. The instruction matched no targets "
            f"in the model (report: {report}). Refusing to report success."
        )


# ---------------------------------------------------------------------------
# Host-side CLI (plain Python; no bpy).  The host wrapper `edit_vrm()` spawns
# Blender itself at runtime, so argument parsing here needs no Blender.
# ---------------------------------------------------------------------------

def _host_main() -> None:
    """Plain-Python CLI entry: parse args, load the adjustments JSON, and call
    ``edit_vrm()`` which launches Blender headlessly."""
    parser = argparse.ArgumentParser(
        description="Edit an existing VRM headlessly via the Blender VRM addon.",
    )
    parser.add_argument("--in", dest="in_vrm", required=True,
                        help="Path to the input .vrm file.")
    parser.add_argument("--out", dest="out", required=True,
                        help="Destination path for the output .vrm file.")
    parser.add_argument("--adjustments-file", dest="adjustments_file", required=True,
                        help="Path to a JSON file containing the adjustments dict.")
    parser.add_argument("--blender", dest="blender", default=None,
                        help="Path to the Blender executable (default: auto).")
    parser.add_argument("--apply-log-dir", dest="apply_log_dir", default=None,
                        help="Directory to write the apply-log into (optional).")
    args = parser.parse_args()

    try:
        with open(args.adjustments_file, "r", encoding="utf-8") as af:
            adjustments = json.load(af)
    except (OSError, ValueError) as exc:
        print(f"error: failed to read adjustments file "
              f"{args.adjustments_file!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    out_path = edit_vrm(
        args.in_vrm,
        args.out,
        adjustments,
        blender_path=args.blender,
        apply_log_dir=args.apply_log_dir,
    )
    print(out_path)


# ---------------------------------------------------------------------------
# When executed inside Blender as a --python script
# ---------------------------------------------------------------------------

# Blender always has `bpy` importable; plain Python does not.
try:
    import bpy as _bpy_probe  # noqa: F401
    _INSIDE_BLENDER = True
except ImportError:
    _INSIDE_BLENDER = False

if __name__ == "__main__":
    if _INSIDE_BLENDER:
        _bpy_main()
    else:
        _host_main()
