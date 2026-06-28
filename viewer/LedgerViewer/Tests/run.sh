#!/usr/bin/env bash
# Headless verification for the ledger read layer (T2).
# Compiles LedgerStore.swift + Tests/main.swift and runs it against a ledger DB.
#
#   Tests/run.sh [path-to-ledger.db]
#
# Defaults to ~/.vrm-pipeline/ledger.db. Exits non-zero on read/parse failure.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$DIR/../LedgerViewer"
OUT="$(mktemp -d)/ledger-verify"

swiftc -O "$APP/LedgerStore.swift" "$APP/TreeLayout.swift" "$DIR/main.swift" -lsqlite3 -o "$OUT"
"$OUT" "$@"
