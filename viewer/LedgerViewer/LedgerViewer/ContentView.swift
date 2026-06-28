import SwiftUI

/// Root view: loads the ledger forest (read-only) and renders it as a tree.
///
/// Loading is a plain load-on-appear for now; T7 adds live refresh on DB change
/// and T9 hardens the empty/error states.
struct ContentView: View {
    @State private var forest: LedgerForest?
    @State private var loadError: String?
    @State private var selectedID: String?
    @State private var searchText = ""
    @State private var adoptedOnly = false

    private let store = LedgerStore()

    private var selectedRecord: LedgerRecord? {
        guard let selectedID, let forest else { return nil }
        return forest.records.first { $0.id == selectedID }
    }

    private var filterActive: Bool { !searchText.trimmingCharacters(in: .whitespaces).isEmpty || adoptedOnly }

    /// Build the active filter: records matching search/adopted, plus their
    /// ancestors (kept for context so matches aren't orphaned in the tree).
    private var treeFilter: TreeFilter? {
        guard let forest, filterActive else { return nil }
        let q = searchText.trimmingCharacters(in: .whitespaces).lowercased()
        func matches(_ r: LedgerRecord) -> Bool {
            if adoptedOnly && !r.isAdopted { return false }
            if q.isEmpty { return true }
            return r.prompt.lowercased().contains(q)
                || r.id.lowercased().hasPrefix(q)
                || r.tags.contains { $0.lowercased().contains(q) }
        }
        let byID = Dictionary(uniqueKeysWithValues: forest.records.map { ($0.id, $0) })
        let matched = Set(forest.records.filter(matches).map(\.id))
        var context = Set<String>()
        for id in matched {
            var cur = byID[id]?.parentID
            while let pid = cur, byID[pid] != nil, !matched.contains(pid), !context.contains(pid) {
                context.insert(pid)
                cur = byID[pid]?.parentID
            }
        }
        return TreeFilter(matched: matched, context: context)
    }

    var body: some View {
        Group {
            if let forest, !forest.records.isEmpty {
                TreeView(forest: forest, selectedID: $selectedID, filter: treeFilter)
            } else {
                statusView
            }
        }
        .frame(minWidth: 720, minHeight: 480)
        .searchable(text: $searchText, placement: .toolbar, prompt: "Search prompt, id, or tag")
        .inspector(isPresented: .constant(selectedRecord != nil)) {
            if let record = selectedRecord {
                InspectorView(record: record)
                    .inspectorColumnWidth(min: 280, ideal: 320, max: 420)
            } else {
                Text("Select a node").foregroundStyle(.secondary)
            }
        }
        .toolbar {
            ToolbarItem(placement: .navigation) {
                Text("VRM Ledger Tree")
                    .font(.headline)
            }
            ToolbarItem(placement: .primaryAction) {
                if let forest {
                    Text("\(forest.records.count) records · \(forest.roots.count) roots")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            ToolbarItem(placement: .primaryAction) {
                Toggle(isOn: $adoptedOnly) {
                    Label("Adopted only", systemImage: adoptedOnly ? "checkmark.seal.fill" : "checkmark.seal")
                }
                .toggleStyle(.button)
                .help("Show only adopted records (and their ancestors)")
            }
            ToolbarItem(placement: .primaryAction) {
                Button { load() } label: { Image(systemName: "arrow.clockwise") }
                    .help("Reload from ledger")
            }
        }
        .onAppear(perform: load)
    }

    @ViewBuilder private var statusView: some View {
        VStack(spacing: 12) {
            Image(systemName: loadError == nil ? "point.3.connected.trianglepath.dotted" : "exclamationmark.triangle")
                .font(.system(size: 44, weight: .light))
                .foregroundStyle(.secondary)
            Text(loadError ?? "No records in ledger")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Text(store.path)
                .font(.system(.footnote, design: .monospaced))
                .foregroundStyle(.tertiary)
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
    }

    private func load() {
        guard store.exists else {
            loadError = "No ledger database found"
            forest = nil
            return
        }
        do {
            forest = try store.loadForest()
            loadError = nil
        } catch {
            loadError = "\(error)"
            forest = nil
        }
    }
}

#Preview {
    ContentView()
}
