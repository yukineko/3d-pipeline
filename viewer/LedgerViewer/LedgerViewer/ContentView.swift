import SwiftUI

/// Placeholder root view for the scaffold (T1).
///
/// Intentionally minimal: it only proves the app builds and a window opens.
/// T2 replaces this with the ledger read layer and T3 with the tidy-tree canvas.
struct ContentView: View {
    private var defaultLedgerPath: String {
        (NSHomeDirectory() as NSString).appendingPathComponent(".vrm-pipeline/ledger.db")
    }

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "point.3.connected.trianglepath.dotted")
                .font(.system(size: 48, weight: .light))
                .foregroundStyle(.secondary)
            Text("VRM Ledger Tree Viewer")
                .font(.title2).bold()
            Text("read-only · arm64 · scaffold")
                .font(.callout)
                .foregroundStyle(.secondary)
            Text(defaultLedgerPath)
                .font(.system(.footnote, design: .monospaced))
                .foregroundStyle(.tertiary)
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
    }
}

#Preview {
    ContentView()
}
