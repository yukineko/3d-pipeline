#!/usr/bin/env bash
# Run the viewer's headless read-layer test (Tests/run.sh) against a throwaway
# ledger DB built with the real `ledger` CLI, so the check never depends on (or
# touches) the user's ~/.vrm-pipeline/ledger.db. Any failure exits non-zero.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
DB="$TMP/ledger.db"

cargo build --quiet --manifest-path "$ROOT/vrm-pipeline/Cargo.toml" -p ledger
LEDGER="$ROOT/vrm-pipeline/target/debug/ledger"

"$LEDGER" --db "$DB" init
PARENT="$("$LEDGER" --db "$DB" insert --prompt "fixture parent")"
"$LEDGER" --db "$DB" insert --prompt "fixture child" --parent-id "$PARENT" >/dev/null

"$DIR/run.sh" "$DB"
