"""
render/vrm.py - VRM character canonical render: body3 + face4 views.

Usage (Blender headless):
    blender --background --python render/vrm.py -- \\
        --vrm <path> --output <dir> [--golden <dir>] [--resolution 768]
"""

import sys
import os
import json
import argparse
import math
import traceback
import importlib

# Allow `from core import *` when run directly as a Blender script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import (
    clear_scene, remove_all_lights, setup_unlit_materials,
    get_scene_bounds, create_ortho_camera, remove_camera,
    configure_render, render_to_file,
    compute_phash_signal, get_script_sha256, write_manifest,
)


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
        description="Canonical VRM render: body3 + face4 views, orthographic, unlit."
    )
    parser.add_argument("--vrm", required=True, help="Path to .vrm file")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--golden", default=None, help="Golden render directory for pHash comparison")
    parser.add_argument("--resolution", type=int, default=768)
    return parser.parse_args(script_args)


# ---------------------------------------------------------------------------
# VRM import + neutralise
# ---------------------------------------------------------------------------

def import_vrm(vrm_path):
    import bpy
    bpy.ops.import_scene.vrm(filepath=vrm_path)


def neutralise_character():
    import bpy
    for obj in bpy.data.objects:
        if obj.type == "MESH" and obj.data.shape_keys:
            for key_block in obj.data.shape_keys.key_blocks:
                if key_block.name != "Basis":
                    key_block.value = 0.0
        if obj.type == "ARMATURE":
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode="POSE")
            bpy.ops.pose.select_all(action="SELECT")
            bpy.ops.pose.rot_clear()
            bpy.ops.pose.loc_clear()
            bpy.ops.pose.scale_clear()
            bpy.ops.object.mode_set(mode="OBJECT")
    _disable_vrm_spring_bones()


def _disable_vrm_spring_bones():
    import bpy
    for obj in bpy.data.objects:
        if obj.type != "ARMATURE":
            continue
        vrm_ext = getattr(obj.data, "vrm_addon_extension", None)
        if vrm_ext is None:
            continue
        sb1 = getattr(vrm_ext, "spring_bone1", None)
        if sb1 is not None:
            for spring in getattr(sb1, "springs", []):
                for joint in getattr(spring, "joints", []):
                    stiffness = getattr(joint, "stiffness", None)
                    if stiffness is not None:
                        joint.stiffness = 0.0
                    drag = getattr(joint, "drag_force", None)
                    if drag is not None:
                        joint.drag_force = 1.0
        sa = getattr(vrm_ext, "vrm0", None)
        if sa is not None:
            sec = getattr(sa, "secondary_animation", None)
            if sec is not None:
                for bone_group in getattr(sec, "bone_groups", []):
                    bone_group.stiffiness = 0.0
                    bone_group.drag_force = 1.0


# ---------------------------------------------------------------------------
# VRM Add-on version detection
# ---------------------------------------------------------------------------

def get_vrm_addon_version():
    """Return the installed VRM add-on version string, or None.

    Handles both the legacy add-on (module ``io_scene_vrm``, version in
    ``bl_info``) and the Blender 4.2+/5.x *extension* install (module
    ``bl_ext.user_default.vrm``, version in ``blender_manifest.toml``).
    ``addon_utils.module_bl_info()`` normalizes both into a dict with
    ``version``, so the extension's manifest version is found too — the plain
    ``bl_info`` read used to miss it and report null despite a working install.
    """
    # Preferred: scan enabled add-ons/extensions and read the normalized info.
    try:
        import bpy
        import addon_utils
        for addon in bpy.context.preferences.addons:
            module = addon.module
            if "vrm" not in module.lower():
                continue
            mod = sys.modules.get(module)
            if mod is None:
                try:
                    mod = importlib.import_module(module)
                except Exception:  # audit-ignore: best-effort version probe; skip unimportable addon
                    continue
            try:
                info = addon_utils.module_bl_info(mod)
            except Exception:  # audit-ignore: fall back to raw bl_info if normalization fails
                info = getattr(mod, "bl_info", None)
            ver = info.get("version") if isinstance(info, dict) else None
            if ver:
                return ".".join(str(v) for v in ver)
    except Exception:  # audit-ignore: version detection is best-effort and must never break a render
        pass
    # Legacy fallback: direct import (older Blender / non-extension installs).
    for mod_name in ("io_scene_vrm", "VRM_Addon_for_Blender", "vrm_addon_for_blender"):
        try:
            mod = importlib.import_module(mod_name)
            ver = getattr(mod, "bl_info", {}).get("version", None)
            if ver:
                return ".".join(str(v) for v in ver)
        except ImportError:
            continue
    return None


# ---------------------------------------------------------------------------
# Bounding box helpers (VRM-specific: character + head)
# ---------------------------------------------------------------------------

def get_character_bounds():
    return get_scene_bounds()


def get_head_bounds():
    bounds = get_character_bounds()
    min_x, min_y, min_z = bounds[0], bounds[1], bounds[2]
    max_x, max_y, max_z = bounds[3], bounds[4], bounds[5]
    height = max_z - min_z
    head_z_min = min_z + height * 0.78
    head_z_max = max_z
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    head_half = (head_z_max - head_z_min) * 0.6
    return (cx - head_half, cy - head_half, head_z_min, cx + head_half, cy + head_half, head_z_max)


# ---------------------------------------------------------------------------
# Camera layout
# ---------------------------------------------------------------------------

BODY_FACES = ["body_front", "body_side", "body_back"]
FACE_FACES = ["face_front", "face_L", "face_R", "face_34"]
ALL_FACES = BODY_FACES + FACE_FACES


def _body_camera_params(bounds):
    min_x, min_y, min_z = bounds[0], bounds[1], bounds[2]
    max_x, max_y, max_z = bounds[3], bounds[4], bounds[5]
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    cz = (min_z + max_z) / 2.0
    width = max_x - min_x
    height_char = max_z - min_z
    depth = max_y - min_y
    ortho_scale = max(width, height_char) * 1.1
    dist = max(depth, width, height_char) * 2.0
    return {
        "body_front": ((cx, cy - dist, cz), (math.radians(90), 0.0, 0.0), ortho_scale),
        "body_side":  ((cx + dist, cy, cz), (math.radians(90), 0.0, math.radians(90)), ortho_scale),
        "body_back":  ((cx, cy + dist, cz), (math.radians(90), 0.0, math.radians(180)), ortho_scale),
    }


def _face_camera_params(head_bounds):
    min_x, min_y, min_z = head_bounds[0], head_bounds[1], head_bounds[2]
    max_x, max_y, max_z = head_bounds[3], head_bounds[4], head_bounds[5]
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    cz = (min_z + max_z) / 2.0
    width = max_x - min_x
    height_head = max_z - min_z
    depth = max_y - min_y
    ortho_scale = max(width, height_head) * 1.2
    dist = max(depth, width, height_head) * 2.0
    diag_dist = dist / math.sqrt(2.0)
    return {
        "face_front": ((cx, cy - dist, cz),                  (math.radians(90), 0.0, 0.0),           ortho_scale),
        "face_L":     ((cx - dist, cy, cz),                  (math.radians(90), 0.0, math.radians(-90)), ortho_scale),
        "face_R":     ((cx + dist, cy, cz),                  (math.radians(90), 0.0, math.radians(90)),  ortho_scale),
        "face_34":    ((cx - diag_dist, cy - diag_dist, cz), (math.radians(90), 0.0, math.radians(-45)), ortho_scale),
    }


# ---------------------------------------------------------------------------
# Render loop helper
# ---------------------------------------------------------------------------

def _render_faces(face_names, cam_params, output_dir, errors):
    import bpy
    rendered = {}
    for face_name in face_names:
        loc, rot, ortho_scale = cam_params[face_name]
        cam = create_ortho_camera(face_name, loc, rot, ortho_scale)
        bpy.context.scene.camera = cam
        out_path = os.path.join(output_dir, face_name + ".webp")
        try:
            render_to_file(out_path)
            rendered[face_name] = out_path
            print(f"[vrm.py] Rendered {face_name} -> {out_path}")
        except Exception as exc:
            msg = f"Render failed for {face_name}: {exc}"
            print(f"[vrm.py] ERROR: {msg}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            errors.append(msg)
        finally:
            remove_camera(cam)
    return rendered


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    vrm_path = os.path.abspath(args.vrm)
    output_dir = os.path.abspath(args.output)
    golden_dir = os.path.abspath(args.golden) if args.golden else None
    resolution = args.resolution

    os.makedirs(output_dir, exist_ok=True)
    errors = []

    import bpy

    clear_scene()

    try:
        import_vrm(vrm_path)
    except Exception as exc:
        msg = f"VRM import failed: {exc}"
        print(f"[vrm.py] ERROR: {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        _write_manifest_and_exit(output_dir, {}, [msg], args, golden_dir, code=1)

    try:
        neutralise_character()
    except Exception as exc:
        errors.append(f"Neutralise failed: {exc}")

    try:
        setup_unlit_materials()
    except Exception as exc:
        errors.append(f"Material setup failed: {exc}")

    remove_all_lights()

    body_bounds = get_character_bounds()
    if body_bounds[0] == float("inf"):
        _write_manifest_and_exit(output_dir, {}, ["No mesh objects found after VRM import."], args, golden_dir, code=1)

    configure_render(resolution)

    rendered = {}
    rendered.update(_render_faces(BODY_FACES, _body_camera_params(body_bounds), output_dir, errors))
    head_bounds = get_head_bounds()
    rendered.update(_render_faces(FACE_FACES, _face_camera_params(head_bounds), output_dir, errors))

    _write_manifest_and_exit(output_dir, rendered, errors, args, golden_dir, code=0)


def _write_manifest_and_exit(output_dir, rendered, errors, args, golden_dir, code=0):
    import bpy

    rendered_faces = list(rendered.keys())
    phash_signal = None
    if golden_dir and rendered_faces:
        try:
            phash_signal = compute_phash_signal(output_dir, golden_dir, rendered_faces)
        except Exception as exc:
            print(f"[vrm.py] pHash failed: {exc}", file=sys.stderr)

    manifest = {
        "blender_version": bpy.app.version_string,
        "vrm_addon_version": get_vrm_addon_version(),
        "render_sha256": get_script_sha256(__file__),
        "output_dir": output_dir,
        "resolution": args.resolution,
        "faces": rendered_faces,
        "phash_signal": phash_signal,
        "errors": errors,
    }
    mpath = write_manifest(output_dir, manifest)
    print(f"[vrm.py] Manifest written to {mpath}")

    summary = {k: manifest[k] for k in
               ("blender_version", "vrm_addon_version", "render_sha256",
                "output_dir", "faces", "phash_signal")}
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if code != 0:
        sys.exit(code)


if __name__ == "__main__":
    main()
