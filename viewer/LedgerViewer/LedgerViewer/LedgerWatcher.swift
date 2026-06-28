import Combine
import Foundation

/// Watches the ledger DB and bumps `changeToken` when it changes, so the view
/// reloads as the pipeline writes new records.
///
/// Two complementary signals (the viewer only reads, so this is best-effort):
///  - A DispatchSource on the *directory*: SQLite's rollback journal is
///    created/deleted on every transaction, which fires directory write/rename
///    events — immediate notification of inserts.
///  - An mtime+size poll backstop every ~1.5s: catches in-place writes (e.g.
///    WAL mode) that don't change the directory.
/// Both feed a 300ms debounce so a journal create+delete burst reloads once.
final class LedgerWatcher: ObservableObject {
    @Published private(set) var changeToken: Int = 0

    private let path: String
    private var dirSource: DispatchSourceFileSystemObject?
    private var dirFD: Int32 = -1
    private var pollTimer: DispatchSourceTimer?
    private var lastSignature: String = ""
    private var pendingReload = false

    init(path: String) {
        self.path = path
        lastSignature = Self.signature(of: path)
        startDirectoryWatch()
        startPoll()
    }

    deinit { stop() }

    func stop() {
        dirSource?.cancel()
        dirSource = nil
        pollTimer?.cancel()
        pollTimer = nil
    }

    private func startDirectoryWatch() {
        let dir = (path as NSString).deletingLastPathComponent
        let fd = open(dir, O_EVTONLY)
        guard fd >= 0 else { return }
        dirFD = fd
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd, eventMask: [.write, .rename, .delete], queue: .main)
        source.setEventHandler { [weak self] in self?.scheduleReload() }
        source.setCancelHandler { [weak self] in
            if let fd = self?.dirFD, fd >= 0 { close(fd) }
            self?.dirFD = -1
        }
        source.resume()
        dirSource = source
    }

    private func startPoll() {
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 1.5, repeating: 1.5)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            let sig = Self.signature(of: self.path)
            if sig != self.lastSignature {
                self.lastSignature = sig
                self.scheduleReload()
            }
        }
        timer.resume()
        pollTimer = timer
    }

    private func scheduleReload() {
        guard !pendingReload else { return }
        pendingReload = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
            guard let self else { return }
            self.pendingReload = false
            self.lastSignature = Self.signature(of: self.path)
            self.changeToken &+= 1
        }
    }

    private static func signature(of path: String) -> String {
        guard FileManager.default.fileExists(atPath: path) else { return "absent" }
        // SQLite header change-counter (offset 24, 4-byte big-endian) bumps on
        // every commit — even when free-page reuse leaves file size/mtime
        // unchanged. This is the dependable signal; size/mtime are belt-and-braces.
        var changeCounter: UInt32 = 0
        if let handle = try? FileHandle(forReadingFrom: URL(fileURLWithPath: path)) {
            defer { try? handle.close() }
            if let data = try? handle.read(upToCount: 28), data.count >= 28 {
                changeCounter = data.subdata(in: 24..<28).reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
            }
        }
        let attrs = try? FileManager.default.attributesOfItem(atPath: path)
        let size = (attrs?[.size] as? Int) ?? -1
        let mod = ((attrs?[.modificationDate] as? Date)?.timeIntervalSince1970) ?? 0
        return "\(changeCounter):\(size):\(mod)"
    }
}
