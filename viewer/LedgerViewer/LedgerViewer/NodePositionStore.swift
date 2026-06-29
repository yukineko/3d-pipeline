import CoreGraphics
import Foundation

/// Persists manual node placements — the per-node drag **offset** from the
/// computed tree layout — so the canvas restores to the same positions after a
/// restart.
///
/// This is **viewer-local UI state**, written to its own JSON file next to the
/// ledger DB (`~/.vrm-pipeline/viewer_node_positions.json`). It is never written
/// into the ledger DB, so that store's read-only / append-only guarantee is
/// untouched.
///
/// Persisting the offset (delta from layout) rather than an absolute position
/// keeps a node where the user left it while still letting the auto-layout place
/// newly added nodes.
struct NodePositionStore {
    /// One node's manual offset (delta from its computed layout center).
    struct Offset: Codable, Equatable {
        var dx: Double
        var dy: Double
    }

    let path: String

    static var defaultPath: String {
        (NSHomeDirectory() as NSString)
            .appendingPathComponent(".vrm-pipeline/viewer_node_positions.json")
    }

    init(path: String = NodePositionStore.defaultPath) { self.path = path }

    /// Load saved offsets keyed by record id. A missing or unreadable/corrupt
    /// file yields an empty map (best effort — positions simply fall back to the
    /// computed layout).
    func load() -> [String: CGSize] {
        guard let data = FileManager.default.contents(atPath: path),
              let decoded = try? JSONDecoder().decode([String: Offset].self, from: data)
        else { return [:] }
        return decoded.mapValues { CGSize(width: $0.dx, height: $0.dy) }
    }

    /// Persist offsets as `{ "<node-id>": { "dx": .., "dy": .. } }`. Best effort:
    /// write failures are swallowed (a lost layout save must never break the
    /// read-only viewer).
    func save(_ offsets: [String: CGSize]) {
        let encodable = offsets.mapValues { Offset(dx: Double($0.width), dy: Double($0.height)) }
        guard let data = try? JSONEncoder().encode(encodable) else { return }
        let dir = (path as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        try? data.write(to: URL(fileURLWithPath: path))
    }
}
