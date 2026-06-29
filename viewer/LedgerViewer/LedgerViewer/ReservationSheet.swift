import AppKit
import SwiftUI

/// Sheet to reserve a child generation from a selected parent record.
///
/// This is the viewer's *only* write path, and it is deliberately indirect: it
/// never touches SQLite. It shells out to the official `ledger reserve` CLI,
/// which appends a new pending row (status = "reserved"). The DB connection in
/// `LedgerStore` stays `SQLITE_OPEN_READONLY`; the new node then appears live
/// via `LedgerWatcher`.
struct ReservationSheet: View {
    /// The parent record the new generation derives from.
    let parent: LedgerRecord
    /// Called after a successful `ledger reserve` (passes the new record id).
    var onReserved: ((String) -> Void)? = nil

    @Environment(\.dismiss) private var dismiss

    @State private var prompt: String = ""
    @State private var imagePath: String? = nil
    @State private var changeMode = false
    @State private var vroidEdit = false

    @State private var submitting = false
    @State private var errorMessage: String? = nil

    private var canSubmit: Bool {
        !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !submitting
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("新規生成を予約 (from \(parent.shortID))")
                .font(.headline)

            VStack(alignment: .leading, spacing: 4) {
                Text("プロンプト").font(.caption).foregroundStyle(.secondary)
                TextEditor(text: $prompt)
                    .font(.body)
                    .frame(minHeight: 110)
                    .overlay(
                        RoundedRectangle(cornerRadius: 6)
                            .strokeBorder(Color.secondary.opacity(0.3), lineWidth: 1)
                    )
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("入力画像 (任意)").font(.caption).foregroundStyle(.secondary)
                HStack(spacing: 8) {
                    Button("画像を選択…") { pickImage() }
                    if let imagePath {
                        Text((imagePath as NSString).lastPathComponent)
                            .font(.system(.caption, design: .monospaced))
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Button {
                            self.imagePath = nil
                        } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.borderless)
                            .foregroundStyle(.secondary)
                    } else {
                        Text("未選択").font(.caption).foregroundStyle(.tertiary)
                    }
                    Spacer(minLength: 0)
                }
            }

            VStack(alignment: .leading, spacing: 4) {
                Toggle("change モード", isOn: $changeMode)
                Toggle("vroid-edit モード", isOn: $vroidEdit)
            }
            .toggleStyle(.checkbox)

            if let errorMessage {
                Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Divider()

            HStack {
                Spacer()
                Button("キャンセル", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button {
                    submit()
                } label: {
                    if submitting {
                        ProgressView().controlSize(.small)
                    } else {
                        Text("予約")
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!canSubmit)
            }
        }
        .padding(20)
        .frame(width: 460)
    }

    // MARK: - Actions

    private func pickImage() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        if #available(macOS 11.0, *) {
            panel.allowedContentTypes = [.image]
        } else {
            panel.allowedFileTypes = ["png", "jpg", "jpeg", "webp", "heic"]
        }
        if panel.runModal() == .OK, let url = panel.url {
            imagePath = url.path
        }
    }

    private func submit() {
        errorMessage = nil
        submitting = true
        let promptText = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        let parentID = parent.id
        let chosenImage = imagePath
        let extraTags: [String] = {
            var t: [String] = []
            if changeMode { t.append("change") }
            if vroidEdit { t.append("vroid-edit") }
            return t
        }()

        DispatchQueue.global(qos: .userInitiated).async {
            let result = ReservationService.reserve(
                prompt: promptText,
                parentID: parentID,
                imagePath: chosenImage,
                modes: extraTags
            )
            DispatchQueue.main.async {
                submitting = false
                switch result {
                case .success(let newID):
                    onReserved?(newID)
                    dismiss()
                case .failure(let err):
                    errorMessage = err.message
                }
            }
        }
    }
}

/// A human-readable reservation failure (shown in the sheet's error alert).
struct ReservationError: Error, CustomStringConvertible {
    let message: String
    init(_ message: String) { self.message = message }
    var description: String { message }
}

/// Encapsulates the side-effecting reservation: copy the input image into the
/// pipeline's reservations dir, then run `ledger reserve`. No SQLite here.
enum ReservationService {
    /// Run `ledger reserve` for a new pending child. Returns the new record id
    /// (CLI stdout) on success, or a human-readable error string on failure.
    static func reserve(
        prompt: String,
        parentID: String,
        imagePath: String?,
        modes: [String]
    ) -> Result<String, ReservationError> {
        guard let ledger = resolveLedgerBinary() else {
            return .failure(ReservationError(
                "`ledger` CLI が見つかりません。LEDGER_BIN を設定するか、"
                + "vrm-pipeline/target/release/ledger をビルドしてください。"))
        }

        // Copy the chosen image into ~/.vrm-pipeline/reservations/ so the
        // reservation references a stable, pipeline-owned path.
        var copiedImagePath: String? = nil
        if let imagePath, !imagePath.isEmpty {
            switch copyImageToReservations(imagePath) {
            case .success(let p): copiedImagePath = p
            case .failure(let e): return .failure(e)
            }
        }

        // The CLI contract is limited to --prompt/--parent-id/--image-ref, so we
        // fold the optional change/vroid-edit modes into the prompt as a hint
        // rather than invent unsupported flags.
        let effectivePrompt = modes.isEmpty
            ? prompt
            : prompt + " [" + modes.joined(separator: ", ") + "]"

        var finalArgs = ["reserve", "--prompt", effectivePrompt, "--parent-id", parentID]
        if let copiedImagePath {
            finalArgs.append(contentsOf: ["--image-ref", copiedImagePath])
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: ledger.path)
        proc.arguments = ledger.leadingArgs + finalArgs
        let stdout = Pipe()
        let stderr = Pipe()
        proc.standardOutput = stdout
        proc.standardError = stderr

        do {
            try proc.run()
        } catch {
            return .failure(ReservationError("`ledger reserve` を起動できません: \(error.localizedDescription)"))
        }
        proc.waitUntilExit()

        let outData = stdout.fileHandleForReading.readDataToEndOfFile()
        let errData = stderr.fileHandleForReading.readDataToEndOfFile()
        let out = String(data: outData, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let err = String(data: errData, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""

        guard proc.terminationStatus == 0 else {
            let detail = err.isEmpty ? (out.isEmpty ? "exit \(proc.terminationStatus)" : out) : err
            return .failure(ReservationError("`ledger reserve` が失敗しました: \(detail)"))
        }
        guard !out.isEmpty else {
            return .failure(ReservationError("`ledger reserve` が id を出力しませんでした。"))
        }
        // The CLI prints the new id; take the last non-empty line defensively.
        let newID = out.split(whereSeparator: \.isNewline).last.map(String.init) ?? out
        return .success(newID)
    }

    // MARK: - Image copy

    private static var reservationsDir: String {
        (NSHomeDirectory() as NSString).appendingPathComponent(".vrm-pipeline/reservations")
    }

    private static func copyImageToReservations(_ source: String) -> Result<String, ReservationError> {
        let fm = FileManager.default
        guard fm.fileExists(atPath: source) else {
            return .failure(ReservationError("選択した画像が見つかりません: \(source)"))
        }
        do {
            try fm.createDirectory(atPath: reservationsDir, withIntermediateDirectories: true)
        } catch {
            return .failure(ReservationError("reservations ディレクトリを作成できません: \(error.localizedDescription)"))
        }
        let ext = (source as NSString).pathExtension
        let stamp = Int(Date().timeIntervalSince1970)
        let base = "reservation-\(stamp)" + (ext.isEmpty ? "" : ".\(ext)")
        let dest = (reservationsDir as NSString).appendingPathComponent(base)
        do {
            if fm.fileExists(atPath: dest) { try fm.removeItem(atPath: dest) }
            try fm.copyItem(atPath: source, toPath: dest)
        } catch {
            return .failure(ReservationError("画像をコピーできません: \(error.localizedDescription)"))
        }
        return .success(dest)
    }

    // MARK: - Binary resolution

    /// How to invoke the ledger CLI: a path to run plus any leading args.
    struct LedgerInvocation {
        let path: String
        let leadingArgs: [String]
    }

    /// Resolve the `ledger` binary robustly:
    /// (a) `LEDGER_BIN` env var,
    /// (b) common release build output relative to a repo root / ~/.vrm-pipeline,
    /// (c) PATH lookup via `/usr/bin/env ledger`.
    private static func resolveLedgerBinary() -> LedgerInvocation? {
        let fm = FileManager.default

        // (a) explicit override
        if let bin = ProcessInfo.processInfo.environment["LEDGER_BIN"],
           !bin.isEmpty, fm.isExecutableFile(atPath: bin) {
            return LedgerInvocation(path: bin, leadingArgs: [])
        }

        // (b) common build-output locations
        let home = NSHomeDirectory()
        var roots: [String] = []
        if let repoRoot = ProcessInfo.processInfo.environment["VRM_PIPELINE_ROOT"], !repoRoot.isEmpty {
            roots.append(repoRoot)
        }
        roots.append((home as NSString).appendingPathComponent(".vrm-pipeline"))
        roots.append((home as NSString).appendingPathComponent("src/3d-pipeline"))
        roots.append((home as NSString).appendingPathComponent("3d-pipeline"))

        var candidates: [String] = []
        for root in roots {
            candidates.append((root as NSString).appendingPathComponent("vrm-pipeline/target/release/ledger"))
            candidates.append((root as NSString).appendingPathComponent("target/release/ledger"))
            candidates.append((root as NSString).appendingPathComponent("bin/ledger"))
            candidates.append((root as NSString).appendingPathComponent("ledger"))
        }
        for c in candidates where fm.isExecutableFile(atPath: c) {
            return LedgerInvocation(path: c, leadingArgs: [])
        }

        // (c) PATH lookup via /usr/bin/env
        if fm.isExecutableFile(atPath: "/usr/bin/env") {
            return LedgerInvocation(path: "/usr/bin/env", leadingArgs: ["ledger"])
        }
        return nil
    }
}
