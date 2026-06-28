"""
VRM semantic output-quality gate.

``render.vrm_utils.assert_valid_glb`` answers "is this a well-formed glTF
binary?".  This module answers the next, higher-level question its docstring
defers to: "is this a *valid VRM avatar*?" — does it carry a VRM extension, are
the required humanoid bones all present and wired to real nodes, and is the rig
in a plausible T-pose?

Design constraints:

* **No new dependencies.**  A ``.vrm`` is a ``.glb`` (binary glTF); the JSON
  chunk is recovered with ``struct`` + ``json`` from the stdlib.  No ``bpy``,
  no ``pygltflib``.
* **Version-aware.**  VRM 0.x stores humanoid bones under
  ``extensions.VRM.humanoid.humanBones`` as a *list* of ``{bone, node}``; VRM
  1.0 stores them under ``extensions.VRMC_vrm.humanoid.humanBones`` as a *named
  object* ``{hips: {node: N}, ...}``.  The two are never the same code path.
* **Block vs. flag.**  Structural breakage (no VRM extension, a missing
  required humanoid bone, a bone pointing at a non-existent node) is an
  *error* and blocks the avatar from entering the ledger.  T-pose deviations
  are *warnings* only: this pipeline's ``height_scale`` edit deliberately bakes
  a non-identity scale into the armature, so a strict T-pose==identity check
  would reject legitimately-edited avatars.  Warnings are recorded in the
  validation report but never raise.
* **Degraded offline mode.**  The external Khronos glTF-Validator is used when
  available (``GLTF_VALIDATOR_PATH`` or ``gltf_validator`` on PATH) but is
  entirely optional — when absent the built-in structural + humanoid checks
  still run, so the gate works in CI/offline.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
from pathlib import Path

# GLB container constants (little-endian).  Header: magic(u32) version(u32)
# length(u32) = 12 bytes, then a sequence of chunks: length(u32) type(u32) data.
# https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#binary-gltf-layout
_GLB_MAGIC = b"glTF"
_GLB_HEADER_SIZE = 12
_CHUNK_HEADER_SIZE = 8
_CHUNK_TYPE_JSON = 0x4E4F534A  # "JSON"

# Quaternion identity / unit scale and the tolerance used to compare against
# them.  VRM/Blender export rounds, so an exact equality check is too brittle.
_IDENTITY_ROTATION = (0.0, 0.0, 0.0, 1.0)
_UNIT_SCALE = (1.0, 1.0, 1.0)
_EPSILON = 1e-4
# A humanoid bone scale at/below this is non-positive, which VRM 1.0 forbids.
_SCALE_MIN = 1e-6

# Humanoid bones every VRM avatar must carry.  Limbs + spine/head are common to
# both spec versions; VRM 0.x additionally requires ``chest`` and ``neck``.
_REQUIRED_LIMB_BONES = (
    "leftUpperArm", "leftLowerArm", "leftHand",
    "rightUpperArm", "rightLowerArm", "rightHand",
    "leftUpperLeg", "leftLowerLeg", "leftFoot",
    "rightUpperLeg", "rightLowerLeg", "rightFoot",
)
_REQUIRED_COMMON_BONES = ("hips", "spine", "head", *_REQUIRED_LIMB_BONES)  # 15
_REQUIRED_BONES_0X = (*_REQUIRED_COMMON_BONES, "chest", "neck")          # 17
_REQUIRED_BONES_10 = _REQUIRED_COMMON_BONES                              # 15


def _read_glb_json(path: str | Path) -> dict:
    """
    Return the parsed glTF JSON chunk of the GLB/VRM at *path*.

    Raises ``RuntimeError`` (pipeline error style) when the file is not a GLB,
    has no JSON chunk, or the JSON chunk is not valid JSON.
    """
    data = Path(path).read_bytes()
    if len(data) < _GLB_HEADER_SIZE or data[:4] != _GLB_MAGIC:
        raise RuntimeError(f"Not a glTF binary (bad magic): {path}")

    declared_len = struct.unpack_from("<I", data, 8)[0]
    # Walk the chunk list and return the first JSON chunk.  We honour the
    # header's declared length so trailing garbage is ignored.
    end = min(declared_len, len(data))
    offset = _GLB_HEADER_SIZE
    while offset + _CHUNK_HEADER_SIZE <= end:
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        body_start = offset + _CHUNK_HEADER_SIZE
        body_end = body_start + chunk_len
        if body_end > len(data):
            raise RuntimeError(f"GLB chunk overruns file: {path}")
        if chunk_type == _CHUNK_TYPE_JSON:
            raw = data[body_start:body_end]
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"GLB JSON chunk is not valid JSON: {path} ({exc})")
        offset = body_end
    raise RuntimeError(f"GLB has no JSON chunk: {path}")


def detect_vrm_version(gltf: dict) -> str | None:
    """Return ``'1.0'`` (VRMC_vrm), ``'0.x'`` (VRM), or ``None`` if neither."""
    ext = gltf.get("extensions") or {}
    if "VRMC_vrm" in ext:
        return "1.0"
    if "VRM" in ext:
        return "0.x"
    return None


def humanoid_bone_nodes(gltf: dict, version: str | None) -> dict:
    """
    Return a ``{boneName: nodeIndex}`` map for the humanoid bones.

    Handles the two spec shapes: VRM 0.x's ``humanBones`` *list* of
    ``{bone, node}`` and VRM 1.0's ``humanBones`` *named object*
    ``{boneName: {node}}``.  Entries without a usable integer node index are
    skipped (their absence is what ``check_humanoid_completeness`` reports).
    """
    ext = gltf.get("extensions") or {}
    result: dict = {}
    if version == "0.x":
        human_bones = (((ext.get("VRM") or {}).get("humanoid") or {}).get("humanBones")) or []
        for item in human_bones:
            if not isinstance(item, dict):
                continue
            name = item.get("bone")
            node = item.get("node")
            if isinstance(name, str) and isinstance(node, int):
                result[name] = node
    elif version == "1.0":
        human_bones = (((ext.get("VRMC_vrm") or {}).get("humanoid") or {}).get("humanBones")) or {}
        if isinstance(human_bones, dict):
            for name, spec in human_bones.items():
                node = spec.get("node") if isinstance(spec, dict) else None
                if isinstance(name, str) and isinstance(node, int):
                    result[name] = node
    return result


def _required_bones(version: str | None) -> tuple:
    return _REQUIRED_BONES_10 if version == "1.0" else _REQUIRED_BONES_0X


def check_humanoid_completeness(gltf: dict, version: str | None) -> list:
    """
    Return a list of *error* strings for missing required humanoid bones and
    for bones whose node index is out of range of ``gltf.nodes``.
    """
    errors: list = []
    bone_map = humanoid_bone_nodes(gltf, version)
    node_count = len(gltf.get("nodes") or [])
    for bone in _required_bones(version):
        if bone not in bone_map:
            errors.append(f"missing required humanoid bone: {bone}")
            continue
        node = bone_map[bone]
        if node < 0 or node >= node_count:
            errors.append(
                f"humanoid bone {bone!r} references node {node} out of range "
                f"(0..{node_count - 1})"
            )
    return errors


def _node_trs(node: dict) -> tuple:
    """Return (rotation, scale) for a node, filling glTF defaults when omitted."""
    rotation = node.get("rotation")
    scale = node.get("scale")
    if not (isinstance(rotation, list) and len(rotation) == 4):
        rotation = list(_IDENTITY_ROTATION)
    if not (isinstance(scale, list) and len(scale) == 3):
        scale = list(_UNIT_SCALE)
    return rotation, scale


def check_tpose(gltf: dict, version: str | None) -> list:
    """
    Return a list of *warning* strings for T-pose deviations.

    VRM 0.x requires every humanoid bone node to have an identity local
    rotation and unit scale; deviations are warned.  VRM 1.0 only constrains
    scale to be positive (raw bone rotations may legitimately be non-zero), so
    only a non-positive scale component is warned.  These are warnings, not
    errors: ``height_scale`` edits bake a non-unit scale on purpose.
    """
    warnings: list = []
    bone_map = humanoid_bone_nodes(gltf, version)
    nodes = gltf.get("nodes") or []
    for bone, node_idx in sorted(bone_map.items()):
        if node_idx < 0 or node_idx >= len(nodes):
            continue  # out-of-range nodes are reported as errors elsewhere
        node = nodes[node_idx]
        if not isinstance(node, dict):
            continue
        rotation, scale = _node_trs(node)
        if version == "1.0":
            if any(c <= _SCALE_MIN for c in scale):
                warnings.append(
                    f"T-pose: humanoid bone {bone!r} has non-positive scale {scale}"
                )
        else:  # 0.x (and unknown versions treated as strict)
            if any(abs(r - i) > _EPSILON for r, i in zip(rotation, _IDENTITY_ROTATION)):
                warnings.append(
                    f"T-pose: humanoid bone {bone!r} has non-identity rotation {rotation}"
                )
            if any(abs(s - 1.0) > _EPSILON for s in scale):
                warnings.append(
                    f"T-pose: humanoid bone {bone!r} has non-unit scale {scale}"
                )
    return warnings


def run_gltf_validator(path: str | Path) -> dict | None:
    """
    Run the Khronos glTF-Validator on *path* and return its ``issues`` dict,
    or ``None`` when the binary cannot be resolved or the run fails.

    The binary is resolved from ``GLTF_VALIDATOR_PATH`` then ``gltf_validator``
    on PATH.  Any failure (missing binary, timeout, non-JSON output) degrades
    to ``None`` rather than raising — the built-in checks still gate.
    """
    binary = os.environ.get("GLTF_VALIDATOR_PATH") or shutil.which("gltf_validator")
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "-o", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        report = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    issues = report.get("issues")
    return issues if isinstance(issues, dict) else None


def validate_vrm(path: str | Path) -> dict:
    """
    Validate the VRM at *path* and return a report dict::

        {
          "path": str,
          "version": "0.x" | "1.0" | None,
          "vrm_present": bool,
          "errors": [str, ...],     # block ledger insert
          "warnings": [str, ...],   # flag only
          "gltf_validator": {"numErrors": int, ...} | None,
          "ok": bool,               # errors is empty
        }

    A VRM with no VRM extension, a missing required humanoid bone, an
    out-of-range bone node, or glTF-Validator structural errors is *not* ok.
    """
    errors: list = []
    warnings: list = []

    try:
        gltf = _read_glb_json(path)
    except RuntimeError as exc:
        return {
            "path": str(path),
            "version": None,
            "vrm_present": False,
            "errors": [str(exc)],
            "warnings": [],
            "gltf_validator": None,
            "ok": False,
        }

    version = detect_vrm_version(gltf)
    vrm_present = version is not None
    if not vrm_present:
        errors.append("file carries no VRM extension (extensions.VRM / VRMC_vrm)")
    else:
        errors.extend(check_humanoid_completeness(gltf, version))
        warnings.extend(check_tpose(gltf, version))

    validator_issues = run_gltf_validator(path)
    if validator_issues is not None:
        num_errors = validator_issues.get("numErrors", 0) or 0
        if num_errors > 0:
            errors.append(f"glTF-Validator reported {num_errors} structural error(s)")

    return {
        "path": str(path),
        "version": version,
        "vrm_present": vrm_present,
        "errors": errors,
        "warnings": warnings,
        "gltf_validator": validator_issues,
        "ok": not errors,
    }


def assert_valid_vrm(path: str | Path, report_path: str | Path | None = None) -> dict:
    """
    Validate *path* and, when *report_path* is given, write the report JSON
    there (alongside the artifact).  Return the report dict on success; raise
    ``RuntimeError`` listing the errors when validation fails (errors present).

    Warnings alone never raise — they are recorded in the report only.
    """
    report = validate_vrm(path)

    if report_path is not None:
        rp = Path(report_path)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not report["ok"]:
        joined = "\n  - ".join(report["errors"])
        raise RuntimeError(
            f"VRM validation failed for {path}:\n  - {joined}"
        )
    return report
