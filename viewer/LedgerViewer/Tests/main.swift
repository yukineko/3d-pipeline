import CoreGraphics
import Foundation

// Headless verification for the ledger read layer (T2).
//
// Compiled together with ../LedgerViewer/LedgerStore.swift (see run.sh). Reads
// the real ledger DB read-only and prints record/root counts plus a JSON-decode
// sanity check (adopted count). Exit non-zero on failure so it can gate CI.
//
// Usage: Tests/run.sh [path-to-ledger.db]

/// T11: interactive node geometry — per-node drag offset + marquee selection.
/// Exercised here (not in the SwiftUI view) because `NodeGeometry` is the pure,
/// headless-testable core; the gesture wiring itself can't be driven headlessly.
func TestNodeGeometry(nodeSize: CGSize) {   // capitalized so the TDD gate recognizes it as a test
    let centers: [(id: String, center: CGPoint)] = [
        ("A", CGPoint(x: 100, y: 100)),   // top row, left
        ("B", CGPoint(x: 400, y: 100)),   // top row, right
        ("C", CGPoint(x: 100, y: 400)),   // bottom row, left
    ]
    // (1) Marquee rect is direction-independent (drag any corner to any corner).
    let r1 = NodeGeometry.rect(from: CGPoint(x: 0, y: 50), to: CGPoint(x: 500, y: 150))
    let r2 = NodeGeometry.rect(from: CGPoint(x: 500, y: 150), to: CGPoint(x: 0, y: 50))
    precondition(r1 == r2, "marquee rect not direction-independent")
    // (2) A band across the top row selects A and B, never C.
    precondition(Set(NodeGeometry.hits(marquee: r1, centers: centers, nodeSize: nodeSize)) == ["A", "B"],
                 "marquee should select the top row only")
    // (3) A tight box around A selects A alone (boundary tightness).
    let rA = NodeGeometry.rect(from: CGPoint(x: 90, y: 90), to: CGPoint(x: 110, y: 110))
    precondition(Set(NodeGeometry.hits(marquee: rA, centers: centers, nodeSize: nodeSize)) == ["A"],
                 "tight marquee should select a single node")
    // (4) Committed offset shifts a node's on-screen center; nil offset is identity.
    precondition(NodeGeometry.center(base: CGPoint(x: 100, y: 100), offset: CGSize(width: 30, height: -20))
                 == CGPoint(x: 130, y: 80), "drag offset not applied")
    precondition(NodeGeometry.center(base: CGPoint(x: 5, y: 5), offset: nil) == CGPoint(x: 5, y: 5),
                 "nil offset must not move the node")
    print("geometry OK  : marquee selection + per-node drag offset")
}

/// T12: node positions persist across a restart (id + coordinates round-trip
/// through the JSON store, and a reopened store at the same path sees them).
func TestNodePositionStore() {
    let tmp = NSTemporaryDirectory() + "ledgerviewer-pos-\(getpid()).json"
    defer { try? FileManager.default.removeItem(atPath: tmp) }

    let store = NodePositionStore(path: tmp)
    precondition(store.load().isEmpty, "a fresh store must load empty")

    store.save(["A": CGSize(width: 30, height: -20), "B": CGSize(width: 0, height: 5)])
    let back = store.load()
    precondition(back["A"] == CGSize(width: 30, height: -20), "A offset not round-tripped")
    precondition(back["B"] == CGSize(width: 0, height: 5), "B offset not round-tripped")
    precondition(back.count == 2, "unexpected entry count \(back.count)")

    // Simulate a restart: a brand-new store at the same path must see the data.
    precondition(NodePositionStore(path: tmp).load()["A"] == CGSize(width: 30, height: -20),
                 "positions did not persist across reopen")
    print("positions OK : node offsets persist across reopen (\(back.count) ids)")
}

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

    TestNodeGeometry(nodeSize: nodeSize)
    TestNodePositionStore()
} catch {
    FileHandle.standardError.write(Data("verify_read: \(error)\n".utf8))
    exit(1)
}
