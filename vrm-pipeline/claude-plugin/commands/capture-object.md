---
description: Capture an object generated in the live Blender (MCP) session — export GLB, render object views, register it in the ledger as a done object record
argument-hint: [--stem <name>] [--output-base <dir>] [--db <path>] [--prompt <text>]
allowed-tools: Bash(ledger:*), Bash(/Applications/Blender.app/Contents/MacOS/Blender:*), mcp__blender__execute_blender_code, mcp__blender__get_scene_info, mcp__blender__get_hyper3d_status, mcp__blender__generate_hyper3d_model_via_text, mcp__blender__import_generated_asset
---

**Direct object capture**: take an object you generated in the **live Blender
(MCP) session** and register it in the ledger as a finished `asset_type:"object"`
record (GLB only, no VRM) — exporting the GLB, rendering the canonical object
views, and inserting a `done` row in one pass.

> This is the **direct-capture** counterpart to [`reserve-run.md`](reserve-run.md).
> `reserve-run` is the **queue consumer**: it drains viewer-created reservations
> (`ledger pending` → generate → `ledger fulfill`). This command has **no queue
> and no reservation** — the asset already exists in Blender, so it registers
> straight to `done` via `ledger insert` instead of `reserve`→`fulfill`.
>
> Like `reserve-run`, this writes **no new generation logic**. It only reuses the
> tools that already exist: the live Blender MCP session, the glTF exporter,
> `render/object.py`, and the `ledger` CLI.

## Defaults

- **output base**: `~/.vrm-pipeline/output` (renders land under `<base>/renders/<stem>/`)
- **ledger DB**: `~/.vrm-pipeline/ledger.db` (pass `--db <path>` to target another)
- **Blender binary**: `/Applications/Blender.app/Contents/MacOS/Blender`
- **stem**: a slug for this object (e.g. `vase_celadon_original`); it names the
  render dir and the GLB file.

## Flow

### 1. Have the object ready in Blender

Produce (or already have) the object in the **live Blender MCP session** and
leave it **selected** (the export uses the current selection):

- **Procedural** — build geometry with `mcp__blender__execute_blender_code`
  (bmesh, modifiers, materials). Select it and make it the active object.
- **Hyper3D Rodin** (only if enabled — check `get_hyper3d_status`) —
  `generate_hyper3d_model_via_text` / `..._via_images`, then
  `import_generated_asset`. Select the imported object.

This command does **not** prescribe *how* the object is made — only how it is
captured. (If Rodin is disabled, procedural generation is the path that needs no
Blender-side toggle.)

### 2. Export the selection as GLB

Run in the live session (`mcp__blender__execute_blender_code`). Export **only the
selected object** so the table / scene props are not captured:

```python
import bpy, os
stem = "<stem>"
base = os.path.expanduser("~/.vrm-pipeline/output/renders/" + stem)
gen_dir = os.path.join(base, "generated")
os.makedirs(gen_dir, exist_ok=True)
glb_path = os.path.join(gen_dir, stem + ".glb")

bpy.ops.export_scene.gltf(
    filepath=glb_path,
    export_format='GLB',
    use_selection=True,    # selected object only, not the whole scene
    export_apply=True,     # bake modifiers (subsurf etc.)
    export_yup=True,
)
print("GLB", glb_path, os.path.getsize(glb_path))
```

The GLB lands at `<output_base>/renders/<stem>/generated/<stem>.glb`.

### 3. Render the canonical object views (headless)

`render/object.py` renders the object view set (`obj_front` / `obj_side` /
`obj_top` / `obj_persp`, see its `OBJ_FACES`) as WebP plus a `manifest.json`:

```bash
BL="/Applications/Blender.app/Contents/MacOS/Blender"
STEM="<stem>"
OUTDIR="$HOME/.vrm-pipeline/output/renders/$STEM"
"$BL" --background --python render/object.py -- \
    --input "$OUTDIR/generated/$STEM.glb" \
    --output-dir "$OUTDIR" \
    --resolution 768
```

After this, `$OUTDIR` holds `obj_front.webp` … `obj_persp.webp` + `manifest.json`,
with `generated/<stem>.glb` alongside.

### 4. Register the object in the ledger (status = done)

The asset is already finished, so register it directly with `ledger insert`
(which writes `status='done'`) — **not** `reserve`/`fulfill`. Set `asset_ref` to
the **GLB alone** (object assets have no VRM) and mark `asset_type:"object"` in
`generation_params` so the viewer treats it as an object node:

```bash
STEM="<stem>"
OUTDIR="$HOME/.vrm-pipeline/output/renders/$STEM"
ledger insert \
  --prompt "<short description of the object>" \
  --r0-dir "$OUTDIR" \
  --asset-ref "{\"glb\":\"$OUTDIR/generated/$STEM.glb\"}" \
  --generation-params '{"asset_type":"object","source":"blender_mcp","method":"<procedural|hyper3d>"}'
```

`insert` prints the new record UUID. To hang the object under an existing node,
add `--parent-id <uuid>` (lineage). The ledger DB **auto-migrates on open**, so
no `ledger init` is needed beforehand even on an older DB.

## Result

- One `done` ledger record with `asset_type:"object"`, `asset_ref={"glb":...}`,
  and `r0_ref` pointing at the render dir.
- The viewer picks `obj_front` as the node thumbnail (Thumbnail.swift recognizes
  the object view names) and shows the ✓ done badge — no VRM is involved.

## Notes

- **object vs VRM**: object capture produces a **GLB only**. The VRM face/body
  view set and `{"vrm":...,"glb":...}` asset shape belong to the VRM flow; do not
  use them here.
- **Reuse, don't reinvent**: generation is the live Blender session; rendering is
  `render/object.py`; registration is the `ledger` CLI. This command is glue.
- For the **queued** path (viewer reserves a node → Claude Code drains it), use
  [`reserve-run.md`](reserve-run.md) instead — that consumes `ledger pending` and
  closes with `ledger fulfill`.
