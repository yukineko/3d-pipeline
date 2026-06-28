import AppKit
import SwiftUI

/// Read-only detail panel for the selected record: thumbnail, prompt, outcome
/// metrics, tags, asset/input paths (with Reveal in Finder), and the raw
/// generation params. Nothing here mutates the ledger.
struct InspectorView: View {
    let record: LedgerRecord

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                header
                Divider()
                promptSection
                outcomeSection
                if !record.tags.isEmpty { tagsSection }
                pathsSection
                paramsSection
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(minWidth: 280)
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            ThumbnailView(dir: record.r0Ref, cornerRadius: 8)
                .frame(width: 88, height: 88)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(record.shortID)
                        .font(.system(.headline, design: .monospaced))
                    if record.isAdopted {
                        Label("adopted", systemImage: "checkmark.seal.fill")
                            .labelStyle(.iconOnly)
                            .foregroundStyle(.green)
                    }
                }
                Text(record.id)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .textSelection(.enabled)
                Text(record.timestamp)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
    }

    private var promptSection: some View {
        section("Prompt") {
            Text(record.prompt.isEmpty ? "(no prompt)" : record.prompt)
                .font(.callout)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder private var outcomeSection: some View {
        section("Outcome") {
            VStack(alignment: .leading, spacing: 4) {
                row("adopted", record.isAdopted ? "yes" : "no")
                if let p = record.editDistPHash { row("edit dist (pHash)", String(format: "%.4f", p)) }
                if let e = record.editDistEmbed { row("edit dist (embed)", String(format: "%.4f", e)) }
                if record.editDistPHash == nil && record.editDistEmbed == nil && !record.isAdopted {
                    Text("no measured outcome yet").font(.caption).foregroundStyle(.tertiary)
                }
            }
        }
    }

    private var tagsSection: some View {
        section("Tags") {
            FlowChips(record.tags)
        }
    }

    @ViewBuilder private var pathsSection: some View {
        section("Files") {
            VStack(alignment: .leading, spacing: 6) {
                pathRow(label: "asset", path: record.assetPath)
                pathRow(label: "input image", path: record.imageRef)
                pathRow(label: "render (r0)", path: record.r0Ref.isEmpty ? nil : record.r0Ref)
                if let r1 = record.r1Ref, !r1.isEmpty { pathRow(label: "render (r1)", path: r1) }
            }
        }
    }

    @ViewBuilder private var paramsSection: some View {
        let pretty = record.prettyGenerationParams
        if pretty != "{}" && !pretty.isEmpty {
            section("Generation params") {
                Text(pretty)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.08)))
            }
        }
    }

    // MARK: - Pieces

    private func section<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased())
                .font(.caption2).bold()
                .foregroundStyle(.secondary)
            content()
        }
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Spacer()
            Text(value).font(.system(.caption, design: .monospaced))
        }
    }

    @ViewBuilder private func pathRow(label: String, path: String?) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Text(label).font(.caption).foregroundStyle(.secondary).frame(width: 78, alignment: .leading)
            if let path, !path.isEmpty {
                Text(path)
                    .font(.system(.caption2, design: .monospaced))
                    .textSelection(.enabled)
                    .lineLimit(3)
                    .truncationMode(.middle)
                Spacer(minLength: 0)
                if FileManager.default.fileExists(atPath: path) {
                    Button {
                        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
                    } label: { Image(systemName: "arrow.up.forward.app") }
                        .buttonStyle(.borderless)
                        .help("Reveal in Finder")
                }
            } else {
                Text("—").font(.caption2).foregroundStyle(.tertiary)
                Spacer(minLength: 0)
            }
        }
    }
}

/// Minimal wrapping chip row for tags.
private struct FlowChips: View {
    let items: [String]
    init(_ items: [String]) { self.items = items }

    var body: some View {
        WrappingHStack(items) { tag in
            Text(tag)
                .font(.caption2)
                .padding(.horizontal, 7).padding(.vertical, 3)
                .background(Capsule().fill(Color.accentColor.opacity(0.15)))
        }
    }
}

/// Tiny wrapping HStack good enough for short tag lists.
private struct WrappingHStack<Item: Hashable, Content: View>: View {
    let items: [Item]
    let content: (Item) -> Content
    init(_ items: [Item], @ViewBuilder content: @escaping (Item) -> Content) {
        self.items = items
        self.content = content
    }

    var body: some View {
        var width: CGFloat = 0
        var rows: [[Item]] = [[]]
        let maxWidth: CGFloat = 248
        for item in items {
            let est = CGFloat("\(item)".count) * 7 + 22
            if width + est > maxWidth, !rows[rows.count - 1].isEmpty {
                rows.append([]); width = 0
            }
            rows[rows.count - 1].append(item); width += est
        }
        return VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                HStack(spacing: 4) { ForEach(row, id: \.self) { content($0) } }
            }
        }
    }
}
