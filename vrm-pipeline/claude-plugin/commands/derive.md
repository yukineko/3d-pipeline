---
description: Derive a new generation from a parent ledger record (prompt / image / VRoid edit)
argument-hint: --parent-id <ID> --change <text> [--image <png>] [--vroid-edit] --emit-drop <dir> --name <stem>
allowed-tools: Bash(derive:*), Bash(python:*)
---

Run the `derive` CLI to spawn a child generation from an existing ledger record,
linking it into the lineage tree via `parent_id`.

```
derive $ARGUMENTS
```

Modes (all start from `--parent-id <RECORD_ID>`, the full UUID from `ledger list`):

- **Prompt derive** (default): `--change "<instruction>"` rewrites the parent's
  prompt via Gemini and emits a `<name>.prompt` + `<name>.params.json` pair.
- **Image derive**: add `--image <path>` to carry a reference image (written as
  `image_ref` in params). When `--image` is omitted the parent's render image is
  auto-resolved. The watcher routes this through the Hyper3D image-to-3D backend
  and converts the GLB to VRM.
- **VRoid edit**: add `--vroid-edit` to derive by editing the parent's base VRM
  instead of regenerating. The `--change` text (and `--image`, if given) is later
  turned into VRM parameter adjustments (expression / material color / height)
  and applied via the Blender VRM addon. Emits `{parent_id, vroid_edit, base_vrm,
  change}` params.

The emitted drop files are picked up by `vrm-watch`. Use `--emit-drop <dir>` to
write into the watched drop directory and `--name <stem>` for the file stem.
The DB defaults to `~/.vrm-pipeline/ledger.db`; pass `--db-path <path>` to target
another database. `--dry-run` prints the result without writing.

If no arguments were given, run `derive --help` and summarize the options.
