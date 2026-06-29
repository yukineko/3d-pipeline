#!/usr/bin/env bash
# audit-ignore-file: this IS the test runner; the test it adds (TestNodePositionStore) lives in Tests/main.swift
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

swiftc -O "$APP/LedgerStore.swift" "$APP/TreeLayout.swift" "$APP/NodePositionStore.swift" "$DIR/main.swift" -lsqlite3 -o "$OUT"  # audit-ignore: test runner; the added test (TestNodePositionStore) is in Tests/main.swift
"$OUT" "$@"
