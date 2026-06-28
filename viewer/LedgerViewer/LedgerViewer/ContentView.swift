import SwiftUI

/// Root view: loads the ledger forest (read-only) and renders it as a tree,
/// with explicit states for a missing DB, an empty ledger, and read errors so
/// the app never shows a blank window or crashes.
struct ContentView: View {
    enum LoadState {
        case loading
        case missingDB
        case empty
        case failed(String)
        case loaded(LedgerForest)
    }

    @State private var state: LoadState = .loading
    @State private var selectedID: String?
    @State private var searchText = ""
    @State private var adoptedOnly = false
    @StateObject private var watcher = LedgerWatcher(path: LedgerStore.defaultPath)

    private let store = LedgerStore()

    private var forest: LedgerForest? {
        if case .loaded(let f) = state { return f }
        return nil
    }

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
            if case .loaded(let forest) = state {
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
                Text("VRM Ledger Tree").font(.headline)
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
                .disabled(forest == nil)
                .help("Show only adopted records (and their ancestors)")
            }
            ToolbarItem(placement: .primaryAction) {
                Button { load() } label: { Image(systemName: "arrow.clockwise") }
                    .help("Reload from ledger")
            }
        }
        .onAppear(perform: load)
        .onChange(of: watcher.changeToken) { _, _ in load() }
    }

    @ViewBuilder private var statusView: some View {
        VStack(spacing: 12) {
            Image(systemName: statusIcon)
                .font(.system(size: 44, weight: .light))
                .foregroundStyle(statusIsError ? .orange : .secondary)
            Text(statusTitle)
                .font(.title3)
            Text(statusDetail)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            Text(store.path)
                .font(.system(.footnote, design: .monospaced))
                .foregroundStyle(.tertiary)
                .textSelection(.enabled)
            Button { load() } label: { Label("Reload", systemImage: "arrow.clockwise") }
                .padding(.top, 4)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
    }

    private var statusIsError: Bool { if case .failed = state { return true }; return false }

    private var statusIcon: String {
        switch state {
        case .loading: return "hourglass"
        case .missingDB: return "tray"
        case .empty: return "point.3.connected.trianglepath.dotted"
        case .failed: return "exclamationmark.triangle"
        case .loaded: return "checkmark"
        }
    }

    private var statusTitle: String {
        switch state {
        case .loading: return "Loading…"
        case .missingDB: return "No ledger yet"
        case .empty: return "Ledger is empty"
        case .failed: return "Couldn’t read the ledger"
        case .loaded: return ""
        }
    }

    private var statusDetail: String {
        switch state {
        case .loading: return "Reading the ledger database."
        case .missingDB:
            return "No ledger database was found. It appears here automatically once you generate your first VRM through the pipeline."
        case .empty:
            return "The ledger exists but has no records yet. The tree fills in as VRMs are generated."
        case .failed(let message): return message
        case .loaded: return ""
        }
    }

    private func load() {
        guard store.exists else { state = .missingDB; return }
        do {
            let forest = try store.loadForest()
            state = forest.records.isEmpty ? .empty : .loaded(forest)
        } catch {
            state = .failed("\(error)")
        }
    }
}

#Preview {
    ContentView()
}
