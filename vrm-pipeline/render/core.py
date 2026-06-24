"""
render/core.py - Shared headless Blender rendering utilities.

Imported by render/vrm.py and render/object.py.
All functions that touch bpy import it locally so this file passes
py_compile without Blender present.
"""

import sys
import os
import json
import hashlib
import math


# ---------------------------------------------------------------------------
# aHash (average hash) - pure bpy, no external libs
# ---------------------------------------------------------------------------

def _pixels_to_grayscale(pixels, width, height):
    gray = []
    for y in range(height):
        row = []
        for x in range(width):
            idx = (y * width + x) * 4
            r, g, b = pixels[idx], pixels[idx + 1], pixels[idx + 2]
            row.append(0.299 * r + 0.587 * g + 0.114 * b)
        gray.append(row)
    return gray


def compute_ahash_from_file(filepath):
    import bpy
    img = bpy.data.images.load(filepath, check_existing=False)
    try:
        img.scale(8, 8)
        pixels = list(img.pixels)
        width, height = img.size
        gray = _pixels_to_grayscale(pixels, width, height)
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
    xor = hash_a ^ hash_b
    count = 0
    while xor:
        count += xor & 1
        xor >>= 1
    return count


def compute_phash_signal(output_dir, golden_dir, face_names):
    result = {}
    for face in face_names:
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
            print(f"[core] pHash error for {face}: {exc}", file=sys.stderr)
            result[face] = None
    return result


# ---------------------------------------------------------------------------
# Scene utilities
# ---------------------------------------------------------------------------

def clear_scene():
    import bpy
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in list(bpy.data.meshes):
        if block.users == 0:
            bpy.data.meshes.remove(block)
    for block in list(bpy.data.lights):
        if block.users == 0:
            bpy.data.lights.remove(block)
    for block in list(bpy.data.cameras):
        if block.users == 0:
            bpy.data.cameras.remove(block)


def remove_all_lights():
    import bpy
    for obj in list(bpy.data.objects):
        if obj.type == "LIGHT":
            bpy.data.objects.remove(obj, do_unlink=True)


# ---------------------------------------------------------------------------
# Unlit emission materials
# ---------------------------------------------------------------------------

def _find_base_color_image(mat):
    if not mat.use_nodes or mat.node_tree is None:
        return None
    candidate_socket_names = {
        "Base Color", "baseColorTexture", "Lit Color, Alpha",
        "MainTexture", "Main Texture",
    }
    nodes = mat.node_tree.nodes
    for node in nodes:
        if node.type != "TEX_IMAGE":
            continue
        for output in node.outputs:
            for link in output.links:
                if link.to_socket.name in candidate_socket_names:
                    return node.image
    for node in nodes:
        if node.type == "TEX_IMAGE" and node.image is not None:
            return node.image
    return None


def setup_unlit_materials():
    import bpy
    for mat in bpy.data.materials:
        base_color_image = _find_base_color_image(mat)
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.clear()
        out_node = nodes.new("ShaderNodeOutputMaterial")
        out_node.location = (400, 0)
        emit_node = nodes.new("ShaderNodeEmission")
        emit_node.location = (200, 0)
        emit_node.inputs["Strength"].default_value = 1.0
        if base_color_image is not None:
            tex_node = nodes.new("ShaderNodeTexImage")
            tex_node.location = (-100, 0)
            tex_node.image = base_color_image
            links.new(tex_node.outputs["Color"], emit_node.inputs["Color"])
        else:
            emit_node.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        links.new(emit_node.outputs["Emission"], out_node.inputs["Surface"])
        mat.blend_method = "BLEND"
        mat.shadow_method = "NONE"
        mat.use_backface_culling = False


# ---------------------------------------------------------------------------
# Bounding box
# ---------------------------------------------------------------------------

def get_scene_bounds(obj_filter=None):
    """
    Return world-space bounding box (min_x, min_y, min_z, max_x, max_y, max_z)
    for all MESH objects, optionally filtered by obj_filter(obj) -> bool.
    """
    import bpy
    from mathutils import Vector

    inf = float("inf")
    mn = [inf, inf, inf]
    mx = [-inf, -inf, -inf]

    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if obj_filter is not None and not obj_filter(obj):
            continue
        for corner in obj.bound_box:
            wco = obj.matrix_world @ Vector(corner)
            for i in range(3):
                if wco[i] < mn[i]:
                    mn[i] = wco[i]
                if wco[i] > mx[i]:
                    mx[i] = wco[i]

    return (mn[0], mn[1], mn[2], mx[0], mx[1], mx[2])


# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------

def create_ortho_camera(name, location, rotation_euler, ortho_scale):
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


def create_persp_camera(name, location, rotation_euler, fov_degrees=50.0):
    import bpy
    from mathutils import Euler

    cam_data = bpy.data.cameras.new(name=name)
    cam_data.type = "PERSP"
    cam_data.angle = math.radians(fov_degrees)
    cam_data.clip_start = 0.01
    cam_data.clip_end = 1000.0

    cam_obj = bpy.data.objects.new(name=name, object_data=cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)
    cam_obj.location = location
    cam_obj.rotation_euler = Euler(rotation_euler, "XYZ")
    return cam_obj


def remove_camera(cam_obj):
    import bpy
    cam_data_name = cam_obj.data.name
    bpy.data.objects.remove(cam_obj, do_unlink=True)
    if cam_data_name in bpy.data.cameras:
        bpy.data.cameras.remove(bpy.data.cameras[cam_data_name], do_unlink=True)


# ---------------------------------------------------------------------------
# Render configuration + execution
# ---------------------------------------------------------------------------

def configure_render(resolution):
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
    try:
        scene.display_settings.display_device = "sRGB"
    except Exception:
        pass
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
    except Exception:
        pass
    if render.engine == "CYCLES":
        scene.cycles.samples = 1
        scene.cycles.use_denoising = False


def render_to_file(output_path):
    import bpy
    bpy.context.scene.render.filepath = output_path
    bpy.ops.render.render(write_still=True)


# ---------------------------------------------------------------------------
# Script self-hash
# ---------------------------------------------------------------------------

def get_script_sha256(script_path=None):
    path = os.path.abspath(script_path or __file__)
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


# ---------------------------------------------------------------------------
# Manifest writer
# ---------------------------------------------------------------------------

def write_manifest(output_dir, data):
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return manifest_path
