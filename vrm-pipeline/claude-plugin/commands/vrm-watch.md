---
description: Start the VRM file watcher (blender + ledger pipeline)
argument-hint: --watch-dir <dir> --output-dir <dir> --render-py <path> [opts]
allowed-tools: Bash(vrm-watch:*)
---

Start the `vrm-watch` watcher with the provided arguments.

```
vrm-watch $ARGUMENTS
```

This is a long-running process — run it in the background and report the PID so
the user can stop it later. Required flags: `--watch-dir`, `--output-dir`,
`--render-py`. If any required flag is missing from `$ARGUMENTS`, run
`vrm-watch --help`, show the usage, and ask the user for the missing values
instead of starting the watcher.
