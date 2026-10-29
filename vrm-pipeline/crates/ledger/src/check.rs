//! `ledger check`: read-only, fail-closed, three-valued integrity gate.
//!
//! Exit codes:
//! - 0: clean — every rule was evaluated on every record and none was violated.
//! - 1: violations — one stdout line per violation, naming the record id and
//!   the rule (`parent-missing`, `lineage-cycle`, `asset-missing`).
//! - 2: cannot determine — the DB could not be opened/read, the schema lacks a
//!   required column, or some record's `asset_ref` could not be interpreted.
//!   Each reason goes to stderr (naming the record id where there is one).
//!   Undetermined wins over violations for the exit code, but any violations
//!   found are still printed on stdout.
//!
//! The DB is opened with `SQLITE_OPEN_READ_ONLY` (no create flag, no
//! migrations: `db::open()` is deliberately not used), so the file is never
//! created or modified.

use rusqlite::types::ValueRef;
use rusqlite::{Connection, OpenFlags};
use std::collections::{HashMap, HashSet};
use std::io::Write;
use std::path::Path;

pub const EXIT_CLEAN: i32 = 0;
pub const EXIT_VIOLATIONS: i32 = 1;
pub const EXIT_UNDETERMINED: i32 = 2;

const REQUIRED_COLUMNS: [&str; 3] = ["id", "parent_id", "asset_ref"];

#[derive(Debug, Default)]
pub struct Report {
    /// One line per violation (stdout).
    pub violations: Vec<String>,
    /// One line per reason the check could not be completed (stderr).
    pub undetermined: Vec<String>,
}

impl Report {
    pub fn exit_code(&self) -> i32 {
        if !self.undetermined.is_empty() {
            EXIT_UNDETERMINED
        } else if !self.violations.is_empty() {
            EXIT_VIOLATIONS
        } else {
            EXIT_CLEAN
        }
    }
}

/// Run the check and print the report; returns the process exit code.
pub fn run(db_path: &Path) -> i32 {
    let report = check(db_path);
    let mut out = std::io::stdout().lock();
    let mut err = std::io::stderr().lock();
    let mut io_failed = false;
    for v in &report.violations {
        io_failed |= writeln!(out, "{v}").is_err();
    }
    for u in &report.undetermined {
        let _ = writeln!(err, "ledger check: cannot determine: {u}");
    }
    io_failed |= out.flush().is_err();
    if io_failed {
        // The violation list could not be delivered; never report success.
        let _ = writeln!(
            err,
            "ledger check: cannot determine: failed to write report to stdout"
        );
        let _ = err.flush();
        return EXIT_UNDETERMINED;
    }
    let _ = err.flush();
    report.exit_code()
}

/// One SQLite cell, converted without any panicking path.
enum Cell {
    Null,
    Text(String),
    /// TEXT whose bytes are not valid UTF-8 (lossy rendering, error).
    BadText(String, std::str::Utf8Error),
    /// Any non-text, non-null storage class.
    Other(&'static str),
}

impl Cell {
    fn from_ref(v: ValueRef<'_>) -> Cell {
        match v {
            ValueRef::Null => Cell::Null,
            ValueRef::Text(b) => match std::str::from_utf8(b) {
                Ok(s) => Cell::Text(s.to_string()),
                Err(e) => Cell::BadText(String::from_utf8_lossy(b).into_owned(), e),
            },
            ValueRef::Integer(_) => Cell::Other("integer"),
            ValueRef::Real(_) => Cell::Other("real"),
            ValueRef::Blob(_) => Cell::Other("blob"),
        }
    }

    fn describe(&self) -> String {
        match self {
            Cell::Null => "null".to_string(),
            Cell::Text(s) => format!("text {s:?}"),
            Cell::BadText(lossy, e) => format!("text is not valid UTF-8 ({e}): {lossy:?}"),
            Cell::Other(kind) => (*kind).to_string(),
        }
    }
}

struct Row {
    id: String,
    parent: Option<String>,
    asset_ref: Option<String>,
}

pub fn check(db_path: &Path) -> Report {
    let mut report = Report::default();

    if !db_path.exists() {
        report.undetermined.push(format!(
            "database file does not exist: {}",
            db_path.display()
        ));
        return report;
    }

    let conn = match Connection::open_with_flags(db_path, OpenFlags::SQLITE_OPEN_READ_ONLY) {
        Ok(c) => c,
        Err(e) => {
            report.undetermined.push(format!(
                "cannot open database {} read-only: {e}",
                db_path.display()
            ));
            return report;
        }
    };

    let rows = match load_rows(&conn, &mut report) {
        Some(r) => r,
        None => return report,
    };

    check_lineage(&rows, &mut report);
    for row in &rows {
        check_assets(row, &mut report);
    }
    report
}

/// Returns `None` (with reasons recorded) when the rows cannot be read at all.
fn load_rows(conn: &Connection, report: &mut Report) -> Option<Vec<Row>> {
    let table_count: i64 = match conn.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'records'",
        [],
        |r| r.get(0),
    ) {
        Ok(n) => n,
        Err(e) => {
            report
                .undetermined
                .push(format!("cannot read database schema: {e}"));
            return None;
        }
    };
    if table_count == 0 {
        report
            .undetermined
            .push("no `records` table in database".to_string());
        return None;
    }

    let columns: Vec<String> = match conn
        .prepare("SELECT name FROM pragma_table_info('records')")
        .and_then(|mut s| {
            s.query_map([], |r| r.get::<_, String>(0))?
                .collect::<Result<Vec<_>, _>>()
        }) {
        Ok(c) => c,
        Err(e) => {
            report
                .undetermined
                .push(format!("cannot read `records` columns: {e}"));
            return None;
        }
    };
    let missing: Vec<&str> = REQUIRED_COLUMNS
        .iter()
        .copied()
        .filter(|c| !columns.iter().any(|have| have == c))
        .collect();
    if !missing.is_empty() {
        report.undetermined.push(format!(
            "`records` table is missing required column(s): {}",
            missing.join(", ")
        ));
        return None;
    }

    // Columns are read through `get_ref` into `Cell`, never through
    // `FromSql for Value`: rusqlite's `Value` conversion panics on non-UTF8
    // TEXT. A bad cell becomes `Cell::BadText`, i.e. undetermined.
    let raw: Vec<(Cell, Cell, Cell)> = match conn
        .prepare("SELECT id, parent_id, asset_ref FROM records")
        .and_then(|mut s| {
            s.query_map([], |r| {
                Ok((
                    Cell::from_ref(r.get_ref(0)?),
                    Cell::from_ref(r.get_ref(1)?),
                    Cell::from_ref(r.get_ref(2)?),
                ))
            })?
            .collect::<Result<Vec<_>, _>>()
        }) {
        Ok(r) => r,
        Err(e) => {
            report
                .undetermined
                .push(format!("cannot read records: {e}"));
            return None;
        }
    };

    let mut rows = Vec::with_capacity(raw.len());
    let mut seen: HashMap<String, usize> = HashMap::new();
    for (i, (id, parent, asset_ref)) in raw.into_iter().enumerate() {
        let id = match id {
            Cell::Text(s) if !s.is_empty() => s,
            other => {
                report.undetermined.push(format!(
                    "record at row {} has an unreadable, non-text or empty id: {}",
                    i + 1,
                    other.describe()
                ));
                continue;
            }
        };
        *seen.entry(id.clone()).or_default() += 1;
        let parent = match parent {
            Cell::Null => None,
            Cell::Text(s) if s.is_empty() => None,
            Cell::Text(s) => Some(s),
            other => {
                report.undetermined.push(format!(
                    "record {id}: parent_id is not readable text: {}",
                    other.describe()
                ));
                continue;
            }
        };
        let asset_ref = match asset_ref {
            Cell::Text(s) => Some(s),
            other => {
                report.undetermined.push(format!(
                    "record {id}: asset_ref is not readable text: {}",
                    other.describe()
                ));
                None
            }
        };
        rows.push(Row {
            id,
            parent,
            asset_ref,
        });
    }

    let mut dups: Vec<&String> = seen
        .iter()
        .filter(|(_, n)| **n > 1)
        .map(|(k, _)| k)
        .collect();
    dups.sort();
    for d in dups {
        report.undetermined.push(format!(
            "record {d}: id appears more than once; lineage is ambiguous"
        ));
    }
    Some(rows)
}

fn check_lineage(rows: &[Row], report: &mut Report) {
    let index: HashMap<&str, usize> = rows
        .iter()
        .enumerate()
        .map(|(i, r)| (r.id.as_str(), i))
        .collect();

    // parent edge resolved to an index; None for roots and for missing parents.
    let mut next: Vec<Option<usize>> = vec![None; rows.len()];
    for (i, r) in rows.iter().enumerate() {
        if let Some(p) = &r.parent {
            match index.get(p.as_str()) {
                Some(&j) => next[i] = Some(j),
                None => report
                    .violations
                    .push(format!("{}\tparent-missing\tparent_id={p}", r.id)),
            }
        }
    }

    // Out-degree <= 1, so a simple path walk with three colours finds every
    // cycle and visits each node at most once: O(n), terminates on any graph.
    const UNSEEN: u8 = 0;
    const ON_PATH: u8 = 1;
    const DONE: u8 = 2;
    let mut state = vec![UNSEEN; rows.len()];
    let mut in_cycle = vec![false; rows.len()];
    for start in 0..rows.len() {
        if state[start] != UNSEEN {
            continue;
        }
        let mut path = Vec::new();
        let mut cur = Some(start);
        while let Some(n) = cur {
            match state[n] {
                UNSEEN => {
                    state[n] = ON_PATH;
                    path.push(n);
                    cur = next[n];
                }
                ON_PATH => {
                    let pos = path.iter().position(|&x| x == n).unwrap_or(0);
                    for &m in &path[pos..] {
                        in_cycle[m] = true;
                    }
                    break;
                }
                _ => break,
            }
        }
        for n in path {
            state[n] = DONE;
        }
    }
    for (i, r) in rows.iter().enumerate() {
        if in_cycle[i] {
            let parent = r.parent.as_deref().unwrap_or("");
            report
                .violations
                .push(format!("{}\tlineage-cycle\tparent_id={parent}", r.id));
        }
    }
}

/// Top-level key scan of a JSON object: holds the first repeated key, if any.
struct TopLevelKeys(Option<String>);

impl<'de> serde::Deserialize<'de> for TopLevelKeys {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> serde::de::Visitor<'de> for V {
            type Value = TopLevelKeys;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a JSON object")
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut m: A,
            ) -> Result<TopLevelKeys, A::Error> {
                let mut keys = HashSet::new();
                let mut dup = None;
                while let Some(k) = m.next_key::<String>()? {
                    m.next_value::<serde::de::IgnoredAny>()?;
                    if dup.is_none() && !keys.insert(k.clone()) {
                        dup = Some(k);
                    }
                }
                Ok(TopLevelKeys(dup))
            }
        }
        d.deserialize_map(V)
    }
}

fn check_assets(row: &Row, report: &mut Report) {
    let id = &row.id;
    let Some(text) = &row.asset_ref else {
        return; // already recorded as undetermined in load_rows
    };
    let value: serde_json::Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(e) => {
            report
                .undetermined
                .push(format!("record {id}: asset_ref is not valid JSON: {e}"));
            return;
        }
    };
    let Some(map) = value.as_object() else {
        report.undetermined.push(format!(
            "record {id}: asset_ref is not a JSON object: {text}"
        ));
        return;
    };
    // `serde_json::Value` keeps only the last of repeated keys, so an earlier
    // (missing / relative) path would never be checked. Re-scan the top-level
    // keys; nested objects need no scan because any non-string value is
    // already undetermined below.
    match serde_json::from_str::<TopLevelKeys>(text) {
        Ok(TopLevelKeys(None)) => {}
        Ok(TopLevelKeys(Some(dup))) => {
            report.undetermined.push(format!(
                "record {id}: asset_ref has duplicate key {dup:?}; which path applies is ambiguous"
            ));
            return;
        }
        Err(e) => {
            report
                .undetermined
                .push(format!("record {id}: cannot scan asset_ref keys: {e}"));
            return;
        }
    }
    for (kind, v) in map {
        let Some(path) = v.as_str() else {
            report.undetermined.push(format!(
                "record {id}: asset_ref[{kind:?}] is not a string: {v}"
            ));
            continue;
        };
        if path.is_empty() {
            report.undetermined.push(format!(
                "record {id}: asset_ref[{kind:?}] is an empty string"
            ));
            continue;
        }
        let p = Path::new(path);
        if !p.is_absolute() {
            report.undetermined.push(format!(
                "record {id}: asset_ref[{kind:?}] is a relative path with no documented base: {path}"
            ));
            continue;
        }
        match std::fs::metadata(p) {
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => report
                .violations
                .push(format!("{id}\tasset-missing\t{kind}={path}")),
            Err(e) => report.undetermined.push(format!(
                "record {id}: cannot stat asset_ref[{kind:?}] {path}: {e}"
            )),
        }
    }
}
