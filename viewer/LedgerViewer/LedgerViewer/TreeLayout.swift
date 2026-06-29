import CoreGraphics
import Foundation

/// A node placed in 2D space by the layout pass.
struct LayoutNode: Identifiable {
    let id: String
    let record: LedgerRecord
    let center: CGPoint
    let depth: Int
}

/// Result of laying out a `LedgerForest`.
struct LaidOutForest {
    var nodes: [LayoutNode]
    var centerByID: [String: CGPoint]
    var edges: [(from: String, to: String)]
    var size: CGSize
}

/// Pure geometry for interactive node placement: per-node drag offsets and
/// marquee (drag-box) selection. Deliberately SwiftUI-free so it can be unit
/// tested headlessly (Tests/main.swift) alongside the layout pass.
enum NodeGeometry {
    /// Normalized rectangle spanning two corner points, regardless of drag
    /// direction (so a bottom-right→top-left drag yields the same box).
    static func rect(from a: CGPoint, to b: CGPoint) -> CGRect {
        CGRect(x: min(a.x, b.x), y: min(a.y, b.y),
               width: abs(a.x - b.x), height: abs(a.y - b.y))
    }

    /// On-screen center = layout center + committed manual offset. (Live in-flight
    /// drag is layered on by the caller.)
    static func center(base: CGPoint, offset: CGSize?) -> CGPoint {
        guard let offset else { return base }
        return CGPoint(x: base.x + offset.width, y: base.y + offset.height)
    }

    /// IDs whose node box (centered at `center`, sized `nodeSize`) intersects the
    /// marquee rectangle.
    static func hits(marquee: CGRect,
                     centers: [(id: String, center: CGPoint)],
                     nodeSize: CGSize) -> [String] {
        centers.compactMap { item in
            let frame = CGRect(x: item.center.x - nodeSize.width / 2,
                               y: item.center.y - nodeSize.height / 2,
                               width: nodeSize.width, height: nodeSize.height)
            return marquee.intersects(frame) ? item.id : nil
        }
    }
}

/// Tidy tree layout (Reingold–Tilford style, simplified).
///
/// Post-order walk: each leaf claims the next free column; each internal node is
/// centered over its children. This guarantees columns never collide, so nodes
/// never overlap. Forest roots are laid out left-to-right sharing one column
/// counter, so independent trees sit side by side without overlapping.
enum TreeLayout {
    static func layout(
        forest: LedgerForest,
        nodeSize: CGSize,
        hSpacing: CGFloat = 28,
        vSpacing: CGFloat = 56
    ) -> LaidOutForest {
        var columnByID: [String: CGFloat] = [:]
        var depthByID: [String: Int] = [:]
        var nextLeafColumn: CGFloat = 0
        var maxDepth = 0

        // Children ordered oldest-first (timestamp, then id) to match ledger order.
        func sortedChildren(_ id: String) -> [LedgerRecord] {
            forest.children(of: id).sorted {
                $0.timestamp == $1.timestamp ? $0.id < $1.id : $0.timestamp < $1.timestamp
            }
        }

        func assign(_ record: LedgerRecord, depth: Int) {
            depthByID[record.id] = depth
            maxDepth = max(maxDepth, depth)
            let kids = sortedChildren(record.id)
            if kids.isEmpty {
                columnByID[record.id] = nextLeafColumn
                nextLeafColumn += 1
            } else {
                for kid in kids { assign(kid, depth: depth + 1) }
                let first = columnByID[kids.first!.id] ?? 0
                let last = columnByID[kids.last!.id] ?? first
                columnByID[record.id] = (first + last) / 2
            }
        }

        let roots = forest.roots.sorted {
            $0.timestamp == $1.timestamp ? $0.id < $1.id : $0.timestamp < $1.timestamp
        }
        for root in roots { assign(root, depth: 0) }

        let columnWidth = nodeSize.width + hSpacing
        let rowHeight = nodeSize.height + vSpacing

        func center(forID id: String) -> CGPoint {
            let col = columnByID[id] ?? 0
            let depth = depthByID[id] ?? 0
            return CGPoint(
                x: col * columnWidth + nodeSize.width / 2,
                y: CGFloat(depth) * rowHeight + nodeSize.height / 2
            )
        }

        var centerByID: [String: CGPoint] = [:]
        var nodes: [LayoutNode] = []
        for record in forest.records {
            let c = center(forID: record.id)
            centerByID[record.id] = c
            nodes.append(LayoutNode(id: record.id, record: record, center: c, depth: depthByID[record.id] ?? 0))
        }

        var edges: [(from: String, to: String)] = []
        let ids = Set(forest.records.map(\.id))
        for record in forest.records {
            if let pid = record.parentID, ids.contains(pid) {
                edges.append((from: pid, to: record.id))
            }
        }

        let width = max(nextLeafColumn, 1) * columnWidth
        let height = CGFloat(maxDepth + 1) * rowHeight
        return LaidOutForest(nodes: nodes, centerByID: centerByID, edges: edges,
                             size: CGSize(width: width, height: height))
    }
}
