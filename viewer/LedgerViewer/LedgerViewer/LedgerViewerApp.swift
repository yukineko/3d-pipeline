import SwiftUI

/// Entry point for the VRM Ledger Tree Viewer — a read-only macOS app (arm64)
/// that visualizes the `~/.vrm-pipeline/ledger.db` derivation forest as a tree.
///
/// This is the T1 scaffold: it stands up a buildable, launchable window that
/// later tasks (T2 read layer, T3 tidy-tree rendering) hang their UI on.
@main
struct LedgerViewerApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
                .frame(minWidth: 720, minHeight: 480)
        }
        .windowStyle(.titleBar)
    }
}
