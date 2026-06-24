"""
render.py - Canonical VRM render script for body3 + face4 views.

Usage (Blender headless):
    blender --background --python render.py -- \\
        --vrm <path> --output <dir> [--golden <dir>] [--resolution 768]
"""

import sys
import os
import json
import argparse
import hashlib
import math
import traceback


# ---------------------------------------------------------------------------
# Argument parsing (Blender passes everything after "--" to the script)
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
    parser.add_argument(
        "--golden", default=None, help="Golden render directory for pHash comparison"
    )
    parser.add_argument(
        "--resolution", type=int, default=768, help="Render resolution in pixels (square)"
    )
    return parser.parse_args(script_args)


# ---------------------------------------------------------------------------
# pHash (aHash) implementation using only bpy - no external libs
# ---------------------------------------------------------------------------

def _pixels_to_grayscale(pixels, width, height):
    """Convert flat RGBA pixel list to 2D grayscale list."""
    gray = []
    for y in range(height):
        row = []
        for x in range(width):
            idx = (y * width + x) * 4
            r, g, b = pixels[idx], pixels[idx + 1], pixels[idx + 2]
            luma = 0.299 * r + 0.587 * g + 0.114 * b
            row.append(luma)
        gray.append(row)
    return gray


def compute_ahash_from_file(filepath):
    """
    Compute a 64-bit average hash (aHash) from an image file.
    Uses bpy to load the image, scales to 8x8, then thresholds against mean.
    Returns an integer (the hash).
    """
    import bpy

    img = bpy.data.images.load(filepath, check_existing=False)
    try:
        img.scale(8, 8)
        pixels = list(img.pixels)
        width, height = img.size
        gray = _pixels_to_grayscale(pixels, width, height)

        # Flatten to 1D
        flat = [gray[y][x] for y in range(height) for x in range(width)]
        mean = sum(flat) / len(flat)

        bits = 0
        for i, val in enumerate(flat):
            if val >= mean:
                bits |= 1 << i
        return bits
    finally:
        bpy.data.images.remove(img)


def hamming_distance(hash_a, hash_b):
    """Compute Hamming distance between two 64-bit integers."""
    xor = hash_a ^ hash_b
    count = 0
    while xor:
        count += xor & 1
        xor >>= 1
    return count


def compute_phash_signal(output_dir, golden_dir, faces):
    """
    Compare rendered faces against golden renders.
    Returns dict: face_name -> hamming_distance (or None on error).
    """
    result = {}
    for face in faces:
        filename = face + ".webp"
        rendered_path = os.path.join(output_dir, filename)
        golden_path = os.path.join(golden_dir, filename)

        if not os.path.isfile(rendered_path) or not os.path.isfile(golden_path):
            result[face] = None
            continue
        try:
            h_render = compute_ahash_from_file(rendered_path)
            h_golden = compute_ahash_from_file(golden_path)
            result[face] = hamming_distance(h_render, h_golden)
        except Exception as exc:
            print(f"[render.py] pHash error for {face}: {exc}", file=sys.stderr)
            result[face] = None
    return result


# ---------------------------------------------------------------------------
# Scene utilities
# ---------------------------------------------------------------------------

def clear_scene():
    """Remove all objects, lights, and cameras from the current scene."""
    import bpy

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    # Remove orphan data
    for block in list(bpy.data.meshes):
        if block.users == 0:
            bpy.data.meshes.remove(block)
    for block in list(bpy.data.lights):
        if block.users == 0:
            bpy.data.lights.remove(block)
    for block in list(bpy.data.cameras):
        if block.users == 0:
            bpy.data.cameras.remove(block)


def import_vrm(vrm_path):
    """Import a VRM file using the VRM Add-on."""
    import bpy

    bpy.ops.import_scene.vrm(filepath=vrm_path)


# ---------------------------------------------------------------------------
# Neutralise pose / shape keys / spring bones
# ---------------------------------------------------------------------------

def neutralise_character():
    """
    Reset all shape keys to 0, pose bones to rest, and disable spring bones.
    """
    import bpy

    for obj in bpy.data.objects:
        # Reset shape keys (blend shapes)
        if obj.type == "MESH" and obj.data.shape_keys:
            for key_block in obj.data.shape_keys.key_blocks:
                if key_block.name != "Basis":
                    key_block.value = 0.0

        # Reset armature pose to A-pose (clear all bone transforms)
        if obj.type == "ARMATURE":
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode="POSE")
            bpy.ops.pose.select_all(action="SELECT")
            bpy.ops.pose.rot_clear()
            bpy.ops.pose.loc_clear()
            bpy.ops.pose.scale_clear()
            bpy.ops.object.mode_set(mode="OBJECT")

    # Disable VRM spring bone physics
    _disable_vrm_spring_bones()


def _disable_vrm_spring_bones():
    """Attempt to disable VRM spring bone / physics simulation."""
    import bpy

    for obj in bpy.data.objects:
        if obj.type != "ARMATURE":
            continue

        # VRM 1.0 Add-on exposes spring_bone settings on the armature data
        vrm_ext = getattr(obj.data, "vrm_addon_extension", None)
        if vrm_ext is None:
            continue

        # VRM 1.0: spring_bone1
        sb1 = getattr(vrm_ext, "spring_bone1", None)
        if sb1 is not None:
            for spring in getattr(sb1, "springs", []):
                for joint in getattr(spring, "joints", []):
                    stiffness = getattr(joint, "stiffness", None)
                    if stiffness is not None:
                        joint.stiffness = 0.0
                    drag = getattr(joint, "drag_force", None)
                    if drag is not None:
                        joint.drag_force = 1.0  # full damping stops motion

        # VRM 0.x: secondary_animation
        sa = getattr(vrm_ext, "vrm0", None)
        if sa is not None:
            sec = getattr(sa, "secondary_animation", None)
            if sec is not None:
                for bone_group in getattr(sec, "bone_groups", []):
                    bone_group.stiffiness = 0.0  # sic - VRM0 typo preserved
                    bone_group.drag_force = 1.0


# ---------------------------------------------------------------------------
# Unlit material setup
# ---------------------------------------------------------------------------

def setup_unlit_materials():
    """
    Convert all materials to emission-based unlit rendering.
    Connects each material's base colour texture directly to the Emission socket.
    """
    import bpy

    for mat in bpy.data.materials:
        # Capture base colour image before clearing nodes
        base_color_image = _find_base_color_image(mat)

        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        # Clear existing nodes
        nodes.clear()

        # Output node
        out_node = nodes.new("ShaderNodeOutputMaterial")
        out_node.location = (400, 0)

        # Emission shader
        emit_node = nodes.new("ShaderNodeEmission")
        emit_node.location = (200, 0)
        emit_node.inputs["Strength"].default_value = 1.0

        if base_color_image is not None:
            tex_node = nodes.new("ShaderNodeTexImage")
            tex_node.location = (-100, 0)
            tex_node.image = base_color_image
            links.new(tex_node.outputs["Color"], emit_node.inputs["Color"])
        else:
            # Fall back to a white emission
            emit_node.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)

        links.new(emit_node.outputs["Emission"], out_node.inputs["Surface"])

        # Make sure alpha is handled
        mat.blend_method = "BLEND"
        mat.shadow_method = "NONE"
        mat.use_backface_culling = False


def _find_base_color_image(mat):
    """
    Try to find the base colour image from a material before nodes are cleared.
    This function is called *before* clearing nodes so we inspect existing nodes.

    Returns a bpy.types.Image or None.
    """
    if not mat.use_nodes or mat.node_tree is None:
        return None

    candidate_socket_names = {
        "Base Color", "baseColorTexture", "Lit Color, Alpha",
        "MainTexture", "Main Texture",
    }

    nodes = mat.node_tree.nodes

    # 1. Look for a texture node connected to a "Base Color"-like socket
    for node in nodes:
        if node.type != "TEX_IMAGE":
            continue
        for output in node.outputs:
            for link in output.links:
                if link.to_socket.name in candidate_socket_names:
                    return node.image

    # 2. Fallback: return the first TEX_IMAGE node with an image assigned
    for node in nodes:
        if node.type == "TEX_IMAGE" and node.image is not None:
            return node.image

    return None


# ---------------------------------------------------------------------------
# Bounding box helpers
# ---------------------------------------------------------------------------

def get_character_bounds():
    """
    Return (min_x, max_x, min_y, max_y, min_z, max_z) world-space bounding box
    for all MESH objects in the scene.
    Returns tuple of 6 floats: (min_x, min_y, min_z, max_x, max_y, max_z).
    """
    import bpy
    from mathutils import Vector

    inf = float("inf")
    min_v = [inf, inf, inf]
    max_v = [-inf, -inf, -inf]

    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        for corner in obj.bound_box:
            world_co = obj.matrix_world @ Vector(corner)
            for i in range(3):
                if world_co[i] < min_v[i]:
                    min_v[i] = world_co[i]
                if world_co[i] > max_v[i]:
                    max_v[i] = world_co[i]

    return (min_v[0], min_v[1], min_v[2], max_v[0], max_v[1], max_v[2])


def get_head_bounds():
    """
    Return bounding box for the head region only.
    Heuristic: top ~22% of character height in Z, centred on character.
    Returns tuple: (min_x, min_y, min_z, max_x, max_y, max_z).
    """
    bounds = get_character_bounds()
    min_x, min_y, min_z = bounds[0], bounds[1], bounds[2]
    max_x, max_y, max_z = bounds[3], bounds[4], bounds[5]

    height = max_z - min_z
    # Head is roughly the top 20% of the body
    head_z_min = min_z + height * 0.78
    head_z_max = max_z

    # Centre X/Y on character
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    head_half = (head_z_max - head_z_min) * 0.6  # add some margin

    return (
        cx - head_half, cy - head_half, head_z_min,
        cx + head_half, cy + head_half, head_z_max,
    )


# ---------------------------------------------------------------------------
# Camera setup
# ---------------------------------------------------------------------------

def create_ortho_camera(name, location, rotation_euler, ortho_scale):
    """Create an orthographic camera object and return it."""
    import bpy
    from mathutils import Euler

    cam_data = bpy.data.cameras.new(name=name)
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = ortho_scale
    cam_data.clip_start = 0.01
    cam_data.clip_end = 1000.0

    cam_obj = bpy.data.objects.new(name=name, object_data=cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)

    cam_obj.location = location
    cam_obj.rotation_euler = Euler(rotation_euler, "XYZ")

    return cam_obj


# ---------------------------------------------------------------------------
# Render configuration
# ---------------------------------------------------------------------------

def configure_render(resolution, output_dir):
    """Configure Blender render settings for WebP output."""
    import bpy

    scene = bpy.context.scene
    render = scene.render

    render.engine = "CYCLES"
    render.resolution_x = resolution
    render.resolution_y = resolution
    render.resolution_percentage = 100
    render.image_settings.file_format = "WEBP"
    render.image_settings.quality = 90
    render.film_transparent = True

    # Use Standard view transform (no Filmic tone mapping)
    try:
        scene.display_settings.display_device = "sRGB"
    except Exception:
        pass
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
    except Exception:
        pass

    # Fast render settings (single sample, no denoising)
    if render.engine == "CYCLES":
        scene.cycles.samples = 1
        scene.cycles.use_denoising = False


def render_to_file(output_path):
    """Render the current frame to the given output path."""
    import bpy

    bpy.context.scene.render.filepath = output_path
    bpy.ops.render.render(write_still=True)


# ---------------------------------------------------------------------------
# Face definitions
# ---------------------------------------------------------------------------

BODY_FACES = ["body_front", "body_side", "body_back"]
FACE_FACES = ["face_front", "face_L", "face_R", "face_34"]
ALL_FACES = BODY_FACES + FACE_FACES


def _body_camera_params(bounds):
    """
    Return camera (location, rotation_euler, ortho_scale) for each body face.
    bounds = (min_x, min_y, min_z, max_x, max_y, max_z)
    """
    min_x, min_y, min_z = bounds[0], bounds[1], bounds[2]
    max_x, max_y, max_z = bounds[3], bounds[4], bounds[5]

    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    cz = (min_z + max_z) / 2.0

    width = max_x - min_x
    height_char = max_z - min_z
    depth = max_y - min_y

    # ortho_scale fits the whole body with 10% margin
    ortho_scale = max(width, height_char) * 1.1
    dist = max(depth, width, height_char) * 2.0

    return {
        "body_front": (
            (cx, cy - dist, cz),
            (math.radians(90), 0.0, 0.0),
            ortho_scale,
        ),
        "body_side": (
            (cx + dist, cy, cz),
            (math.radians(90), 0.0, math.radians(90)),
            ortho_scale,
        ),
        "body_back": (
            (cx, cy + dist, cz),
            (math.radians(90), 0.0, math.radians(180)),
            ortho_scale,
        ),
    }


def _face_camera_params(head_bounds):
    """
    Return camera (location, rotation_euler, ortho_scale) for each face view.
    head_bounds = (min_x, min_y, min_z, max_x, max_y, max_z)
    """
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

    # 45 degree diagonal for face_34
    diag_dist = dist / math.sqrt(2.0)

    return {
        "face_front": (
            (cx, cy - dist, cz),
            (math.radians(90), 0.0, 0.0),
            ortho_scale,
        ),
        "face_L": (
            (cx - dist, cy, cz),
            (math.radians(90), 0.0, math.radians(-90)),
            ortho_scale,
        ),
        "face_R": (
            (cx + dist, cy, cz),
            (math.radians(90), 0.0, math.radians(90)),
            ortho_scale,
        ),
        "face_34": (
            (cx - diag_dist, cy - diag_dist, cz),
            (math.radians(90), 0.0, math.radians(-45)),
            ortho_scale,
        ),
    }


# ---------------------------------------------------------------------------
# VRM Add-on version detection
# ---------------------------------------------------------------------------

def get_vrm_addon_version():
    """Return the VRM Add-on version as a string, or None if not found."""
    import importlib
    import sys as _sys

    # Common module names used by different VRM add-on packages
    candidate_modules = [
        "io_scene_vrm",
        "VRM_Addon_for_Blender",
        "vrm_addon_for_blender",
    ]

    for mod_name in candidate_modules:
        try:
            mod = importlib.import_module(mod_name)
            ver = getattr(mod, "bl_info", {}).get("version", None)
            if ver:
                return ".".join(str(v) for v in ver)
        except ImportError:
            continue

    # Try bpy.context.preferences.addons
    try:
        import bpy
        for addon in bpy.context.preferences.addons:
            if "vrm" in addon.module.lower():
                mod = _sys.modules.get(addon.module)
                if mod:
                    ver = getattr(mod, "bl_info", {}).get("version", None)
                    if ver:
                        return ".".join(str(v) for v in ver)
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Script self-hash
# ---------------------------------------------------------------------------

def get_script_sha256():
    """Return the SHA256 hex digest of this script file."""
    script_path = os.path.abspath(__file__)
    sha = hashlib.sha256()
    with open(script_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


# ---------------------------------------------------------------------------
# Main render orchestration
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    vrm_path = os.path.abspath(args.vrm)
    output_dir = os.path.abspath(args.output)
    golden_dir = os.path.abspath(args.golden) if args.golden else None
    resolution = args.resolution

    os.makedirs(output_dir, exist_ok=True)

    # Manifest accumulator
    manifest_faces = {face: None for face in ALL_FACES}
    errors = []

    import bpy

    # ------------------------------------------------------------------
    # 1. Clear default scene
    # ------------------------------------------------------------------
    clear_scene()

    # ------------------------------------------------------------------
    # 2. Import VRM
    # ------------------------------------------------------------------
    try:
        import_vrm(vrm_path)
    except Exception as exc:
        msg = f"VRM import failed: {exc}"
        print(f"[render.py] ERROR: {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        errors.append(msg)
        _write_manifest(output_dir, manifest_faces, errors, args, golden_dir)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 3. Neutralise
    # ------------------------------------------------------------------
    try:
        neutralise_character()
    except Exception as exc:
        msg = f"Neutralise failed: {exc}"
        print(f"[render.py] WARNING: {msg}", file=sys.stderr)
        errors.append(msg)

    # ------------------------------------------------------------------
    # 4. Unlit materials (capture base colour BEFORE clearing nodes)
    # ------------------------------------------------------------------
    try:
        setup_unlit_materials()
    except Exception as exc:
        msg = f"Material setup failed: {exc}"
        print(f"[render.py] WARNING: {msg}", file=sys.stderr)
        errors.append(msg)

    # ------------------------------------------------------------------
    # 5. Remove all existing lights
    # ------------------------------------------------------------------
    for obj in list(bpy.data.objects):
        if obj.type == "LIGHT":
            bpy.data.objects.remove(obj, do_unlink=True)

    # ------------------------------------------------------------------
    # 6. Compute bounding boxes
    # ------------------------------------------------------------------
    body_bounds = get_character_bounds()
    head_bounds = get_head_bounds()

    # Guard: if bounds are still infinite (no mesh objects), abort
    if body_bounds[0] == float("inf"):
        msg = "No mesh objects found after VRM import."
        print(f"[render.py] ERROR: {msg}", file=sys.stderr)
        errors.append(msg)
        _write_manifest(output_dir, manifest_faces, errors, args, golden_dir)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 7. Configure base render settings
    # ------------------------------------------------------------------
    configure_render(resolution, output_dir)

    # ------------------------------------------------------------------
    # 8. Render body faces
    # ------------------------------------------------------------------
    body_params = _body_camera_params(body_bounds)
    for face_name in BODY_FACES:
        loc, rot, ortho_scale = body_params[face_name]
        cam = create_ortho_camera(face_name, loc, rot, ortho_scale)
        bpy.context.scene.camera = cam
        out_path = os.path.join(output_dir, face_name + ".webp")
        try:
            render_to_file(out_path)
            manifest_faces[face_name] = out_path
            print(f"[render.py] Rendered {face_name} -> {out_path}")
        except Exception as exc:
            msg = f"Render failed for {face_name}: {exc}"
            print(f"[render.py] ERROR: {msg}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            errors.append(msg)
        finally:
            cam_data_name = cam.data.name
            bpy.data.objects.remove(cam, do_unlink=True)
            if cam_data_name in bpy.data.cameras:
                bpy.data.cameras.remove(bpy.data.cameras[cam_data_name], do_unlink=True)

    # ------------------------------------------------------------------
    # 9. Render face views
    # ------------------------------------------------------------------
    face_params = _face_camera_params(head_bounds)
    for face_name in FACE_FACES:
        loc, rot, ortho_scale = face_params[face_name]
        cam = create_ortho_camera(face_name, loc, rot, ortho_scale)
        bpy.context.scene.camera = cam
        out_path = os.path.join(output_dir, face_name + ".webp")
        try:
            render_to_file(out_path)
            manifest_faces[face_name] = out_path
            print(f"[render.py] Rendered {face_name} -> {out_path}")
        except Exception as exc:
            msg = f"Render failed for {face_name}: {exc}"
            print(f"[render.py] ERROR: {msg}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            errors.append(msg)
        finally:
            cam_data_name = cam.data.name
            bpy.data.objects.remove(cam, do_unlink=True)
            if cam_data_name in bpy.data.cameras:
                bpy.data.cameras.remove(bpy.data.cameras[cam_data_name], do_unlink=True)

    # ------------------------------------------------------------------
    # 10. Write manifest + print JSON summary to stdout
    # ------------------------------------------------------------------
    _write_manifest(output_dir, manifest_faces, errors, args, golden_dir)


def _write_manifest(output_dir, manifest_faces, errors, args, golden_dir):
    """Write manifest.json and print a JSON summary to stdout."""
    import bpy

    rendered_faces = [k for k, v in manifest_faces.items() if v is not None]

    # pHash signal
    phash_signal = None
    if golden_dir and rendered_faces:
        try:
            phash_signal = compute_phash_signal(output_dir, golden_dir, rendered_faces)
        except Exception as exc:
            print(f"[render.py] pHash computation failed: {exc}", file=sys.stderr)

    manifest = {
        "blender_version": bpy.app.version_string,
        "vrm_addon_version": get_vrm_addon_version(),
        "render_sha256": get_script_sha256(),
        "output_dir": output_dir,
        "resolution": args.resolution,
        "faces": rendered_faces,
        "phash_signal": phash_signal,
        "errors": errors,
    }

    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"[render.py] Manifest written to {manifest_path}")

    # Print JSON summary to stdout for caller consumption
    summary = {
        "blender_version": manifest["blender_version"],
        "vrm_addon_version": manifest["vrm_addon_version"],
        "render_sha256": manifest["render_sha256"],
        "output_dir": output_dir,
        "faces": rendered_faces,
        "phash_signal": phash_signal,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
