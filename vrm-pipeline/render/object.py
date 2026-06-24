"""
render/object.py - 4-view headless render for .blend / .glb / .obj / .fbx.

Usage (Blender headless):
    blender --background --python render/object.py -- \\
        --input <path>           # .blend / .glb / .gltf / .obj / .fbx (required)
        --output-dir <dir>       # output directory (required)
        [--golden-dir <dir>]     # eval-A golden render dir for pHash comparison
        [--resolution 768]       # render resolution (default 768)
        [--scene-context <path>] # terrain scene .blend: place object in context and render 2 extra views
"""

import sys
import os
import json
import argparse
import math
import traceback

# Allow `from core import ...` when run directly as a Blender script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import (
    clear_scene, remove_all_lights, setup_unlit_materials,
    get_scene_bounds, create_ortho_camera, create_persp_camera, remove_camera,
    configure_render, render_to_file,
    compute_phash_signal, get_script_sha256, write_manifest,
)


# ---------------------------------------------------------------------------
# Format importers
# ---------------------------------------------------------------------------

def _build_importers():
    """Return dict mapping lowercase extension -> import callable."""
    import bpy
    return {
        ".blend": lambda p: bpy.ops.wm.open_mainfile(filepath=p),
        ".glb":   lambda p: bpy.ops.import_scene.gltf(filepath=p),
        ".gltf":  lambda p: bpy.ops.import_scene.gltf(filepath=p),
        ".obj":   lambda p: bpy.ops.import_scene.obj(filepath=p),
        ".fbx":   lambda p: bpy.ops.import_scene.fbx(filepath=p),
    }


def import_object(input_path):
    """
    Import the object file into the current Blender scene.

    For .blend files, open_mainfile replaces the current scene — no clear_scene
    needed. For other formats, the caller must have already called clear_scene.
    Returns the lowercase extension string.
    """
    import bpy
    ext = os.path.splitext(input_path)[1].lower()
    importers = _build_importers()
    if ext not in importers:
        raise ValueError(f"Unsupported file format: {ext!r}. Supported: {list(importers.keys())}")
    importers[ext](input_path)
    return ext


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    argv = sys.argv
    try:
        sep = argv.index("--")
        script_args = argv[sep + 1:]
    except ValueError:
        script_args = []

    parser = argparse.ArgumentParser(
        description="4-view headless render for .blend/.glb/.obj/.fbx."
    )
    parser.add_argument("--input", required=True, help="Path to input file (.blend/.glb/.gltf/.obj/.fbx)")
    parser.add_argument("--output-dir", required=True, dest="output_dir", help="Output directory")
    parser.add_argument("--golden-dir", default=None, dest="golden_dir",
                        help="Golden render directory for pHash comparison")
    parser.add_argument("--resolution", type=int, default=768)
    parser.add_argument("--scene-context", default=None, dest="scene_context",
                        help="Terrain scene .blend: place imported object here for context renders")
    return parser.parse_args(script_args)


# ---------------------------------------------------------------------------
# Camera layout
# ---------------------------------------------------------------------------

OBJ_FACES = ["obj_front", "obj_side", "obj_top", "obj_persp"]
CONTEXT_FACES = ["context_front", "context_persp"]


def _compute_camera_params(bounds):
    """
    Compute camera position/rotation/scale parameters for the 4 standard views.

    Camera conventions (Blender coordinate system: Y-forward, Z-up):
      obj_front : ORTHO, from Y- direction looking toward +Y (Z-up)
      obj_side  : ORTHO, from X+ direction looking toward -X (Z-up)
      obj_top   : ORTHO, from Z+ direction looking toward -Z (Y-back)
      obj_persp : PERSP, from front-right-top 45° diagonal

    Rotation tuples are (X, Y, Z) Euler angles in radians (XYZ order).
    """
    min_x, min_y, min_z = bounds[0], bounds[1], bounds[2]
    max_x, max_y, max_z = bounds[3], bounds[4], bounds[5]

    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    cz = (min_z + max_z) / 2.0

    width  = max_x - min_x
    depth  = max_y - min_y
    height = max_z - min_z

    max_dim = max(width, depth, height)
    # Guard against degenerate (zero-size) objects
    if max_dim < 1e-6:
        max_dim = 1.0

    ortho_scale  = max_dim * 1.2   # 10% margin on each side (1.2 = 1.0 + 2*0.1)
    ortho_dist   = max_dim * 3.0   # far enough for clip
    persp_dist   = max_dim * 3.0   # object fills ~2/3 of FOV=50° frame

    # obj_front: camera on Y- axis, pointing +Y; up = +Z
    #   rotation: X=90°, Y=0°, Z=0°
    front_loc = (cx, cy - ortho_dist, cz)
    front_rot = (math.radians(90), 0.0, 0.0)

    # obj_side: camera on X+ axis, pointing -X; up = +Z
    #   rotation: X=90°, Y=0°, Z=90°
    side_loc = (cx + ortho_dist, cy, cz)
    side_rot = (math.radians(90), 0.0, math.radians(90))

    # obj_top: camera on Z+ axis, pointing -Z; forward direction = -Y
    #   rotation: X=0°, Y=0°, Z=0° (Blender camera -Z points down, +Y points back)
    top_loc = (cx, cy, cz + ortho_dist)
    top_rot = (0.0, 0.0, 0.0)

    # obj_persp: 45° front-right-top diagonal
    #   offset equally in X+, Y-, Z+ at 45°, then look at center
    #   direction vector: (-1, +1, -1) normalized from camera -> target
    #   Using elevation 35.264° (arctan(1/sqrt(2))) for true isometric
    diag = persp_dist / math.sqrt(3.0)
    persp_loc = (cx + diag, cy - diag, cz + diag)
    # Rotation to point from persp_loc toward center (cx, cy, cz):
    #   azimuth = -45° (from +Y axis toward +X), elevation = 35.264°
    #   In Blender XYZ Euler: X = 90° - elevation ≈ 54.736°, Z = 45°
    persp_rot = (math.radians(54.736), 0.0, math.radians(45))

    return {
        "obj_front": (front_loc, front_rot, "ORTHO", ortho_scale),
        "obj_side":  (side_loc,  side_rot,  "ORTHO", ortho_scale),
        "obj_top":   (top_loc,   top_rot,   "ORTHO", ortho_scale),
        "obj_persp": (persp_loc, persp_rot, "PERSP", 50.0),
    }


def _compute_context_camera_params(bounds):
    """
    Camera params for the 2 context views (obj placed in terrain scene).
    Uses same front and persp positions as the 4-view set.
    """
    params_4v = _compute_camera_params(bounds)
    return {
        "context_front": params_4v["obj_front"],
        "context_persp": params_4v["obj_persp"],
    }


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

def _render_one(face_name, params, output_dir, errors):
    """
    Create camera, render, remove camera.
    params = (location, rotation_euler, camera_type, scale_or_fov)
    """
    import bpy

    loc, rot, cam_type, scale_or_fov = params
    if cam_type == "ORTHO":
        cam = create_ortho_camera(face_name, loc, rot, scale_or_fov)
    else:
        cam = create_persp_camera(face_name, loc, rot, fov_degrees=scale_or_fov)

    bpy.context.scene.camera = cam
    out_path = os.path.join(output_dir, face_name + ".webp")
    try:
        render_to_file(out_path)
        print(f"[object.py] Rendered {face_name} -> {out_path}")
        return out_path
    except Exception as exc:
        msg = f"Render failed for {face_name}: {exc}"
        print(f"[object.py] ERROR: {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        errors.append(msg)
        return None
    finally:
        remove_camera(cam)


def render_4views(bounds, output_dir, errors):
    """Render obj_front, obj_side, obj_top, obj_persp."""
    params = _compute_camera_params(bounds)
    rendered = {}
    for face_name in OBJ_FACES:
        out_path = _render_one(face_name, params[face_name], output_dir, errors)
        if out_path is not None:
            rendered[face_name] = out_path
    return rendered


def render_context_views(bounds, output_dir, errors):
    """Render context_front and context_persp (lit, no unlit override)."""
    params = _compute_context_camera_params(bounds)
    rendered = {}
    for face_name in CONTEXT_FACES:
        out_path = _render_one(face_name, params[face_name], output_dir, errors)
        if out_path is not None:
            rendered[face_name] = out_path
    return rendered


# ---------------------------------------------------------------------------
# Ground the object (min_z -> 0)
# ---------------------------------------------------------------------------

def ground_object(obj_names):
    """
    Move the named objects so their collective bounding box min_z == 0.
    obj_names: set/list of object names that belong to the imported object.
    """
    import bpy
    from mathutils import Vector

    inf = float("inf")
    min_z = inf
    for name in obj_names:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH":
            continue
        for corner in obj.bound_box:
            wco = obj.matrix_world @ Vector(corner)
            if wco[2] < min_z:
                min_z = wco[2]

    if min_z == inf or abs(min_z) < 1e-6:
        return  # nothing to do

    offset = -min_z
    for name in obj_names:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            obj.location.z += offset


# ---------------------------------------------------------------------------
# Scene-context merge
# ---------------------------------------------------------------------------

def merge_scene_context(context_blend_path):
    """
    Append objects from context_blend_path into the current scene.
    Returns the set of object names that were already in the scene before merging
    (i.e., the imported object's names — so we can identify them later).
    """
    import bpy

    # Record objects already present (the imported object)
    before = {obj.name for obj in bpy.data.objects}

    # Append all objects from the context scene
    with bpy.data.libraries.load(context_blend_path, link=False) as (data_from, data_to):
        data_to.objects = list(data_from.objects)

    # Link newly added objects into the active scene collection
    after = {obj.name for obj in bpy.data.objects}
    new_obj_names = after - before
    for name in new_obj_names:
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.name not in bpy.context.scene.collection.objects:
            try:
                bpy.context.scene.collection.objects.link(obj)
            except RuntimeError:
                pass  # already linked

    return before  # object names that belong to the imported object


# ---------------------------------------------------------------------------
# Summary / manifest
# ---------------------------------------------------------------------------

def _write_manifest_and_summary(output_dir, rendered, errors, args, golden_dir, all_face_names):
    import bpy

    phash_signal = None
    if golden_dir and rendered:
        try:
            phash_signal = compute_phash_signal(output_dir, golden_dir, list(rendered.keys()))
        except Exception as exc:
            print(f"[object.py] pHash failed: {exc}", file=sys.stderr)

    manifest = {
        "blender_version": bpy.app.version_string,
        "render_sha256": get_script_sha256(__file__),
        "output_dir": output_dir,
        "resolution": args.resolution,
        "faces": list(rendered.keys()),
        "phash_signal": phash_signal,
        "errors": errors,
    }
    mpath = write_manifest(output_dir, manifest)
    print(f"[object.py] Manifest written to {mpath}")

    summary = {
        "blender_version": manifest["blender_version"],
        "render_sha256":   manifest["render_sha256"],
        "output_dir":      manifest["output_dir"],
        "resolution":      manifest["resolution"],
        "faces":           manifest["faces"],
        "phash_signal":    manifest["phash_signal"],
        "errors":          manifest["errors"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return manifest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    input_path      = os.path.abspath(args.input)
    output_dir      = os.path.abspath(args.output_dir)
    golden_dir      = os.path.abspath(args.golden_dir) if args.golden_dir else None
    scene_context   = os.path.abspath(args.scene_context) if args.scene_context else None
    resolution      = args.resolution

    os.makedirs(output_dir, exist_ok=True)
    errors = []

    import bpy

    # --- Import object --------------------------------------------------
    ext = os.path.splitext(input_path)[1].lower()

    if ext == ".blend":
        # open_mainfile replaces the entire scene; no clear_scene needed
        try:
            import_object(input_path)
        except Exception as exc:
            msg = f"Import failed: {exc}"
            print(f"[object.py] ERROR: {msg}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            _write_manifest_and_summary(output_dir, {}, [msg], args, golden_dir, OBJ_FACES)
            sys.exit(1)
    else:
        clear_scene()
        try:
            import_object(input_path)
        except Exception as exc:
            msg = f"Import failed: {exc}"
            print(f"[object.py] ERROR: {msg}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            _write_manifest_and_summary(output_dir, {}, [msg], args, golden_dir, OBJ_FACES)
            sys.exit(1)

    # --- Validate mesh --------------------------------------------------
    bounds = get_scene_bounds()
    if bounds[0] == float("inf"):
        msg = "No mesh objects found after import."
        print(f"[object.py] ERROR: {msg}", file=sys.stderr)
        _write_manifest_and_summary(output_dir, {}, [msg], args, golden_dir, OBJ_FACES)
        sys.exit(1)

    # --- 4-view (unlit) render ------------------------------------------
    try:
        setup_unlit_materials()
    except Exception as exc:
        errors.append(f"Unlit material setup failed: {exc}")

    remove_all_lights()
    configure_render(resolution)

    rendered = render_4views(bounds, output_dir, errors)

    # --- Scene-context render (optional, lit) ---------------------------
    if scene_context:
        # Record which objects belong to the imported model
        imported_obj_names = {obj.name for obj in bpy.data.objects}

        try:
            # Merge terrain context into current scene
            merge_scene_context(scene_context)
        except Exception as exc:
            msg = f"Scene-context merge failed: {exc}"
            print(f"[object.py] ERROR: {msg}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            errors.append(msg)
        else:
            # Ground the imported object (move so min_z == 0)
            try:
                ground_object(imported_obj_names)
            except Exception as exc:
                errors.append(f"Ground object failed: {exc}")

            # Recompute bounds after grounding
            context_bounds = get_scene_bounds(
                obj_filter=lambda obj: obj.name in imported_obj_names
            )
            if context_bounds[0] == float("inf"):
                context_bounds = get_scene_bounds()

            # Context renders use scene lighting (no unlit override)
            rendered.update(render_context_views(context_bounds, output_dir, errors))

    # --- Manifest + stdout summary -------------------------------------
    all_face_names = OBJ_FACES + (CONTEXT_FACES if scene_context else [])
    _write_manifest_and_summary(output_dir, rendered, errors, args, golden_dir, all_face_names)

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
