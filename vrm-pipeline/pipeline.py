#!/usr/bin/env python3
"""
pipeline.py — Drop-zone watcher → generate → render → ledger

Trigger files in --drop-dir:
  *.vrm             → VRM pipeline: render/vrm.py → ledger → embed → tag
  *.prompt          → Object pipeline: generate.py → render/object.py → ledger → embed → tag
  *.glb *.gltf *.obj *.fbx *.blend
                    → Object render: render/object.py → ledger → embed → tag

Sidecar conventions:
  <stem>.prompt     next to *.vrm or mesh files overrides the prompt text
  <stem>.params.json generation params override (merged into generation_params)

Usage:
    python pipeline.py \\
        --drop-dir ./drop \\
        --output-base ./output \\
        --blender-path blender \\
        [--db-path ~/.vrm-pipeline/ledger.db] \\
        [--golden-dir ./golden] \\
        [--scene-context ./scenes/terrain.blend] \\
        [--resolution 768] \\
        [--embed-model dinov2-small] \\
        [--no-embed] [--no-tag] \\
        [--interval 3]
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent.resolve()

MESH_EXTS = {".glb", ".gltf", ".obj", ".fbx", ".blend"}
VRM_EXTS = {".vrm"}
STATE_FILE = ".pipeline_state.json"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _run(cmd, label=""):
    """Run subprocess, return (stdout, stderr, returncode)."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[pipeline] WARN {label} exit={result.returncode}", file=sys.stderr)
        if result.stderr:
            print(result.stderr[-2000:], file=sys.stderr)
    return result.stdout, result.stderr, result.returncode


def _extract_json(text):
    """Find first parseable JSON object in multi-line text."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    # Try whole text
    try:
        return json.loads(text)
    except Exception:
        return {}


def _read_sidecar_prompt(path: Path) -> str:
    """Read <stem>.prompt sidecar if present, else return stem as fallback."""
    sidecar = path.with_suffix(".prompt")
    if sidecar.exists():
        return sidecar.read_text(encoding="utf-8").strip()
    return path.stem.replace("_", " ").replace("-", " ")


def _read_sidecar_params(path: Path) -> dict:
    sidecar = path.with_suffix(".params.json")
    if sidecar.exists():
        try:
            return json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_state(drop_dir: Path) -> dict:
    state_path = drop_dir / STATE_FILE
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(drop_dir: Path, state: dict):
    state_path = drop_dir / STATE_FILE
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _mark_done(drop_dir: Path, state: dict, file_hash: str, record_id: str, stem: str = ""):
    state[file_hash] = {"record_id": record_id, "ts": time.time()}
    if stem:
        state[f"stem:{stem}"] = {"record_id": record_id, "ts": time.time()}
    _save_state(drop_dir, state)


def _lookup_r0_record_id(drop_dir: Path, state: dict, base_stem: str) -> str | None:
    """Return record_id for the original R0 VRM identified by base_stem."""
    entry = state.get(f"stem:{base_stem}")
    if entry:
        return entry.get("record_id")
    return None


def _file_hash(path: Path) -> str:
    sha = hashlib.sha256()
    sha.update(str(path.stat().st_mtime).encode())
    sha.update(path.name.encode())
    return sha.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Ledger operations
# ---------------------------------------------------------------------------

def _compute_phash_distance(dir_a: Path, dir_b: Path) -> float | None:
    """Average pHash hamming distance between matching *.webp files in two dirs."""
    try:
        import imagehash
        from PIL import Image
    except ImportError:
        return None
    distances = []
    for img_file in sorted(dir_a.glob("*.webp")):
        other = dir_b / img_file.name
        if not other.exists():
            continue
        try:
            h_a = imagehash.phash(Image.open(img_file))
            h_b = imagehash.phash(Image.open(other))
            distances.append(h_a - h_b)
        except Exception:
            pass
    if not distances:
        return None
    return sum(distances) / len(distances)


def _ledger_update_outcome_phash(db_path: Path, record_id: str, edit_dist: float):
    cmd = [
        "ledger", "--db", str(db_path), "update-outcome",
        "--id", record_id,
        "--edit-dist-phash", str(edit_dist),
    ]
    _run(cmd, "ledger update-outcome")


def _ledger_update_outcome_embed(db_path: Path, record_id: str, edit_dist: float):
    cmd = [
        "ledger", "--db", str(db_path), "update-outcome",
        "--id", record_id,
        "--edit-dist-embed", str(edit_dist),
    ]
    _run(cmd, "ledger update-outcome embed")


def _ledger_set_r1_ref(db_path: Path, record_id: str, r1_dir: str):
    cmd = [
        "ledger", "--db", str(db_path), "set-r1-ref",
        "--id", record_id,
        "--path", r1_dir,
    ]
    _run(cmd, "ledger set-r1-ref")


def _ledger_get_embedding(db_path: Path, record_id: str) -> list | None:
    cmd = ["ledger", "--db", str(db_path), "get-embedding", "--id", record_id]
    out, err, rc = _run(cmd, "ledger get-embedding")
    if rc != 0:
        return None
    try:
        return json.loads(out).get("record_embedding")
    except Exception:
        return None


def _cosine_distance(a: list, b: list) -> float | None:
    if len(a) != len(b) or not a:
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return None
    return 1.0 - dot / (na * nb)


def _ledger_init(db_path: Path):
    cmd = ["ledger", "--db", str(db_path), "init"]
    out, err, rc = _run(cmd, "ledger init")
    if rc != 0:
        raise RuntimeError(f"ledger init failed: {err}")


def _ledger_insert(
    db_path: Path,
    prompt: str,
    r0_dir: str,
    generation_params: dict,
    parent_id: str | None = None,
    image_ref: str | None = None,
) -> str:
    cmd = [
        "ledger", "--db", str(db_path), "insert",
        "--prompt", prompt,
        "--r0-dir", r0_dir,
        "--generation-params", json.dumps(generation_params),
    ]
    if parent_id:
        cmd += ["--parent-id", parent_id]
    if image_ref:
        cmd += ["--image-ref", image_ref]
    out, err, rc = _run(cmd, "ledger insert")
    if rc != 0:
        raise RuntimeError(f"ledger insert failed: {err}")
    return out.strip()


def _validate_vrm_gate(vrm_path: Path, output_dir: Path) -> dict:
    """
    VRM output-quality gate, run before ``_ledger_insert`` so a structurally
    broken avatar never enters the lineage.

    Writes the validation report to ``output_dir/validation.json`` (alongside
    ``manifest.json``, so it rides into the ledger as part of ``r0_dir``) and
    returns the report dict on success.  Raises ``RuntimeError`` — blocking the
    ledger insert — when validation finds *errors* (no VRM extension, a missing
    required humanoid bone, a dangling bone node, glTF-Validator structural
    errors).  T-pose deviations are recorded as warnings and do not block,
    since ``height_scale`` edits legitimately perturb the rig.
    """
    from render.vrm_validate import assert_valid_vrm  # lazy: keep module import light

    report_path = Path(output_dir) / "validation.json"
    return assert_valid_vrm(vrm_path, report_path=report_path)


def _pop_parent_id(params: dict) -> str | None:
    """Pull parent_id out of merged params so it rides as a lineage link, not a gen param."""
    return params.pop("parent_id", None)


def _pop_image_ref(params: dict) -> str | None:
    """Pull image_ref out of merged params so it rides as a dedicated column, not a gen param."""
    return params.pop("image_ref", None)


def _ledger_embed(db_path: Path, record_id: str, embed_json: Path):
    cmd = ["ledger", "--db", str(db_path), "embed",
           "--id", record_id, "--embed-json", str(embed_json)]
    _run(cmd, "ledger embed")


def _ledger_tag(db_path: Path, record_id: str, tag_json: Path):
    cmd = ["ledger", "--db", str(db_path), "tag",
           "--id", record_id, "--tag-json", str(tag_json)]
    _run(cmd, "ledger tag")


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

def _render_vrm(vrm_path: Path, output_dir: Path, args) -> dict:
    cmd = [
        args.blender_path, "--background", "--python",
        str(HERE / "render" / "vrm.py"),
        "--", "--vrm", str(vrm_path), "--output", str(output_dir),
        "--resolution", str(args.resolution),
    ]
    if args.golden_dir:
        cmd += ["--golden", args.golden_dir]
    out, err, rc = _run(cmd, "render/vrm.py")
    if rc != 0:
        raise RuntimeError(f"render/vrm.py failed (exit {rc})")
    return _extract_json(out)


def _render_object(mesh_path: Path, output_dir: Path, args) -> dict:
    cmd = [
        args.blender_path, "--background", "--python",
        str(HERE / "render" / "object.py"),
        "--", "--input", str(mesh_path), "--output-dir", str(output_dir),
        "--resolution", str(args.resolution),
    ]
    if args.golden_dir:
        cmd += ["--golden-dir", args.golden_dir]
    if args.scene_context:
        cmd += ["--scene-context", args.scene_context]
    out, err, rc = _run(cmd, "render/object.py")
    if rc != 0:
        raise RuntimeError(f"render/object.py failed (exit {rc})")
    return _extract_json(out)


def _generate(prompt: str, output_dir: Path, args, image_ref: str | None = None) -> dict:
    glb_output_dir = output_dir / "generated"
    glb_output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(HERE / "generate.py"),
        "--prompt", prompt,
        "--output-dir", str(glb_output_dir),
        "--max-retries", str(args.gen_retries),
        "--blender-path", args.blender_path,
        "--model", args.gen_model,
    ]
    if image_ref:
        # Image-driven generation: generate.py routes through the Hyper3D backend.
        cmd += ["--image", image_ref]
    out, err, rc = _run(cmd, "generate.py")
    if rc != 0:
        raise RuntimeError(f"generate.py failed (exit {rc})")
    gen_params = _extract_json(out)
    if not gen_params.get("output_glb"):
        raise RuntimeError("generate.py did not output output_glb in JSON")
    return gen_params


def _run_embed(render_dir: Path, tmp_dir: Path, args) -> Path | None:
    if args.no_embed:
        return None
    embed_json = tmp_dir / "embed.json"
    cmd = [
        sys.executable, str(HERE / "embed.py"),
        "--render-dir", str(render_dir),
        "--model", args.embed_model,
        "--output", str(embed_json),
    ]
    out, err, rc = _run(cmd, "embed.py")
    if rc != 0:
        print(f"[pipeline] embed.py failed (non-fatal)", file=sys.stderr)
        return None
    return embed_json if embed_json.exists() else None


def _run_tag(render_dir: Path, tmp_dir: Path, args) -> Path | None:
    if args.no_tag:
        return None
    tag_json = tmp_dir / "tags.json"
    cmd = [
        sys.executable, str(HERE / "tag.py"),
        "--render-dir", str(render_dir),
        "--output", str(tag_json),
    ]
    out, err, rc = _run(cmd, "tag.py")
    if rc != 0:
        print(f"[pipeline] tag.py failed (non-fatal)", file=sys.stderr)
        return None
    return tag_json if tag_json.exists() else None


# ---------------------------------------------------------------------------
# Flow handlers
# ---------------------------------------------------------------------------

def handle_vrm(path: Path, args) -> str:
    print(f"[pipeline] VRM flow: {path.name}")
    stem = path.stem
    output_dir = Path(args.output_base) / "renders" / stem
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = _render_vrm(path, output_dir, args)

    prompt = _read_sidecar_prompt(path)
    extra_params = _read_sidecar_params(path)
    gen_params = {
        "asset_type": "vrm",
        "blender_version": summary.get("blender_version"),
        "vrm_addon_version": summary.get("vrm_addon_version"),
        "render_sha256": summary.get("render_sha256"),
        **extra_params,
    }
    parent_id = _pop_parent_id(gen_params)

    record_id = _ledger_insert(args.db_path, prompt, str(output_dir), gen_params, parent_id)
    print(f"[pipeline] ledger record: {record_id}")

    _post_process(record_id, output_dir, args)
    return record_id


def handle_vrm_r1(path: Path, args, state: dict, drop_dir: Path) -> str:
    """Process <stem>_r1.vrm: render R1, compute R0↔R1 pHash distance, update ledger."""
    base_stem = path.stem[:-3]  # strip "_r1"
    print(f"[pipeline] R1 flow: {path.name} (base={base_stem})")

    r0_record_id = _lookup_r0_record_id(drop_dir, state, base_stem)
    if not r0_record_id:
        print(f"[pipeline] WARN no R0 record found for stem '{base_stem}', skipping R1", file=sys.stderr)
        return ""

    r0_dir = Path(args.output_base) / "renders" / base_stem
    r1_dir = Path(args.output_base) / "renders" / path.stem
    r1_dir.mkdir(parents=True, exist_ok=True)

    _render_vrm(path, r1_dir, args)
    _ledger_set_r1_ref(args.db_path, r0_record_id, str(r1_dir))

    phash_dist = _compute_phash_distance(r0_dir, r1_dir)
    if phash_dist is not None:
        _ledger_update_outcome_phash(args.db_path, r0_record_id, phash_dist)
        print(f"[pipeline] R1 edit_dist_phash={phash_dist:.4f} -> record {r0_record_id}")
    else:
        print(f"[pipeline] WARN could not compute pHash distance (imagehash not available?)", file=sys.stderr)

    if not args.no_embed:
        tmp_dir = r1_dir / ".pipeline_tmp"
        tmp_dir.mkdir(exist_ok=True)
        r1_embed_json = _run_embed(r1_dir, tmp_dir, args)
        if r1_embed_json:
            r1_data = json.loads(r1_embed_json.read_text(encoding="utf-8"))
            r1_vec = r1_data.get("record_embedding")
            r0_vec = _ledger_get_embedding(args.db_path, r0_record_id)
            if r1_vec and r0_vec:
                embed_dist = _cosine_distance(r0_vec, r1_vec)
                if embed_dist is not None:
                    _ledger_update_outcome_embed(args.db_path, r0_record_id, embed_dist)
                    print(f"[pipeline] R1 edit_dist_embed={embed_dist:.6f} -> record {r0_record_id}")

    return r0_record_id


def _enrich_prompt(prompt: str, args) -> str:
    """Call suggest.py to enrich prompt; fall back to original on any failure."""
    if not getattr(args, "enrich_prompt", False):
        return prompt
    cmd = [
        sys.executable, str(HERE / "suggest.py"),
        "--prompt", prompt,
        "--db-path", str(args.db_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            enriched = result.stdout.strip()
            if enriched:
                print(f"[pipeline] prompt enriched via suggest.py")
                return enriched
        print(f"[pipeline] suggest.py failed (non-fatal), using original prompt", file=sys.stderr)
    except Exception as exc:
        print(f"[pipeline] suggest.py error (non-fatal): {exc}", file=sys.stderr)
    return prompt


def handle_prompt(path: Path, args) -> str:
    prompt = path.read_text(encoding="utf-8").strip()
    print(f"[pipeline] Object flow (prompt): {path.name}")
    stem = path.stem
    output_dir = Path(args.output_base) / "renders" / stem
    output_dir.mkdir(parents=True, exist_ok=True)

    sidecar_params = _read_sidecar_params(path)
    parent_id = _pop_parent_id(sidecar_params)
    image_ref = _pop_image_ref(sidecar_params)
    vroid_edit = sidecar_params.pop("vroid_edit", False)
    base_vrm = sidecar_params.pop("base_vrm", None)
    change = sidecar_params.pop("change", None)
    preset = sidecar_params.pop("preset", None)
    prompt = _enrich_prompt(prompt, args)

    if vroid_edit:
        # VRoid edit flow: infer VRM param adjustments from prompt/image,
        # apply them to the parent's base VRM via Blender (VRM addon), re-export.
        if not base_vrm:
            raise RuntimeError("vroid_edit requires base_vrm in sidecar params")
        from vroid_params import resolve_vrm_adjustments  # lazy: pulls in Gemini SDK
        from render.vrm_edit import edit_vrm  # lazy: avoids bpy import at module load
        from presets import DEFAULT_PRESET_NAME  # lazy: get default preset name
        adjustments = resolve_vrm_adjustments(
            change or prompt, preset_name=preset, image_path=image_ref, model=args.gen_model
        )
        vrm_path = output_dir / "generated" / f"{stem}.vrm"
        vrm_path.parent.mkdir(parents=True, exist_ok=True)
        edit_vrm(base_vrm, vrm_path, adjustments, blender_path=args.blender_path)
        print(f"[pipeline] edited VRM -> {vrm_path}")

        validation = _validate_vrm_gate(vrm_path, output_dir)
        if validation.get("warnings"):
            print(f"[pipeline] VRM validation warnings: {len(validation['warnings'])}")

        summary = _render_vrm(vrm_path, output_dir, args)
        gen_params = {
            "asset_type": "vrm",
            "vroid_edit": True,
            "base_vrm": str(base_vrm),
            "change": change,
            "preset": preset or DEFAULT_PRESET_NAME,
            "adjustments": adjustments,
            "vrm_path": str(vrm_path),
            "validation": validation,
            "blender_version": summary.get("blender_version"),
            "render_sha256": summary.get("render_sha256"),
        }
    elif image_ref:
        # Image-derived VRM flow: Hyper3D image-to-3D → GLB → VRM conversion.
        gen_params = _generate(prompt, output_dir, args, image_ref=image_ref)
        glb_path = Path(gen_params["output_glb"])
        vrm_path = output_dir / "generated" / f"{stem}.vrm"
        from render.vrm_convert import glb_to_vrm  # lazy: avoids bpy import at module load
        glb_to_vrm(glb_path, vrm_path, blender_path=args.blender_path)
        print(f"[pipeline] converted GLB -> VRM: {vrm_path}")

        validation = _validate_vrm_gate(vrm_path, output_dir)
        if validation.get("warnings"):
            print(f"[pipeline] VRM validation warnings: {len(validation['warnings'])}")

        summary = _render_vrm(vrm_path, output_dir, args)
        gen_params.update({
            "asset_type": "vrm",
            "vrm_path": str(vrm_path),
            "validation": validation,
            "blender_version": summary.get("blender_version"),
            "render_sha256": summary.get("render_sha256"),
        })
    else:
        gen_params = _generate(prompt, output_dir, args)
        mesh_path = Path(gen_params["output_glb"])

        summary = _render_object(mesh_path, output_dir, args)
        gen_params.update({
            "asset_type": "object",
            "blender_version": summary.get("blender_version"),
            "render_sha256": summary.get("render_sha256"),
        })

    record_id = _ledger_insert(
        args.db_path, prompt, str(output_dir), gen_params, parent_id, image_ref
    )
    print(f"[pipeline] ledger record: {record_id}")

    _post_process(record_id, output_dir, args)
    return record_id


def handle_mesh(path: Path, args) -> str:
    print(f"[pipeline] Object render flow: {path.name}")
    stem = path.stem
    output_dir = Path(args.output_base) / "renders" / stem
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = _render_object(path, output_dir, args)

    prompt = _read_sidecar_prompt(path)
    extra_params = _read_sidecar_params(path)
    gen_params = {
        "asset_type": "object",
        "source": str(path),
        "blender_version": summary.get("blender_version"),
        "render_sha256": summary.get("render_sha256"),
        **extra_params,
    }
    parent_id = _pop_parent_id(gen_params)

    record_id = _ledger_insert(args.db_path, prompt, str(output_dir), gen_params, parent_id)
    print(f"[pipeline] ledger record: {record_id}")

    _post_process(record_id, output_dir, args)
    return record_id


def _post_process(record_id: str, render_dir: Path, args):
    """Run embed + tag and store results in ledger (non-fatal)."""
    tmp_dir = render_dir / ".pipeline_tmp"
    tmp_dir.mkdir(exist_ok=True)

    embed_json = _run_embed(render_dir, tmp_dir, args)
    if embed_json:
        _ledger_embed(args.db_path, record_id, embed_json)
        print(f"[pipeline] embedded -> {record_id}")

    tag_json = _run_tag(render_dir, tmp_dir, args)
    if tag_json:
        _ledger_tag(args.db_path, record_id, tag_json)
        print(f"[pipeline] tagged -> {record_id}")


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------

def watch_loop(args):
    drop_dir = Path(args.drop_dir)
    drop_dir.mkdir(parents=True, exist_ok=True)

    # Init ledger
    try:
        _ledger_init(args.db_path)
    except Exception as e:
        print(f"[pipeline] ledger init warning: {e}", file=sys.stderr)

    print(f"[pipeline] watching {drop_dir} (interval={args.interval}s)")
    print(f"[pipeline] output_base={args.output_base}")
    print(f"[pipeline] db={args.db_path}")

    state = _load_state(drop_dir)

    while True:
        for path in sorted(drop_dir.iterdir()):
            if path.name.startswith(".") or path.name == STATE_FILE:
                continue

            ext = path.suffix.lower()
            fhash = _file_hash(path)

            if fhash in state:
                continue  # already processed

            try:
                if ext in VRM_EXTS and path.stem.endswith("_r1"):
                    record_id = handle_vrm_r1(path, args, state, drop_dir)
                    if not record_id:
                        continue
                elif ext in VRM_EXTS:
                    record_id = handle_vrm(path, args)
                    _mark_done(drop_dir, state, fhash, record_id, stem=path.stem)
                    print(f"[pipeline] done: {path.name} -> {record_id}")
                    continue
                elif ext == ".prompt":
                    record_id = handle_prompt(path, args)
                elif ext in MESH_EXTS:
                    record_id = handle_mesh(path, args)
                else:
                    continue

                _mark_done(drop_dir, state, fhash, record_id)
                print(f"[pipeline] done: {path.name} -> {record_id}")

            except Exception as exc:
                print(f"[pipeline] ERROR processing {path.name}: {exc}", file=sys.stderr)
                # Mark as attempted (with error) so we don't retry immediately
                state[fhash] = {"error": str(exc), "ts": time.time()}
                _save_state(drop_dir, state)

        time.sleep(args.interval)


# ---------------------------------------------------------------------------
# One-shot mode (process a single file directly)
# ---------------------------------------------------------------------------

def run_once(path: Path, args):
    ext = path.suffix.lower()
    if ext in VRM_EXTS and path.stem.endswith("_r1"):
        drop_dir = path.parent
        state = _load_state(drop_dir)
        return handle_vrm_r1(path, args, state, drop_dir)
    elif ext in VRM_EXTS:
        return handle_vrm(path, args)
    elif ext == ".prompt":
        return handle_prompt(path, args)
    elif ext in MESH_EXTS:
        return handle_mesh(path, args)
    else:
        print(f"[pipeline] unknown file type: {ext}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="VRM/Object pipeline: watch drop dir → generate → render → ledger"
    )
    # Mode
    parser.add_argument("--file", default=None,
                        help="Process a single file instead of watching a directory")
    parser.add_argument("--drop-dir", default="./drop",
                        help="Directory to watch for trigger files (default: ./drop)")

    # Paths
    home = os.environ.get("HOME", "/tmp")
    parser.add_argument("--output-base", default="./output",
                        help="Base directory for render outputs (default: ./output)")
    parser.add_argument("--db-path",
                        default=str(Path(home) / ".vrm-pipeline" / "ledger.db"),
                        help="Path to ledger SQLite DB")
    parser.add_argument("--blender-path", default="blender",
                        help="Path to Blender binary (default: blender)")

    # Render options
    parser.add_argument("--golden-dir", default=None,
                        help="Golden render directory for eval-A pHash comparison")
    parser.add_argument("--scene-context", default=None,
                        help="Terrain .blend for object context render")
    parser.add_argument("--resolution", type=int, default=768)

    # Generate options
    parser.add_argument("--gen-model", default="gemini-2.5-flash",
                        help="Gemini model for generate.py (default: gemini-2.5-flash)")
    parser.add_argument("--gen-retries", type=int, default=3)

    # Embed/tag
    parser.add_argument("--embed-model", default="dinov2-small",
                        choices=["dinov2-small", "clip"])
    parser.add_argument("--no-embed", action="store_true",
                        help="Skip embed.py stage")
    parser.add_argument("--no-tag", action="store_true",
                        help="Skip tag.py stage")

    # Prompt enrichment
    parser.add_argument("--enrich-prompt", action="store_true",
                        help="Enrich prompts using suggest.py before generation")

    # Watch
    parser.add_argument("--interval", type=int, default=3,
                        help="Poll interval in seconds (default: 3)")

    args = parser.parse_args()
    args.db_path = Path(args.db_path)
    args.output_base = Path(args.output_base)

    if args.file:
        record_id = run_once(Path(args.file).resolve(), args)
        print(json.dumps({"record_id": record_id}))
    else:
        watch_loop(args)


if __name__ == "__main__":
    main()
