import SwiftUI

/// Root view: loads the ledger forest (read-only) and renders it as a tree.
///
/// Loading is a plain load-on-appear for now; T7 adds live refresh on DB change
/// and T9 hardens the empty/error states.
struct ContentView: View {
    @State private var forest: LedgerForest?
    @State private var loadError: String?
    @State private var selectedID: String?

    private let store = LedgerStore()

    var body: some View {
        Group {
            if let forest, !forest.records.isEmpty {
                TreeView(forest: forest, selectedID: $selectedID)
            } else {
                statusView
            }
        }
        .frame(minWidth: 720, minHeight: 480)
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
