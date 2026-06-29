import SwiftUI
import AppKit

/// Interactive derivation-tree canvas: draws the forest with elbow edges and
/// node cards, supporting pinch-zoom and drag-pan. Read-only.
/// Active search/filter result: which nodes matched, and which are kept for
/// context (ancestors of matches). Nil filter => show everything normally.
struct TreeFilter {
    let matched: Set<String>
    let context: Set<String>
    func isVisible(_ id: String) -> Bool { matched.contains(id) || context.contains(id) }
}

struct TreeView: View {
    let forest: LedgerForest
    @Binding var selectedID: String?
    var filter: TreeFilter?
    /// Invoked when the user picks "生成を予約" from a node's context menu.
    var onReserve: ((LedgerRecord) -> Void)? = nil

    private let nodeSize = CGSize(width: 156, height: 84)

    @State private var committedZoom: CGFloat = 1
    @GestureState private var pinchZoom: CGFloat = 1
    @State private var committedPan: CGSize = .zero

    /// Per-node manual placement, in content coordinates. This is **ephemeral
    /// view state only** — node positions are never written back to the ledger
    /// (the DB stays read-only / append-only). Keyed by record id.
    @State private var nodeOffsets: [String: CGSize] = [:]
    /// Multi-selection for group move (driven by the marquee box). The single
    /// `selectedID` binding (Inspector) tracks the primary of this set.
    @State private var selection: Set<String> = []
    /// Live per-node (or per-group) drag in flight.
    @GestureState private var nodeDrag: NodeDragState? = nil
    /// Live marquee rectangle (content coords) while drag-selecting empty canvas.
    @GestureState private var marqueeRect: CGRect? = nil
    /// Scroll-wheel / two-finger-scroll → pan monitor (one-finger drag is now
    /// reserved for marquee selection, so panning moved to scroll).
    @State private var scrollMonitor: Any? = nil
    /// Only scroll-to-pan while the pointer is over the canvas, so scrolling the
    /// inspector/sidebar doesn't also pan the tree.
    @State private var pointerInside = false
    /// Persists `nodeOffsets` to disk so manual placements survive a restart.
    private let positionStore = NodePositionStore()

    private var zoom: CGFloat { max(0.2, min(3.0, committedZoom * pinchZoom)) }

    /// One node (or a whole selection) being dragged, plus the live translation.
    struct NodeDragState { var ids: Set<String>; var translation: CGSize }

    var body: some View {
        let laid = TreeLayout.layout(forest: forest, nodeSize: nodeSize)

        GeometryReader { _ in
            ZStack(alignment: .topLeading) {
                // Empty-canvas layer: drag = marquee select, tap = clear.
                Color.clear
                    .frame(width: laid.size.width, height: laid.size.height)
                    .contentShape(Rectangle())
                    .gesture(marqueeGesture(laid))
                    .onTapGesture { clearSelection() }

                edges(laid)

                ForEach(laid.nodes) { node in
                    NodeCardView(
                        record: node.record,
                        isSelected: selection.contains(node.id),
                        isMatch: filter?.matched.contains(node.id) ?? false,
                        isDimmed: filter.map { !$0.isVisible(node.id) } ?? false
                    )
                    .frame(width: nodeSize.width, height: nodeSize.height)
                    .position(displayCenter(node.id, base: node.center))
                    .onTapGesture { selectOnly(node.id) }
                    .gesture(nodeDragGesture(node.id))
                    .contextMenu {
                        Button {
                            selectOnly(node.id)
                            onReserve?(node.record)
                        } label: {
                            Label("生成を予約", systemImage: "plus.circle")
                        }
                    }
                }

                if let rect = marqueeRect {
                    Rectangle()
                        .fill(Color.accentColor.opacity(0.12))
                        .overlay(Rectangle().stroke(Color.accentColor, lineWidth: 1))
                        .frame(width: rect.width, height: rect.height)
                        .position(x: rect.midX, y: rect.midY)
                        .allowsHitTesting(false)
                }
            }
            .frame(width: laid.size.width, height: laid.size.height, alignment: .topLeading)
            .scaleEffect(zoom)
            .offset(x: committedPan.width, y: committedPan.height)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            .contentShape(Rectangle())
            .gesture(
                MagnificationGesture()
                    .updating($pinchZoom) { value, state, _ in state = value }
                    .onEnded { value in
                        committedZoom = max(0.2, min(3.0, committedZoom * value))
                    }
            )
        }
        .background(Color(nsColor: .textBackgroundColor))
        .overlay(alignment: .bottomTrailing) { zoomControls }
        .onHover { pointerInside = $0 }
        .onAppear {
            nodeOffsets = positionStore.load()   // restore saved placements
            installScrollPan()
        }
        .onDisappear { removeScrollPan() }
    }

    private func edges(_ laid: LaidOutForest) -> some View {
        Canvas { context, _ in
            for edge in laid.edges {
                guard let parentBase = laid.centerByID[edge.from],
                      let childBase = laid.centerByID[edge.to] else { continue }
                let parent = displayCenter(edge.from, base: parentBase)
                let child = displayCenter(edge.to, base: childBase)
                let start = CGPoint(x: parent.x, y: parent.y + nodeSize.height / 2)
                let end = CGPoint(x: child.x, y: child.y - nodeSize.height / 2)
                let midY = (start.y + end.y) / 2
                var path = Path()
                path.move(to: start)
                path.addLine(to: CGPoint(x: start.x, y: midY))
                path.addLine(to: CGPoint(x: end.x, y: midY))
                path.addLine(to: end)
                let visible = filter.map { $0.isVisible(edge.from) && $0.isVisible(edge.to) } ?? true
                context.stroke(path, with: .color(.secondary.opacity(visible ? 0.55 : 0.12)),
                               style: StrokeStyle(lineWidth: 1.5, lineJoin: .round))
            }
        }
        .frame(width: laid.size.width, height: laid.size.height)
        .allowsHitTesting(false)
    }

    // MARK: - Selection & per-node movement

    /// A node's on-screen center = layout center + committed offset + live drag.
    private func displayCenter(_ id: String, base: CGPoint) -> CGPoint {
        var c = NodeGeometry.center(base: base, offset: nodeOffsets[id])
        if let d = nodeDrag, d.ids.contains(id) {
            c.x += d.translation.width
            c.y += d.translation.height
        }
        return c
    }

    /// Dragging a node that is part of a multi-selection moves the whole group;
    /// otherwise it moves just that node.
    private func movingIDs(for id: String) -> Set<String> {
        (selection.contains(id) && selection.count > 1) ? selection : [id]
    }

    private func selectOnly(_ id: String) {
        selection = [id]
        selectedID = id
    }

    private func clearSelection() {
        selection = []
        selectedID = nil
    }

    private func nodeDragGesture(_ id: String) -> some Gesture {
        DragGesture(minimumDistance: 4)
            .updating($nodeDrag) { value, state, _ in
                state = NodeDragState(ids: movingIDs(for: id), translation: value.translation)
            }
            .onEnded { value in
                for mid in movingIDs(for: id) {
                    var off = nodeOffsets[mid] ?? .zero
                    off.width += value.translation.width
                    off.height += value.translation.height
                    nodeOffsets[mid] = off
                }
                positionStore.save(nodeOffsets)   // persist so it survives restart
            }
    }

    private func marqueeGesture(_ laid: LaidOutForest) -> some Gesture {
        DragGesture(minimumDistance: 4)
            .updating($marqueeRect) { value, state, _ in
                state = NodeGeometry.rect(from: value.startLocation, to: value.location)
            }
            .onEnded { value in
                let box = NodeGeometry.rect(from: value.startLocation, to: value.location)
                selectNodes(in: box, laid: laid)
            }
    }

    private func selectNodes(in marquee: CGRect, laid: LaidOutForest) {
        let centers = laid.nodes.map { (id: $0.id, center: displayCenter($0.id, base: $0.center)) }
        let hits = NodeGeometry.hits(marquee: marquee, centers: centers, nodeSize: nodeSize)
        selection = Set(hits)
        selectedID = hits.sorted().first
    }

    // MARK: - Pan via scroll (one-finger drag is reserved for marquee select)

    private func installScrollPan() {
        guard scrollMonitor == nil else { return }   // idempotent: never leak a prior monitor
        scrollMonitor = NSEvent.addLocalMonitorForEvents(matching: .scrollWheel) { event in
            guard pointerInside else { return event }   // don't pan when scrolling elsewhere
            committedPan.width += event.scrollingDeltaX
            committedPan.height += event.scrollingDeltaY
            return event
        }
    }

    private func removeScrollPan() {
        if let monitor = scrollMonitor {
            NSEvent.removeMonitor(monitor)
            scrollMonitor = nil
        }
    }

    private var zoomControls: some View {
        HStack(spacing: 6) {
            Button { committedZoom = max(0.2, committedZoom - 0.2) } label: { Image(systemName: "minus.magnifyingglass") }
            Button { committedZoom = 1; committedPan = .zero } label: { Image(systemName: "1.magnifyingglass") }
            Button { committedZoom = min(3.0, committedZoom + 0.2) } label: { Image(systemName: "plus.magnifyingglass") }
        }
        .buttonStyle(.bordered)
        .padding(10)
    }
}

/// A single record's card: short id, prompt, and an adopted badge.
/// (T4 adds the r0_ref PNG thumbnail; the image well is reserved here.)
struct NodeCardView: View {
    let record: LedgerRecord
    let isSelected: Bool
    var isMatch: Bool = false
    var isDimmed: Bool = false

    private var isPending: Bool { record.isPending }

    private var borderColor: Color {
        if isSelected { return .accentColor }
        if isPending { return .orange }   // amber: reserved/generating stand out
        if isMatch { return .orange }
        return .secondary.opacity(0.3)
    }

    private var borderWidth: CGFloat {
        if isSelected || isMatch || isPending { return 2 }
        return 1
    }

    /// Status badge next to the id: ⏳ reserved, ⚙ generating, ✓ adopted/done,
    /// ✗ failed. (`done` keeps the existing adopted ✓ behaviour.)
    @ViewBuilder private var statusBadge: some View {
        switch record.statusKind {
        case .reserved:
            Image(systemName: "hourglass")
                .font(.caption2)
                .foregroundStyle(.orange)
        case .generating:
            Image(systemName: "gearshape.fill")
                .font(.caption2)
                .foregroundStyle(.orange)
        case .failed:
            Image(systemName: "xmark.octagon.fill")
                .font(.caption2)
                .foregroundStyle(.red)
        case .done:
            if record.isAdopted {
                Image(systemName: "checkmark.seal.fill")
                    .font(.caption2)
                    .foregroundStyle(.green)
            }
        }
    }

    var body: some View {
        HStack(spacing: 8) {
            ThumbnailView(dir: record.r0Ref)
                .frame(width: 48, height: 48)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 4) {
                    Text(record.shortID)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                    statusBadge
                }
                Text(record.prompt.isEmpty ? "(no prompt)" : record.prompt)
                    .font(.caption)
                    .lineLimit(2)
                    .foregroundStyle(.primary)
            }
            Spacer(minLength: 0)
        }
        .padding(8)
        .background(
            RoundedRectangle(cornerRadius: 9)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 9)
                .strokeBorder(borderColor, lineWidth: borderWidth)
        )
        .shadow(color: .black.opacity(0.08), radius: 2, y: 1)
        .opacity(isDimmed ? 0.22 : 1)
    }
}
