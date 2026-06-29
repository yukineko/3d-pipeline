---
description: VRM pipeline ledger CLI (init/insert/list/stats/similar/embed/tag/...)
argument-hint: <subcommand> [args]
allowed-tools: Bash(ledger:*)
---

Run the `ledger` CLI with the provided arguments and report the result.

```
ledger $ARGUMENTS
```

If no arguments were given, run `ledger --help` and summarize the available
subcommands. The DB defaults to `~/.vrm-pipeline/ledger.db`; pass `--db <path>`
to target a different database.

## Reservation queue subcommands

These support a two-phase flow: reserve a placeholder record first, generate
later, then fulfill it. Records carry a `status` column
(`reserved` | `generating` | `done`); legacy and freshly `insert`ed records are
`done`.

- `ledger reserve --prompt <TEXT> [--parent-id <ID>] [--image-ref <PATH>] [--generation-params <JSON>]`
  Inserts a placeholder record with `status='reserved'` and an empty `r0_ref`,
  auto-generating a UUID and RFC3339 timestamp. Prints the new record id as a
  single line on stdout (just the UUID) so a caller can capture it, e.g.
  `ID=$(ledger reserve --prompt "a red chair")`.

- `ledger pending [--json]`
  Lists records still awaiting generation (`status` in `reserved`/`generating`),
  ordered oldest first. With `--json`, prints a JSON array of
  `{id, prompt, parent_id, image_ref, generation_params, timestamp, status}`;
  without it, a plain table.

- `ledger set-status --id <ID> --status <VALUE>`
  Updates the `status` column for a record (e.g. mark it `generating` while a
  render runs).

- `ledger fulfill --id <ID> --r0-dir <PATH> [--asset-ref <JSON>] [--generation-params <JSON>]`
  Attaches the render output (`r0_ref`) to an existing reserved record and sets
  `status='done'`. Optionally also updates `asset_ref` and `generation_params`.

Typical flow:

```
ID=$(ledger reserve --prompt "a red chair")
ledger pending --json        # shows the reserved row
ledger fulfill --id "$ID" --r0-dir /path/to/r0   # flips status to done
```
