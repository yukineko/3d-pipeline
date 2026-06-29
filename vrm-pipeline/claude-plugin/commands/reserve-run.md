---
description: Drain the ledger reservation queue — generate each pending reservation via the existing pipeline, then fulfill it
argument-hint: [--db <path>] [--watch-dir <dir>] [--output-base <dir>]
allowed-tools: Bash(ledger:*), Bash(python:*)
---

Run the **reservation queue** loop: pick up reservations created by the viewer
(`ledger reserve`) and turn each one into a real generation by driving the
**existing** generation stack — then mark it `done` so the viewer updates live.

> This command writes **no new generation logic**. It only orchestrates the
> tools that already exist: the `<stem>.prompt` + `<stem>.params.json` drop that
> `vrm-watch` / `pipeline.py` consume, or `pipeline.py` / `generate.py` /
> `render.vrm_convert` called directly. The mode (prompt / image / VRoid edit)
> is exactly the three-way branch in `pipeline.py handle_prompt()`.

## Loop

### 1. Fetch the queue

```bash
ledger pending --json
```

Returns a JSON array of pending reservations, each:

```jsonc
{
  "id": "<uuid>",
  "prompt": "<text>",
  "parent_id": "<uuid|null>",
  "image_ref": "<path|null>",
  "generation_params": { /* may carry vroid_edit, base_vrm, change, preset, ... */ },
  "timestamp": "<iso8601>",
  "status": "reserved"   // or "generating" if a prior run was interrupted
}
```

A freshly reserved record has an **empty `r0_ref`** (no outputs yet); that is
fine — `derive.py`'s `resolve_image_from_record` / `resolve_base_vrm_from_record`
already skip gracefully on empty `r0_ref`, so nothing has to be back-filled here.

### 2. For each reservation

**a. Mark it in-progress** so the viewer shows the ⚙ badge:

```bash
ledger set-status --id <id> --status generating
```

**b. Pick the mode** from `image_ref` / `generation_params` (same selection
`handle_prompt()` makes), then either emit the drop pair (preferred — lets
`vrm-watch` run the pipeline) or call the pipeline directly. The drop carries
`parent_id` + `image_ref` so the new record links into the lineage tree.

- **VRoid edit** — `generation_params.vroid_edit == true` (with `base_vrm`):
  emit `{vroid_edit: true, base_vrm, change, parent_id, image_ref?, preset?}`.
  Pipeline routes through `resolve_vrm_adjustments` → `render.vrm_edit.edit_vrm`.
- **Image → VRM** — `image_ref` present (no `vroid_edit`): emit
  `{parent_id, image_ref}`. Pipeline routes through Hyper3D image→GLB
  (`generate.py`) → `render.vrm_convert.glb_to_vrm`.
- **Prompt → object (non-VRM GLB)** — `generation_params.asset_type == "object"`
  (or neither VRoid-edit nor image is set): emit `{parent_id, asset_type: "object"}`.
  Pipeline Branch C routes through Gemini Blender codegen (`generate.py --prompt`),
  which produces an **object GLB** (`asset_type:"object"`, pipeline.py) — there is
  **no VRM** for this mode. Renders use the object view set (`render/object.py`:
  `obj_front` / `obj_persp` / `obj_side` / `obj_top` / `context_front` / `context_persp`),
  not the VRM face/body views.

Emit the drop (let `stem` be a slug of the reservation, e.g. its id):

```bash
# write <stem>.prompt (the reservation's prompt text)
# write <stem>.params.json (mode params above, with parent_id + image_ref)
# then drop both into the watched dir so vrm-watch picks them up
cp <stem>.prompt <stem>.params.json <watch-dir>/
```

Or invoke the pipeline directly instead of going through the watcher:

```bash
python pipeline.py <stem>.prompt --output-base <output-base> --db-path <db>
```

Do **not** hand-roll generation — reuse `pipeline.py` / `generate.py` /
`render.vrm_convert` exactly as above.

**c. Fulfill** once the pipeline finishes and the renders exist at
`{output_base}/renders/{stem}/` (+ `manifest.json`). The `--asset-ref` shape
depends on `asset_type`:

- **VRM** (VRoid-edit / image→VRM): 7 face/body WebP views (`face_front` /
  `body_front` / ...), final VRM at `{output_base}/renders/{stem}/generated/{stem}.vrm`:

  ```bash
  ledger fulfill --id <id> \
    --r0-dir <output_base>/renders/<stem> \
    --asset-ref '{"vrm":"<.../generated/<stem>.vrm>","glb":"<.../model.glb>"}' \
    --generation-params '<gen_params json from the pipeline run>'
  ```

- **Object** (`asset_type == "object"`): object-view WebP renders (`obj_front` /
  `obj_persp` / ...) and a **GLB-only** asset (no VRM) — set `asset-ref` to the
  GLB alone:

  ```bash
  ledger fulfill --id <id> \
    --r0-dir <output_base>/renders/<stem> \
    --asset-ref '{"glb":"<.../generated/<stem>.glb>"}' \
    --generation-params '<gen_params json, includes asset_type:"object">'
  ```

`fulfill` attaches the outputs and flips `status` → `done`. The viewer thumbnail
loader recognizes both view sets, so object nodes get a real thumbnail (it prefers
`obj_front` / `obj_persp`).

**d. On failure** (pipeline error, Blender crash, no renders produced):

```bash
ledger set-status --id <id> --status failed
```

Then continue to the next reservation — one failure must not stall the queue.

### 3. Live badges

The viewer live-refreshes off the SQLite change-counter, so each node's badge
moves **reserved (⏳) → generating (⚙) → done (✓)** (or **failed**)
automatically as you step through the loop — no manual refresh needed.

## Notes

- The DB defaults to `~/.vrm-pipeline/ledger.db`; pass `--db <path>` to target
  another database. Keep the `--db` you read `pending` from consistent with the
  `set-status` / `fulfill` calls.
- Reservations are created by the **viewer** (select a parent node → 予約 sheet →
  `ledger reserve --prompt <text> [--parent-id <id>] [--image-ref <path>]`),
  which appends an append-only pending row. This command is the consumer side.
- If `ledger pending --json` returns `[]`, there is nothing to do — report that
  the queue is empty and stop.
