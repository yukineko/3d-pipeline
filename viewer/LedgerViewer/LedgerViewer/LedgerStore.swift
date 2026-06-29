import Foundation
import SQLite3

// MARK: - Model

/// One ledger record: a generated VRM plus its lineage links.
///
/// Mirrors the `records` table in `crates/ledger/src/schema.rs`. The four JSON
/// columns (`generation_params`, `outcome`, `asset_ref`, `derived`) are kept as
/// raw strings and decoded lazily via the helpers below — the viewer is
/// read-only and tolerant of schema/JSON it does not recognise.
struct LedgerRecord: Identifiable, Hashable {
    let id: String
    let timestamp: String
    let prompt: String
    let generationParams: String   // raw JSON object
    let r0Ref: String              // render dir (PNG faces) — thumbnails in T4
    let r1Ref: String?             // edited render dir
    let outcome: String            // raw JSON: { adopted, edit_dist_phash, edit_dist_embed }
    let assetRef: String           // raw JSON: { vrm/glb paths }
    let derived: String            // raw JSON: { embed, tag }
    let parentID: String?          // lineage parent; nil/dangling => root
    let imageRef: String?          // input image path
    let status: String             // reserved/generating/done/failed (default "done")
}

extension LedgerRecord {
    private func jsonObject(_ raw: String) -> [String: Any] {
        guard let data = raw.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [:] }
        return obj
    }

    /// `outcome.adopted` — whether this generation was adopted (db.rs sets this).
    var isAdopted: Bool { (jsonObject(outcome)["adopted"] as? Bool) ?? false }

    /// `outcome.edit_dist_phash` if present (R0→R1 pHash edit distance).
    var editDistPHash: Double? { jsonObject(outcome)["edit_dist_phash"] as? Double }

    /// `outcome.edit_dist_embed` if present (R0→R1 embedding cosine distance).
    var editDistEmbed: Double? { jsonObject(outcome)["edit_dist_embed"] as? Double }

    /// `generation_params` re-serialized as sorted, pretty JSON for display.
    var prettyGenerationParams: String { LedgerRecord.prettyJSON(generationParams) }

    /// Pretty-print a raw JSON string (sorted keys); returns the raw text if it
    /// is not valid JSON or is an empty object.
    static func prettyJSON(_ raw: String) -> String {
        guard let data = raw.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data),
              JSONSerialization.isValidJSONObject(obj),
              let pretty = try? JSONSerialization.data(
                withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]),
              let s = String(data: pretty, encoding: .utf8)
        else { return raw }
        return s
    }

    /// VLM tags stored at `derived.tag` (shape is tolerant: array or {tags:[...]}).
    var tags: [String] {
        let tag = jsonObject(derived)["tag"]
        if let arr = tag as? [String] { return arr }
        if let obj = tag as? [String: Any], let arr = obj["tags"] as? [String] { return arr }
        return []
    }

    /// First VRM/GLB asset path found in `asset_ref`, if any.
    var assetPath: String? {
        let obj = jsonObject(assetRef)
        for key in ["vrm", "glb", "path", "asset"] {
            if let v = obj[key] as? String, !v.isEmpty { return v }
        }
        return nil
    }

    var shortID: String { String(id.prefix(8)) }

    /// Lifecycle status of the record. `status` is a free-text column (the
    /// `ledger` CLI writes reserved/generating/done/failed); we normalise it to
    /// a small enum and default to `.done` for legacy rows / unknown values.
    enum StatusKind { case reserved, generating, done, failed }

    var statusKind: StatusKind {
        switch status.lowercased() {
        case "reserved": return .reserved
        case "generating": return .generating
        case "failed": return .failed
        default: return .done
        }
    }

    /// A pending row (reserved or generating) is awaiting fulfilment.
    var isPending: Bool {
        switch statusKind { case .reserved, .generating: return true; default: return false }
    }
}

// MARK: - Forest

/// The derivation forest: records linked by `parentID`.
///
/// Root/child classification mirrors the Rust `print_tree` (main.rs:316-321):
/// a record is a child only if its `parentID` is present *and* resolves to a
/// record in the set; otherwise (nil or dangling) it is a root.
struct LedgerForest {
    let records: [LedgerRecord]
    let childrenByParent: [String: [LedgerRecord]]
    let roots: [LedgerRecord]

    init(records: [LedgerRecord]) {
        self.records = records
        let ids = Set(records.map(\.id))
        var children: [String: [LedgerRecord]] = [:]
        var roots: [LedgerRecord] = []
        for r in records {
            if let pid = r.parentID, ids.contains(pid) {
                children[pid, default: []].append(r)
            } else {
                roots.append(r)
            }
        }
        self.childrenByParent = children
        self.roots = roots
    }

    func children(of id: String) -> [LedgerRecord] { childrenByParent[id] ?? [] }
}

// MARK: - Store (read-only)

enum LedgerStoreError: Error, CustomStringConvertible {
    case open(String)
    case prepare(String)

    var description: String {
        switch self {
        case .open(let m): return "cannot open ledger DB: \(m)"
        case .prepare(let m): return "cannot prepare query: \(m)"
        }
    }
}

/// Read-only reader for `~/.vrm-pipeline/ledger.db`.
///
/// Opens with `SQLITE_OPEN_READONLY` — the viewer never writes to the ledger.
struct LedgerStore {
    let path: String

    /// Default ledger location used by the Rust pipeline (main.rs default_db_path).
    static var defaultPath: String {
        (NSHomeDirectory() as NSString).appendingPathComponent(".vrm-pipeline/ledger.db")
    }

    init(path: String = LedgerStore.defaultPath) { self.path = path }

    var exists: Bool { FileManager.default.fileExists(atPath: path) }

    /// Read all records, oldest first. Read-only; never mutates the DB.
    func fetchAllRecords() throws -> [LedgerRecord] {
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            let msg = db.map { String(cString: sqlite3_errmsg($0)) } ?? "unknown"
            sqlite3_close(db)
            throw LedgerStoreError.open(msg)
        }
        defer { sqlite3_close(db) }

        // The `status` column was added later (T1); older DBs lack it. Detect
        // its presence so the SELECT never fails on a legacy schema, and default
        // missing/legacy rows to "done".
        let hasStatus = columnExists(db, table: "records", column: "status")

        let statusCol = hasStatus ? ", status" : ""
        let sql = """
        SELECT id, timestamp, prompt, generation_params, r0_ref, r1_ref, \
        outcome, asset_ref, derived, parent_id, image_ref\(statusCol) \
        FROM records ORDER BY timestamp ASC
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            throw LedgerStoreError.prepare(String(cString: sqlite3_errmsg(db)))
        }
        defer { sqlite3_finalize(stmt) }

        func text(_ col: Int32) -> String { sqlite3_column_text(stmt, col).map { String(cString: $0) } ?? "" }
        func optText(_ col: Int32) -> String? {
            sqlite3_column_type(stmt, col) == SQLITE_NULL ? nil : text(col)
        }

        var out: [LedgerRecord] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let status: String = hasStatus ? (optText(11) ?? "done") : "done"
            out.append(LedgerRecord(
                id: text(0),
                timestamp: text(1),
                prompt: text(2),
                generationParams: text(3),
                r0Ref: text(4),
                r1Ref: optText(5),
                outcome: text(6),
                assetRef: text(7),
                derived: text(8),
                parentID: optText(9),
                imageRef: optText(10),
                status: status.isEmpty ? "done" : status
            ))
        }
        return out
    }

    /// Read-only check for whether `column` exists on `table` via PRAGMA.
    private func columnExists(_ db: OpaquePointer?, table: String, column: String) -> Bool {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "PRAGMA table_info(\(table))", -1, &stmt, nil) == SQLITE_OK else {
            return false
        }
        defer { sqlite3_finalize(stmt) }
        while sqlite3_step(stmt) == SQLITE_ROW {
            // column 1 of table_info is the column name.
            if let name = sqlite3_column_text(stmt, 1), String(cString: name) == column {
                return true
            }
        }
        return false
    }

    /// Convenience: read all records and build the forest in one call.
    func loadForest() throws -> LedgerForest { LedgerForest(records: try fetchAllRecords()) }
}
