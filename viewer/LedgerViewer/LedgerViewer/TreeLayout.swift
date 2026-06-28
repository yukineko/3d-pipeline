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
