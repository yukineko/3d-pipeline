"""
render/generate_body.py - Generate a parametric human body VRM via Blender
                          headless + MPFB2 (MakeHuman Plugin for Blender) +
                          VRM_Addon_for_Blender.

This is the body/morph *generation* analogue of ``render/vrm_edit.py`` (which
*edits* an existing VRM). Where vrm_edit imports a VRM and tweaks it, this module
builds a brand-new human base mesh from MakeHuman macro modifiers, rigs it with
MPFB2's ``game_engine`` rig, assigns the VRM humanoid bone table, bakes the live
morphs into geometry, and exports a .vrm.

Python-side wrapper
-------------------
    from render.generate_body import generate_body
    from render.body_params import resolve_body_morphs
    out = generate_body(resolve_body_morphs("tall athletic woman"), "/out/char.vrm")

Blender script mode
-------------------
When run directly by Blender (``--python render/generate_body.py -- ...``) the
module executes as a bpy script: it enables addons, creates a human, applies the
morphs, attaches the rig, assigns the VRM humanoid, bakes & exports VRM, and
exits with code 0 on success or 1 on failure.

Runtime requirements
--------------------
  * Blender (4.2 LTS recommended), with
  * MPFB2 (MakeHuman Plugin for Blender) addon enabled, and
  * VRM_Addon_for_Blender enabled.
None of these exist in a bare Python environment; ALL ``bpy`` / ``mpfb`` imports
are therefore performed INSIDE the functions that need them, so this module
imports cleanly without Blender (mirroring vrm_edit.py).

Environment / arguments
------------------------
  BLENDER_PATH           Override Blender binary location (default: "blender").
  VRM_SUBPROCESS_TIMEOUT Seconds before the Blender subprocess is killed (600).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from render.body_params import BODY_MORPH_KEYS
from render.vrm_bone_map import (
    MPFB_TO_VRM_HUMANOID,
    missing_required_slots,
)

# ---------------------------------------------------------------------------
# Pure-data: canonical morph key -> MPFB2 MakeHuman target/modifier string.
# ---------------------------------------------------------------------------
#
# IMPORTANT: these exact MPFB2 target strings vary by MPFB2 version and MUST be
# validated at runtime against the installed MPFB2 target namespace
# (TargetService). They are documented best-effort defaults for MPFB2's
# MakeHuman macro modifiers, NOT a guaranteed-stable API. ``_apply_morphs``
# tolerates an unknown target by letting TargetService surface the error.
#
# Every key in ``body_params.BODY_MORPH_KEYS`` MUST be covered here (enforced by
# the assertion below and by tests/test_generate_body.py).
MODIFIER_MAP: dict = {
    "gender": "macrodetails/Gender",
    "age": "macrodetails/Age",
    "height": "macrodetails-height/Height",
    "weight": "macrodetails-universal/Weight",
    "muscle": "macrodetails-universal/Muscle",
    "proportions": "macrodetails-proportions/BodyProportions",
    # NOTE: MPFB2 exposes body fat through the same universal "Weight" macro as
    # `weight` (there is no independent bodyfat macro in the stock target set),
    # so this intentionally OVERLAPS the `weight` target. Applying both means the
    # later-applied value wins; callers that care should reconcile the two.
    "bodyfat": "macrodetails-universal/Weight",
    # Head scale is a detail target rather than a macro; the exact name differs
    # between MPFB2 builds (head-scale-horiz / head-scale-vert / head-scale...).
    # Validate against the installed target namespace at runtime.
    "head_size": "head/head-scale-horiz-decr|incr",
}

# Fail loud at import time if a canonical key is left unmapped — generation must
# never silently drop a requested morph.
assert set(MODIFIER_MAP.keys()) == set(BODY_MORPH_KEYS), (
    "MODIFIER_MAP must cover exactly the canonical BODY_MORPH_KEYS; "
    f"missing={set(BODY_MORPH_KEYS) - set(MODIFIER_MAP.keys())}, "
    f"extra={set(MODIFIER_MAP.keys()) - set(BODY_MORPH_KEYS)}"
)


# ---------------------------------------------------------------------------
# Blender-script helpers (only called from within Blender, after bpy/mpfb exist)
# ---------------------------------------------------------------------------

def _enable_addons_or_raise() -> None:
    """
    Enable the MPFB2 and VRM addons, raising RuntimeError with an actionable
    message if either cannot be enabled. Mirrors vrm_edit._check_vrm_addon_or_raise
    in spirit (clear, install-pointing errors).
    """
    import bpy

    for module, human_name, url in (
        ("mpfb", "MPFB2 (MakeHuman Plugin for Blender)",
         "https://static.makehumancommunity.org/mpfb.html"),
        ("io_scene_vrm", "VRM_Addon_for_Blender",
         "https://vrm-addon-for-blender.info"),
    ):
        try:
            bpy.ops.preferences.addon_enable(module=module)
        except Exception as exc:  # noqa: BLE001 - op failures vary by build/addon
            raise RuntimeError(
                f"{human_name} not available: could not enable addon "
                f"module '{module}' ({exc}). Install it and enable it in Blender "
                f"preferences. See {url}."
            ) from exc


def _create_human():
    """Create a fresh MakeHuman base mesh via MPFB2 and return its base mesh object."""
    from mpfb.services.humanservice import HumanService

    return HumanService.create_human()


def _apply_morphs(basemesh, morphs: dict) -> int:
    """
    Apply each canonical morph in *morphs* to *basemesh* through MPFB2's
    TargetService, using MODIFIER_MAP to resolve the MakeHuman target string.

    Keys not present in MODIFIER_MAP are skipped. Returns the number of morphs
    actually applied so the caller can report an applied count.
    """
    from mpfb.services.targetservice import TargetService

    applied = 0
    for key, value in morphs.items():
        target = MODIFIER_MAP.get(key)
        if target is None:
            print(f"[generate_body] INFO: morph '{key}' has no MODIFIER_MAP entry, skipping",
                  file=sys.stderr)
            continue
        try:
            TargetService.set_target_value(basemesh, target, value)
            applied += 1
            print(f"[generate_body] INFO: set target '{target}' = {value} (morph '{key}')",
                  file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - target names vary by MPFB2 version
            print(f"[generate_body] WARNING: could not set target '{target}' for morph "
                  f"'{key}' ({exc}); the installed MPFB2 may use a different target name",
                  file=sys.stderr)
    return applied


def _attach_rig(basemesh):
    """
    Attach MPFB2's ``game_engine`` rig to *basemesh* and return the armature
    object. The game_engine rig's bone names are what ``vrm_bone_map`` maps to
    VRM humanoid slots.
    """
    from mpfb.services.rigservice import RigService

    return RigService.add_rig(basemesh, "game_engine")


def _assign_vrm_humanoid(armature) -> list:
    """
    Assign the VRM1 humanoid bone table on *armature* from MPFB_TO_VRM_HUMANOID.

    For each MPFB bone that is actually present on ``armature.data.bones`` set
    ``armature.data.vrm_addon_extension.vrm1.humanoid.human_bones.<slot>.node
    .bone_name`` to the bone name. The property path is dynamic, so every hop is
    resolved defensively with getattr/setattr and missing hops are skipped.

    Returns the list of unfilled REQUIRED VRM slots (also printed as a WARNING).
    Never crashes on a missing slot — VRM export will surface hard errors.
    """
    import bpy  # noqa: F401  (only available inside Blender)

    present_bones = [b.name for b in armature.data.bones]
    present_set = set(present_bones)

    ext = getattr(armature.data, "vrm_addon_extension", None)
    vrm1 = getattr(ext, "vrm1", None) if ext is not None else None
    humanoid = getattr(vrm1, "humanoid", None) if vrm1 is not None else None
    human_bones = getattr(humanoid, "human_bones", None) if humanoid is not None else None

    if human_bones is None:
        print("[generate_body] WARNING: vrm1.humanoid.human_bones not found on armature; "
              "cannot assign VRM humanoid bones", file=sys.stderr)
    else:
        for mpfb_bone, vrm_slot in MPFB_TO_VRM_HUMANOID.items():
            if mpfb_bone not in present_set:
                continue
            slot_obj = getattr(human_bones, vrm_slot, None)
            if slot_obj is None:
                print(f"[generate_body] INFO: VRM slot '{vrm_slot}' not found on human_bones, "
                      f"skipping bone '{mpfb_bone}'", file=sys.stderr)
                continue
            node = getattr(slot_obj, "node", None)
            if node is None:
                print(f"[generate_body] INFO: VRM slot '{vrm_slot}' has no .node, skipping",
                      file=sys.stderr)
                continue
            try:
                setattr(node, "bone_name", mpfb_bone)
                print(f"[generate_body] INFO: VRM humanoid '{vrm_slot}' <- '{mpfb_bone}'",
                      file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - dynamic property path
                print(f"[generate_body] WARNING: could not set VRM slot '{vrm_slot}' "
                      f"bone_name ({exc})", file=sys.stderr)

    missing = missing_required_slots(present_bones)
    if missing:
        print(f"[generate_body] WARNING: required VRM humanoid slot(s) unfilled: "
              f"{', '.join(missing)}. VRM export may reject the model.", file=sys.stderr)
    return missing


def _bake_and_export(out_path: str) -> None:
    """
    Bake every live MakeHuman morph into geometry, then export the scene as VRM.

    The MakeHuman macro modifiers live as shape-key deformation; the VRM exporter
    will not carry live morph weights, so we fold the current mix into each
    mesh's basis with ``mesh.shape_key_add(from_mix=True)`` (same pattern as
    vrm_edit._bake_binds) before exporting.
    """
    import bpy

    for obj in bpy.data.objects:
        if getattr(obj, "type", None) != "MESH":
            continue
        data = getattr(obj, "data", None)
        shape_keys = getattr(data, "shape_keys", None) if data is not None else None
        if shape_keys is None:
            continue
        try:
            obj.shape_key_add(name="_generate_body_baked", from_mix=True)
            print(f"[generate_body] INFO: baked morph mix into basis of "
                  f"'{getattr(obj, 'name', '?')}'", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - shape_key_add API varies by build
            print(f"[generate_body] WARNING: shape_key_add(from_mix=True) failed on "
                  f"'{getattr(obj, 'name', '?')}': {exc}", file=sys.stderr)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    print(f"[generate_body] Exporting VRM: {out_path}", file=sys.stderr)
    result = bpy.ops.export_scene.vrm(filepath=out_path)
    if result != {"FINISHED"}:
        raise RuntimeError(f"VRM export returned: {result}")
    print(f"[generate_body] VRM written: {out_path}", file=sys.stderr)


def _bpy_main() -> None:
    """
    Entry point when this file is executed as a Blender --python script.
    Parses args after '--', builds and exports a human VRM, exits non-zero on
    error.
    """
    import bpy  # noqa: F401  (only available inside Blender)

    argv = sys.argv
    try:
        sep = argv.index("--")
        script_args = argv[sep + 1:]
    except ValueError:
        script_args = []

    parser = argparse.ArgumentParser(
        description="Blender script: generate a parametric human VRM via MPFB2 + VRM addon."
    )
    parser.add_argument("--morphs-file", required=True,
                        help="Path to JSON file containing a canonical morph->value dict")
    parser.add_argument("--out", required=True, help="Path for the output .vrm file")
    parser.add_argument("--report-file", default=None,
                        help="Optional path to write an applied/missing JSON report")
    args = parser.parse_args(script_args)

    out_vrm = os.path.abspath(args.out)

    with open(args.morphs_file, "r", encoding="utf-8") as f:
        morphs = json.load(f)

    try:
        _enable_addons_or_raise()
    except RuntimeError as exc:
        print(f"[generate_body] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        basemesh = _create_human()
        applied = _apply_morphs(basemesh, morphs)
        armature = _attach_rig(basemesh)
        missing = _assign_vrm_humanoid(armature)
        _bake_and_export(out_vrm)
    except Exception as exc:  # noqa: BLE001 - surface any bpy/mpfb failure as exit 1
        print(f"[generate_body] ERROR: body generation failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.report_file:
        report = {
            "morphs": {"requested": len(morphs), "applied": applied},
            "missing_required_slots": missing,
        }
        try:
            with open(args.report_file, "w", encoding="utf-8") as rf:
                json.dump(report, rf)
        except OSError as exc:
            print(f"[generate_body] WARNING: could not write report file: {exc}",
                  file=sys.stderr)


# ---------------------------------------------------------------------------
# Python-side wrapper (runs in normal Python, without bpy)
# ---------------------------------------------------------------------------

def generate_body(
    morphs: dict,
    out_vrm: str | Path,
    blender_path: str | None = None,
    report_file: str | None = None,
) -> str:
    """
    Generate a parametric human VRM by launching Blender headless.

    Isomorphic to ``render.vrm_edit.edit_vrm``: it serializes *morphs* to a temp
    JSON, runs ``blender --background --python <this file> -- --morphs-file ...
    --out ...``, and returns the absolute output path on success.

    Parameters
    ----------
    morphs:
        Dict of canonical morph key -> value (typically the complete 8-key dict
        from ``render.body_params.resolve_body_morphs``). Values are floats in
        0.0..1.0. Keys are resolved to MPFB2 targets via ``MODIFIER_MAP``.
    out_vrm:
        Destination path for the output .vrm file.
    blender_path:
        Blender binary path. When *None*, the ``BLENDER_PATH`` environment
        variable is used; if that is unset, ``"blender"`` is tried (must be on
        PATH).
    report_file:
        Optional host-side path; when given, ``--report-file`` is passed so
        Blender writes an applied/missing-slot JSON report there. The file is NOT
        deleted by this wrapper (the caller owns it).

    Returns
    -------
    str
        Absolute path to the written .vrm file.

    Raises
    ------
    RuntimeError
        When Blender times out, or exits non-zero (which includes the cases
        where MPFB2 or the VRM addon are absent — the bpy script prints an
        actionable error message before exiting).

    Runtime requirements
    --------------------
    Blender + MPFB2 + VRM_Addon_for_Blender must all be installed and enableable;
    none are required (or importable) in this wrapper's plain-Python process.
    """
    out_vrm = Path(out_vrm).resolve()

    if blender_path is None:
        blender_path = os.environ.get("BLENDER_PATH", "blender")

    script_path = Path(__file__).resolve()

    # Write morphs to a temp JSON file to avoid command-line length limits.
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        json.dump(morphs, tmp)
        morphs_file = tmp.name

    try:
        cmd = [
            blender_path,
            "--background",
            "--python", str(script_path),
            "--",
            "--morphs-file", morphs_file,
            "--out", str(out_vrm),
        ]
        if report_file is not None:
            cmd += ["--report-file", str(report_file)]

        timeout = int(os.environ.get("VRM_SUBPROCESS_TIMEOUT", "600"))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Blender timed out after {timeout}s during body generation."
            )

        if result.returncode != 0:
            stderr_tail = result.stderr[-2000:] if result.stderr else ""
            stdout_tail = result.stdout[-1000:] if result.stdout else ""
            raise RuntimeError(
                f"Blender exited with code {result.returncode} during body generation.\n"
                f"--- stderr (tail) ---\n{stderr_tail}\n"
                f"--- stdout (tail) ---\n{stdout_tail}"
            )
    finally:
        try:
            os.unlink(morphs_file)
        except OSError:
            pass

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
