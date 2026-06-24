---
description: VRM pipeline pHash distance CLI (dist)
argument-hint: [args]
allowed-tools: Bash(dist:*)
---

Run the `dist` CLI with the provided arguments and report the result.

```
dist $ARGUMENTS
```

Note: the `dist` binary is currently a stub (`fn main() {}`) — its CLI is not
yet implemented. If it produces no output, tell the user the command has not
been wired up yet rather than treating the empty output as success.
