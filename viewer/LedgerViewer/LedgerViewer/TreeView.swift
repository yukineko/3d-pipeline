import SwiftUI

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

    private let nodeSize = CGSize(width: 156, height: 84)

    @State private var committedZoom: CGFloat = 1
    @GestureState private var pinchZoom: CGFloat = 1
    @State private var committedPan: CGSize = .zero
    @GestureState private var dragPan: CGSize = .zero

    private var zoom: CGFloat { max(0.2, min(3.0, committedZoom * pinchZoom)) }

    var body: some View {
        let laid = TreeLayout.layout(forest: forest, nodeSize: nodeSize)

        GeometryReader { _ in
            ZStack(alignment: .topLeading) {
                edges(laid)
                ForEach(laid.nodes) { node in
                    NodeCardView(
                        record: node.record,
                        isSelected: selectedID == node.id,
                        isMatch: filter?.matched.contains(node.id) ?? false,
                        isDimmed: filter.map { !$0.isVisible(node.id) } ?? false
                    )
                    .frame(width: nodeSize.width, height: nodeSize.height)
                    .position(node.center)
                    .onTapGesture { selectedID = node.id }
                }
            }
            .frame(width: laid.size.width, height: laid.size.height, alignment: .topLeading)
            .scaleEffect(zoom)
            .offset(x: committedPan.width + dragPan.width,
                    y: committedPan.height + dragPan.height)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            .contentShape(Rectangle())
            .gesture(
                DragGesture()
                    .updating($dragPan) { value, state, _ in state = value.translation }
                    .onEnded { value in
                        committedPan.width += value.translation.width
                        committedPan.height += value.translation.height
                    }
            )
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
    }

    private func edges(_ laid: LaidOutForest) -> some View {
        Canvas { context, _ in
            for edge in laid.edges {
                guard let parent = laid.centerByID[edge.from],
                      let child = laid.centerByID[edge.to] else { continue }
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

    private var borderColor: Color {
        if isSelected { return .accentColor }
        if isMatch { return .orange }
        return .secondary.opacity(0.3)
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
                    if record.isAdopted {
                        Image(systemName: "checkmark.seal.fill")
                            .font(.caption2)
                            .foregroundStyle(.green)
                    }
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
                .strokeBorder(borderColor, lineWidth: (isSelected || isMatch) ? 2 : 1)
        )
        .shadow(color: .black.opacity(0.08), radius: 2, y: 1)
        .opacity(isDimmed ? 0.22 : 1)
    }
}
