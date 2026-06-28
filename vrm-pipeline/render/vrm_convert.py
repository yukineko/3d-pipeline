"""
render/vrm_convert.py - GLB -> VRM conversion via Blender headless + VRM addon.

Python-side wrapper
-------------------
    from render.vrm_convert import glb_to_vrm
    vrm_path = glb_to_vrm("character.glb", "character.vrm")

Blender script mode
-------------------
When run directly by Blender (--python render/vrm_convert.py -- ...) the module
executes as a bpy script: it imports the GLB, attempts VRM export via the VRM
addon operator, and exits with code 0 on success or 1 on failure.

Environment / arguments
------------------------
  BLENDER_PATH  Override Blender binary location (default: "blender").

  glb_to_vrm(glb_path, vrm_path, blender_path=None)
      blender_path: explicit binary; falls back to env BLENDER_PATH then "blender".
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Blender-script entry point  (runs only inside Blender, i.e. when bpy is
# importable).  We guard this block so the module stays importable in plain
# Python as well.
# ---------------------------------------------------------------------------

def _bpy_main() -> None:
    """
    Entry point when this file is executed as a Blender --python script.
    Parses args after '--', imports GLB, exports VRM, exits non-zero on error.
    """
    import bpy  # noqa: F401  (only available inside Blender)

    argv = sys.argv
    try:
        sep = argv.index("--")
        script_args = argv[sep + 1:]
    except ValueError:
        script_args = []

    parser = argparse.ArgumentParser(
        description="Blender script: import GLB and export VRM via VRM addon."
    )
    parser.add_argument("--glb", required=True, help="Path to input .glb file")
    parser.add_argument("--out", required=True, help="Path for output .vrm file")
    args = parser.parse_args(script_args)

    glb_path = os.path.abspath(args.glb)
    vrm_path = os.path.abspath(args.out)

    # Ensure output directory exists
    os.makedirs(os.path.dirname(vrm_path) or ".", exist_ok=True)

    # -----------------------------------------------------------------
    # Check VRM addon availability before doing heavy work
    # -----------------------------------------------------------------
    _check_vrm_addon_or_raise()

    # -----------------------------------------------------------------
    # Clear default scene and import GLB
    # -----------------------------------------------------------------
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    print(f"[vrm_convert] Importing GLB: {glb_path}", file=sys.stderr)
    try:
        result = bpy.ops.import_scene.gltf(filepath=glb_path)
    except Exception as exc:
        print(f"[vrm_convert] ERROR: GLB import failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if result != {"FINISHED"}:
        print(f"[vrm_convert] ERROR: GLB import returned: {result}", file=sys.stderr)
        sys.exit(1)

    # -----------------------------------------------------------------
    # Export VRM
    # -----------------------------------------------------------------
    print(f"[vrm_convert] Exporting VRM: {vrm_path}", file=sys.stderr)
    try:
        result = _run_vrm_export(vrm_path)
    except RuntimeError as exc:
        print(f"[vrm_convert] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"[vrm_convert] ERROR: VRM export failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if result != {"FINISHED"}:
        print(f"[vrm_convert] ERROR: VRM export returned: {result}", file=sys.stderr)
        sys.exit(1)

    print(f"[vrm_convert] VRM written: {vrm_path}", file=sys.stderr)


def _check_vrm_addon_or_raise() -> None:
    """
    Verify that the VRM addon export operator is registered.
    Raises RuntimeError with an actionable message if not found.
    """
    import bpy

    # Try each known operator name in order
    _VRM_EXPORT_OPS = [
        ("export_scene", "vrm"),
        ("export_scene", "vrm1"),
        ("vrm", "export_scene"),
    ]

    for namespace, name in _VRM_EXPORT_OPS:
        ns = getattr(bpy.ops, namespace, None)
        if ns is None:
            continue
        op = getattr(ns, name, None)
        if op is None:
            continue
        try:
            # poll() returns True only when the operator is fully registered
            op.poll()
            return  # found a working operator
        except Exception:
            return  # registered but poll raised — still counts as present

    raise RuntimeError(
        "VRM addon not available: install VRM_Addon_for_Blender "
        "(https://vrm-addon-for-blender.info) and enable it in Blender preferences. "
        "Tried operators: export_scene.vrm, export_scene.vrm1, vrm.export_scene."
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


# ---------------------------------------------------------------------------
# Python-side wrapper (runs in normal Python, without bpy)
# ---------------------------------------------------------------------------

def glb_to_vrm(
    glb_path: str | Path,
    vrm_path: str | Path,
    blender_path: str | None = None,
) -> str:
    """
    Convert a GLB file to VRM by launching Blender headless.

    Parameters
    ----------
    glb_path:
        Path to the input .glb file.
    vrm_path:
        Destination path for the output .vrm file.
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
        When the input GLB file does not exist.
    RuntimeError
        When Blender exits with a non-zero return code (which includes the case
        where the VRM addon is absent — the bpy script prints an actionable
        error message before exiting).
    """
    glb_path = Path(glb_path).resolve()
    vrm_path = Path(vrm_path).resolve()

    if not glb_path.exists():
        raise FileNotFoundError(f"Input GLB not found: {glb_path}")

    if blender_path is None:
        blender_path = os.environ.get("BLENDER_PATH", "blender")

    # This script file is the bpy script Blender will execute
    script_path = Path(__file__).resolve()

    cmd = [
        blender_path,
        "--background",
        "--python", str(script_path),
        "--",
        "--glb", str(glb_path),
        "--out", str(vrm_path),
    ]

    timeout = int(os.environ.get("VRM_SUBPROCESS_TIMEOUT", "600"))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Blender timed out after {timeout}s during GLB→VRM conversion."
        )

    if result.returncode != 0:
        # Surface the bpy script's error output so the caller can diagnose
        # addon-not-found vs. other errors without inspecting raw stderr.
        stderr_tail = result.stderr[-2000:] if result.stderr else ""
        stdout_tail = result.stdout[-1000:] if result.stdout else ""
        raise RuntimeError(
            f"Blender exited with code {result.returncode} during GLB→VRM conversion.\n"
            f"--- stderr (tail) ---\n{stderr_tail}\n"
            f"--- stdout (tail) ---\n{stdout_tail}"
        )

    # Output guarantee: a clean Blender exit does not prove a valid VRM was
    # written.  Reject a missing/empty/corrupt file before reporting success.
    from render.vrm_utils import assert_valid_glb

    assert_valid_glb(vrm_path)

    return str(vrm_path)


# ---------------------------------------------------------------------------
# Host-side CLI (plain Python; no bpy).  The host wrapper `glb_to_vrm()` spawns
# Blender itself at runtime, so argument parsing here needs no Blender.
# ---------------------------------------------------------------------------

def _host_main() -> None:
    """Plain-Python CLI entry: parse args and call ``glb_to_vrm()`` which
    launches Blender headlessly to convert a GLB into a VRM."""
    parser = argparse.ArgumentParser(
        description="Convert a GLB file to VRM headlessly via Blender.",
    )
    parser.add_argument("--glb", dest="glb", required=True,
                        help="Path to the input .glb file.")
    parser.add_argument("--out", dest="out", required=True,
                        help="Destination path for the output .vrm file.")
    parser.add_argument("--blender", dest="blender", default=None,
                        help="Path to the Blender executable (default: auto).")
    args = parser.parse_args()

    out_path = glb_to_vrm(args.glb, args.out, blender_path=args.blender)
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
