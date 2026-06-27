"""
Shared test helpers for fabricating minimal, structurally-valid .glb/.vrm files.

The output-guarantee gate (render.vrm_utils.assert_valid_glb) rejects a clean
Blender exit that produced no real file.  Tests that mock a *successful* Blender
run must therefore also simulate Blender writing a valid GLB to the --out path —
these helpers produce the smallest blob that passes assert_valid_glb.
"""

import struct
from pathlib import Path

_GLB_DEFAULT_SIZE = 128  # comfortably above assert_valid_glb's default min_bytes=64


def minimal_glb_bytes(total_size: int = _GLB_DEFAULT_SIZE) -> bytes:
    """Return bytes of a header-valid GLB whose declared length == total_size."""
    if total_size < 12:
        total_size = 12
    header = b"glTF" + struct.pack("<I", 2) + struct.pack("<I", total_size)
    return header + b"\x00" * (total_size - 12)


def write_minimal_glb(path, total_size: int = _GLB_DEFAULT_SIZE) -> None:
    """Write a header-valid GLB to *path*."""
    Path(path).write_bytes(minimal_glb_bytes(total_size))


def write_glb_for_out_flag(cmd, total_size: int = _GLB_DEFAULT_SIZE) -> None:
    """
    Given a Blender command list, write a valid GLB to the path following the
    ``--out`` flag.  No-op when the flag is absent.  Use inside a mocked
    ``subprocess.run`` side_effect to simulate Blender writing its output.
    """
    try:
        out_path = cmd[cmd.index("--out") + 1]
    except (ValueError, IndexError):
        return
    write_minimal_glb(out_path, total_size)
