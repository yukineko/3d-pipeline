"""
Shared post-condition helpers for the VRM/GLB render pipeline.

These guards exist so that a *successful* Blender exit (returncode 0) is never
mistaken for a *valid* output.  Headless Blender can exit 0 while the VRM/GLB
exporter wrote nothing, a truncated file, or a non-glTF blob — and downstream
code (ledger insert, rendering) would otherwise treat that as a finished
avatar.  ``assert_valid_glb`` makes the "the file Blender promised actually
exists and is a well-formed glTF binary" check explicit and reusable.
"""

from __future__ import annotations

import struct
from pathlib import Path

# glTF binary (.glb) container — VRM is a .glb under the hood.
# Header layout (little-endian): magic(u32) version(u32) length(u32) = 12 bytes.
# https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#binary-gltf-layout
_GLB_MAGIC = b"glTF"
_GLB_HEADER_SIZE = 12


def assert_valid_glb(path: str | Path, min_bytes: int = 64) -> None:
    """
    Assert that *path* is a present, non-trivial, well-formed glTF binary.

    Raises ``RuntimeError`` (matching the pipeline's existing error style) when:
      (a) the file does not exist,
      (b) its size is below ``min_bytes`` (an empty/truncated export), or
      (c) the 12-byte GLB header is malformed — the first 4 bytes are not the
          ``glTF`` magic, or the declared total length (bytes 8–11, little-endian
          ``uint32``) does not match the file's actual size.

    A passing call guarantees the output is at least a structurally-plausible
    GLB/VRM container; it does not assert VRM-semantic validity (humanoid bones,
    T-pose, etc.), which is a separate, higher-level gate.
    """
    p = Path(path)

    if not p.exists():
        raise RuntimeError(f"Output GLB/VRM was not written: {p}")

    size = p.stat().st_size
    if size < min_bytes:
        raise RuntimeError(
            f"Output GLB/VRM is too small to be valid "
            f"({size} bytes < {min_bytes} min): {p}"
        )

    with p.open("rb") as fh:
        header = fh.read(_GLB_HEADER_SIZE)

    if len(header) < _GLB_HEADER_SIZE:
        raise RuntimeError(f"Output GLB/VRM is truncated (no GLB header): {p}")

    magic = header[:4]
    if magic != _GLB_MAGIC:
        raise RuntimeError(
            f"Output is not a glTF binary (bad magic {magic!r}, expected "
            f"{_GLB_MAGIC!r}): {p}"
        )

    declared_len = struct.unpack_from("<I", header, 8)[0]
    if declared_len != size:
        raise RuntimeError(
            f"Output GLB/VRM is corrupt: header declares {declared_len} bytes "
            f"but file is {size} bytes: {p}"
        )
