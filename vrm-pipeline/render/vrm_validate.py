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
* **The Khronos glTF-Validator is mandatory.**  It is resolved from
  ``GLTF_VALIDATOR_PATH`` then ``gltf_validator`` on PATH.  "Could not run it"
  is never "it passed": an unresolvable / non-executable binary, a crash or
  non-zero exit, a timeout, and an unparseable or structurally wrong report all
  become *errors* (cannot determine => block), as does ``numErrors > 0``.  The
  timeout defaults to ``_DEFAULT_VALIDATOR_TIMEOUT_SECS`` and is overridable via
  ``GLTF_VALIDATOR_TIMEOUT_SECS``; an invalid override falls back to the default
  (recorded as a warning) and never disables the timeout.
* **VRM 1.0 meta per spec, as warnings.**  ``VRMC_vrm.schema.json`` requires
  ``meta``; the meta schema requires ``name`` (string, minLength 1),
  ``authors`` (array, minItems 1, of strings minLength 1) and ``licenseUrl``
  (string, empty allowed).  Meta is release/publishing information, not asset
  integrity, so violations are recorded as *warnings* and do not block.  VRM
  0.x has no required meta: not checked.
* Every error/warning message says what is wrong *and* how to fix it.
"""

from __future__ import annotations

import json
import os
import math
import shutil
import struct
import subprocess
import tempfile
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

# Khronos glTF-Validator resolution / timeout.
_VALIDATOR_ENV = "GLTF_VALIDATOR_PATH"
_VALIDATOR_BIN = "gltf_validator"
_VALIDATOR_TIMEOUT_ENV = "GLTF_VALIDATOR_TIMEOUT_SECS"
_DEFAULT_VALIDATOR_TIMEOUT_SECS = 120.0
_VALIDATOR_INSTALL_HINT = (
    "install Khronos gltf_validator "
    "(https://github.com/KhronosGroup/glTF-Validator/releases) on PATH "
    f"or set {_VALIDATOR_ENV} to its executable"
)


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


def _validator_timeout() -> tuple:
    """
    Return ``(timeout_secs, warning_or_None)``.

    ``GLTF_VALIDATOR_TIMEOUT_SECS`` overrides the default when it is a finite
    positive number.  Anything else falls back to the default and yields a
    warning — the timeout is never disabled.
    """
    raw = os.environ.get(_VALIDATOR_TIMEOUT_ENV)
    if raw is None or raw.strip() == "":
        return _DEFAULT_VALIDATOR_TIMEOUT_SECS, None
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_VALIDATOR_TIMEOUT_SECS, (
            f"{_VALIDATOR_TIMEOUT_ENV}={raw!r} is not a positive number of "
            f"seconds; using the default {_DEFAULT_VALIDATOR_TIMEOUT_SECS:g}s "
            f"(fix: set it to e.g. 300, or unset it)"
        )
    return value, None


def _resolve_validator() -> tuple:
    """Return ``(binary_path, None)`` or ``(None, error_message)``."""
    configured = os.environ.get(_VALIDATOR_ENV)
    if configured:
        if not os.path.isfile(configured):
            return None, (
                f"glTF-Validator not found: {_VALIDATOR_ENV}={configured!r} does "
                f"not exist or is not a file (fix: point {_VALIDATOR_ENV} at the "
                f"gltf_validator executable, or unset it and {_VALIDATOR_INSTALL_HINT})"
            )
        if not os.access(configured, os.X_OK):
            return None, (
                f"glTF-Validator not executable: {_VALIDATOR_ENV}={configured!r} "
                f"(fix: chmod +x {configured!r}, or {_VALIDATOR_INSTALL_HINT})"
            )
        return configured, None
    found = shutil.which(_VALIDATOR_BIN)
    if not found:
        return None, (
            f"glTF-Validator is required but was not found ({_VALIDATOR_ENV} "
            f"unset and no {_VALIDATOR_BIN!r} on PATH) (fix: {_VALIDATOR_INSTALL_HINT})"
        )
    return found, None


def run_gltf_validator(path: str | Path) -> dict:
    """
    Run the Khronos glTF-Validator on *path*.  Never raises.

    Returns ``{"issues": dict | None, "error": str | None, "warnings": [str]}``.
    ``error`` is set whenever the validator could not deliver a trustworthy
    verdict (unresolvable/non-executable binary, OSError on launch, timeout,
    non-zero exit, unparseable or structurally wrong report) *or* reported
    ``numErrors > 0``.  ``issues`` is the parsed ``issues`` dict when one was
    obtained.  ``error is None`` is the only "validator passed" outcome.
    """
    result: dict = {"issues": None, "error": None, "warnings": []}
    binary, err = _resolve_validator()
    if err:
        result["error"] = err
        return result

    timeout, timeout_warning = _validator_timeout()
    if timeout_warning:
        result["warnings"].append(timeout_warning)

    try:
        # The Khronos validator picks the loader from the file extension and
        # refuses ``.vrm`` ("unknown file extension", exit 1).  A VRM is a GLB,
        # so hand it a ``.glb``-named symlink in a private temp dir — nothing
        # is written next to the asset.
        with tempfile.TemporaryDirectory(prefix="vrm_validate_") as tmp:
            target = Path(path)
            if target.suffix.lower() not in (".glb", ".gltf"):
                alias = Path(tmp) / "model.glb"
                alias.symlink_to(target.resolve())
                target = alias
            proc = subprocess.run(
                [binary, "-o", str(target)],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        result["error"] = (
            f"glTF-Validator timed out after {timeout:g}s on {path} "
            f"(fix: raise {_VALIDATOR_TIMEOUT_ENV}, e.g. "
            f"{_VALIDATOR_TIMEOUT_ENV}={max(int(timeout * 2), 240)})"
        )
        return result
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result["error"] = (
            f"glTF-Validator could not be run ({binary!r}: {exc}) "
            f"(fix: check that it is a working executable, or {_VALIDATOR_INSTALL_HINT})"
        )
        return result

    stderr_tail = (proc.stderr or "").strip()[-300:]
    issues = None
    parse_problem = None
    try:
        report = json.loads(proc.stdout or "")
    except (json.JSONDecodeError, ValueError, TypeError):
        parse_problem = "its output is not JSON"
    else:
        candidate = report.get("issues") if isinstance(report, dict) else None
        if not isinstance(candidate, dict):
            parse_problem = "its JSON report has no 'issues' object"
        else:
            num = candidate.get("numErrors")
            if not isinstance(num, int) or isinstance(num, bool):
                parse_problem = "its report has no integer 'issues.numErrors'"
            elif num < 0:
                parse_problem = f"its report has a negative 'issues.numErrors' ({num})"
            else:
                issues = candidate
    result["issues"] = issues

    if issues is not None and issues["numErrors"] > 0:
        result["error"] = (
            f"glTF-Validator reported {issues['numErrors']} structural error(s) "
            f"in {path} (fix: run `{_VALIDATOR_BIN} {path}` to see them and "
            f"repair the export/edit step that produced them)"
        )
        return result
    if proc.returncode != 0:
        detail = f"; stderr: {stderr_tail}" if stderr_tail else ""
        result["error"] = (
            f"glTF-Validator exited with status {proc.returncode} on {path}, "
            f"so its verdict cannot be trusted{detail} (fix: run "
            f"`{_VALIDATOR_BIN} -o {path}` by hand; reinstall it if it crashes — "
            f"{_VALIDATOR_INSTALL_HINT})"
        )
        return result
    if parse_problem:
        result["error"] = (
            f"glTF-Validator output could not be read: {parse_problem} "
            f"(fix: make sure {_VALIDATOR_ENV}/PATH points at the Khronos "
            f"gltf_validator, which prints a JSON report with -o; "
            f"{_VALIDATOR_INSTALL_HINT})"
        )
        return result
    return result


def check_meta(gltf: dict, version: str | None) -> list:
    """
    Return *warning* strings for VRM meta spec violations.

    VRM 1.0 (``VRMC_vrm.schema.json`` requires ``meta``;
    ``VRMC_vrm.meta.schema.json`` requires ``name``/``authors``/``licenseUrl``):
    name = string minLength 1; authors = array minItems 1 of strings
    minLength 1; licenseUrl = string (empty allowed).  Meta is publishing
    information, not asset integrity, so these are warnings, never errors.
    VRM 0.x has no required meta, so nothing is checked.
    """
    if version != "1.0":
        return []
    vrmc = (gltf.get("extensions") or {}).get("VRMC_vrm")
    vrmc = vrmc if isinstance(vrmc, dict) else {}
    fix = "set it in the VRM exporter's Meta panel (VRM 1.0 spec, VRMC_vrm.meta)"
    if "meta" not in vrmc:
        return [
            "VRM 1.0 meta is missing (extensions.VRMC_vrm.meta is required with "
            f"name, authors, licenseUrl) (fix: {fix})"
        ]
    meta = vrmc["meta"]
    if not isinstance(meta, dict):
        return [
            f"VRM 1.0 meta must be an object with name, authors, licenseUrl, "
            f"got {type(meta).__name__} (fix: {fix})"
        ]

    problems: list = []
    name = meta.get("name")
    if "name" not in meta:
        problems.append(f"VRM 1.0 meta.name is missing (fix: {fix} — a non-empty avatar name)")
    elif not isinstance(name, str) or name == "":
        problems.append(
            f"VRM 1.0 meta.name must be a non-empty string, got {name!r} "
            f"(fix: {fix} — a non-empty avatar name)"
        )

    authors = meta.get("authors")
    if "authors" not in meta:
        problems.append(f"VRM 1.0 meta.authors is missing (fix: {fix} — at least one author)")
    elif not isinstance(authors, list) or not authors:
        problems.append(
            f"VRM 1.0 meta.authors must be a non-empty array of strings, got "
            f"{authors!r} (fix: {fix} — at least one author)"
        )
    else:
        bad = [a for a in authors if not isinstance(a, str) or a == ""]
        if bad:
            problems.append(
                f"VRM 1.0 meta.authors entries must be non-empty strings, got "
                f"{bad!r} (fix: {fix} — remove or fill in the empty/invalid authors)"
            )

    if "licenseUrl" not in meta:
        problems.append(
            f"VRM 1.0 meta.licenseUrl is missing (fix: {fix} — a license URL "
            f"string; an empty string is allowed)"
        )
    elif not isinstance(meta["licenseUrl"], str):
        problems.append(
            f"VRM 1.0 meta.licenseUrl must be a string, got {meta['licenseUrl']!r} "
            f"(fix: {fix} — a license URL string; an empty string is allowed)"
        )
    return problems


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
    out-of-range bone node, glTF-Validator structural errors, or a
    glTF-Validator that could not be run / read is *not* ok.  VRM 1.0 meta
    spec violations are warnings only.
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
        warnings.extend(check_meta(gltf, version))

    validator = run_gltf_validator(path)
    validator_issues = validator["issues"]
    warnings.extend(validator["warnings"])
    if validator["error"]:
        errors.append(validator["error"])

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
