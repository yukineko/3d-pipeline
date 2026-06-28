import CoreGraphics
import Foundation

// Headless verification for the ledger read layer (T2).
//
// Compiled together with ../LedgerViewer/LedgerStore.swift (see run.sh). Reads
// the real ledger DB read-only and prints record/root counts plus a JSON-decode
// sanity check (adopted count). Exit non-zero on failure so it can gate CI.
//
// Usage: Tests/run.sh [path-to-ledger.db]

let path = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : LedgerStore.defaultPath
let store = LedgerStore(path: path)

guard store.exists else {
    FileHandle.standardError.write(Data("verify_read: no ledger DB at \(path)\n".utf8))
    exit(2)
}

do {
    let records = try store.fetchAllRecords()
    let forest = LedgerForest(records: records)
    let adopted = records.filter(\.isAdopted).count
    let withParent = records.filter { $0.parentID != nil }.count
    let taggedSample = records.first(where: { !$0.tags.isEmpty })?.tags ?? []

    print("ledger: \(path)")
    print("records      : \(records.count)")
    print("roots        : \(forest.roots.count)")
    print("with parent  : \(withParent)")
    print("adopted      : \(adopted)  (JSON-decoded from outcome)")
    print("sample tags  : \(taggedSample)")

    // Invariant: every record is reachable as either a root or some node's child.
    let childCount = forest.childrenByParent.values.reduce(0) { $0 + $1.count }
    precondition(forest.roots.count + childCount == records.count,
                 "forest partition lost records")
    print("partition OK : roots(\(forest.roots.count)) + children(\(childCount)) == \(records.count)")

    // --- T3: layout invariants (no overlap, children below parents, parent centered) ---
    let nodeSize = CGSize(width: 156, height: 84)
    let laid = TreeLayout.layout(forest: forest, nodeSize: nodeSize)

    // (1) No two nodes share a center (no visual overlap of columns).
    var seen = Set<String>()
    for n in laid.nodes {
        let key = "\(Int(n.center.x.rounded())):\(Int(n.center.y.rounded()))"
        precondition(!seen.contains(key), "overlap: two nodes at \(key)")
        seen.insert(key)
    }
    // (2) Every child sits strictly below its parent; (3) parent x within child x-range.
    for (pid, kids) in forest.childrenByParent {
        guard let pc = laid.centerByID[pid] else { continue }
        let kidXs = kids.compactMap { laid.centerByID[$0.id]?.x }
        for kid in kids {
            guard let cc = laid.centerByID[kid.id] else { continue }
            precondition(cc.y > pc.y, "child \(kid.id) not below parent \(pid)")
        }
        if let lo = kidXs.min(), let hi = kidXs.max() {
            precondition(pc.x >= lo - 0.5 && pc.x <= hi + 0.5, "parent \(pid) not centered over children")
        }
    }
    print("layout OK    : \(laid.nodes.count) nodes, canvas \(Int(laid.size.width))x\(Int(laid.size.height)), no overlap")
} catch {
    FileHandle.standardError.write(Data("verify_read: \(error)\n".utf8))
    exit(1)
}
