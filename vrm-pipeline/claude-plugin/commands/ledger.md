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
