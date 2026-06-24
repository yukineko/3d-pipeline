#!/usr/bin/env python3
"""
calibrate.py — Noise-floor calibration for eval-A pHash comparison.

Renders the same VRM N times with Blender and computes pHash Hamming distances
across all run-pairs for each face.  Outputs a JSON summary that can be used as
the noise_floor threshold in eval-A.

Usage:
    python calibrate.py --vrm <path.vrm> [--blender-path blender] [--runs 3] [--output result.json]
"""

import argparse
import itertools
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Dependency check — imagehash must be importable at runtime
# ---------------------------------------------------------------------------
try:
    import imagehash
    from PIL import Image
except ImportError:
    print(
        "[calibrate] ERROR: 'imagehash' (and 'Pillow') must be installed.\n"
        "  pip install imagehash Pillow",
        file=sys.stderr,
    )
    sys.exit(1)

HERE = Path(__file__).parent.resolve()

FACE_NAMES = [
    "body_front",
    "body_side",
    "body_back",
    "face_front",
    "face_L",
    "face_R",
    "face_34",
]


# ---------------------------------------------------------------------------
# Utilities (same pattern as pipeline.py _run)
# ---------------------------------------------------------------------------

def _run(cmd, label=""):
    """Run subprocess, return (stdout, stderr, returncode)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except (FileNotFoundError, OSError) as exc:
        print(f"[calibrate] WARN {label} could not launch: {exc}", file=sys.stderr)
        return "", str(exc), 1
    if result.returncode != 0:
        print(f"[calibrate] WARN {label} exit={result.returncode}", file=sys.stderr)
        if result.stderr:
            print(result.stderr[-2000:], file=sys.stderr)
    return result.stdout, result.stderr, result.returncode


def _render_vrm(vrm_path: Path, output_dir: Path, blender_path: str) -> bool:
    """
    Render VRM into output_dir at 256px.
    Returns True on success, False on failure (non-fatal).
    """
    cmd = [
        blender_path, "--background", "--python",
        str(HERE / "render" / "vrm.py"),
        "--", "--vrm", str(vrm_path), "--output", str(output_dir),
        "--resolution", "256",
    ]
    _out, _err, rc = _run(cmd, f"render/vrm.py -> {output_dir.name}")
    return rc == 0


def _collect_hashes(run_dir: Path) -> dict:
    """
    Load each face image from run_dir and compute its pHash.
    Returns {face_name: phash_object}.  Missing files are silently skipped.
    """
    hashes = {}
    for face in FACE_NAMES:
        img_path = run_dir / f"{face}.webp"
        if img_path.exists():
            try:
                hashes[face] = imagehash.phash(Image.open(str(img_path)))
            except Exception as exc:
                print(
                    f"[calibrate] WARN could not hash {img_path.name}: {exc}",
                    file=sys.stderr,
                )
    return hashes


# ---------------------------------------------------------------------------
# Main calibration logic
# ---------------------------------------------------------------------------

def calibrate(vrm_path: Path, blender_path: str, runs: int) -> dict:
    """
    Render vrm_path `runs` times, collect pHash per face, compute all pairwise
    Hamming distances, and return the calibration result dict.
    """
    run_dirs = []
    run_hashes = []  # list of {face: phash}

    for n in range(runs):
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"calibrate_{n}_"))
        print(f"[calibrate] run {n + 1}/{runs} -> {tmp_dir}", file=sys.stderr)
        ok = _render_vrm(vrm_path, tmp_dir, blender_path)
        if not ok:
            print(f"[calibrate] run {n + 1} render failed, collecting whatever was written", file=sys.stderr)
        run_dirs.append(tmp_dir)
        run_hashes.append(_collect_hashes(tmp_dir))

    # Compute pairwise distances for each face
    per_face: dict[str, list[int]] = {}
    noise_floor: dict[str, int] = {}

    all_faces = set()
    for h in run_hashes:
        all_faces.update(h.keys())

    for face in sorted(all_faces):
        # Only consider runs where this face was successfully hashed
        face_hashes = [h[face] for h in run_hashes if face in h]
        distances: list[int] = []
        for ha, hb in itertools.combinations(face_hashes, 2):
            distances.append(int(ha - hb))
        per_face[face] = distances
        noise_floor[face] = max(distances) if distances else 0

    # Summary statistics
    all_values = [v for dists in per_face.values() for v in dists]
    if all_values:
        max_noise = max(all_values)
        mean_noise = round(sum(all_values) / len(all_values), 4)
    else:
        max_noise = 0
        mean_noise = 0.0

    return {
        "vrm": str(vrm_path),
        "runs": runs,
        "noise_floor": noise_floor,
        "per_face": per_face,
        "summary": {
            "max_noise": max_noise,
            "mean_noise": mean_noise,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Calibrate eval-A noise floor by rendering a VRM multiple times"
    )
    parser.add_argument("--vrm", required=True, help="Path to .vrm file")
    parser.add_argument(
        "--blender-path", default="blender", help="Path to Blender binary (default: blender)"
    )
    parser.add_argument(
        "--runs", type=int, default=3, help="Number of render runs (default: 3)"
    )
    parser.add_argument(
        "--output", default=None,
        help="Write JSON result to this file (default: stdout)"
    )

    args = parser.parse_args()
    vrm_path = Path(args.vrm).resolve()

    result = calibrate(vrm_path, args.blender_path, args.runs)

    out_str = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_str, encoding="utf-8")
        print(f"[calibrate] result written to {out_path}", file=sys.stderr)
    else:
        print(out_str)


if __name__ == "__main__":
    main()
