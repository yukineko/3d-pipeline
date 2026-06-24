use anyhow::{Context, Result};
use chrono::Utc;
use rusqlite::{Connection, params};
use uuid::Uuid;

use crate::schema::{CREATE_TABLE_SQL, Record};

pub fn open(db_path: &std::path::Path) -> Result<Connection> {
    if let Some(parent) = db_path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("failed to create directory: {}", parent.display()))?;
    }
    let conn = Connection::open(db_path)
        .with_context(|| format!("failed to open DB: {}", db_path.display()))?;
    Ok(conn)
}

pub fn init(db_path: &std::path::Path) -> Result<()> {
    let conn = open(db_path)?;
    conn.execute_batch(CREATE_TABLE_SQL)
        .context("failed to create table")?;
    Ok(())
}

pub fn insert(
    db_path: &std::path::Path,
    prompt: &str,
    r0_ref: &str,
    generation_params: &str,
    asset_ref: &str,
) -> Result<String> {
    let conn = open(db_path)?;
    let id = Uuid::new_v4().to_string();
    let timestamp = Utc::now().to_rfc3339();
    conn.execute(
        "INSERT INTO records (id, timestamp, prompt, generation_params, r0_ref, asset_ref) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![id, timestamp, prompt, generation_params, r0_ref, asset_ref],
    )
    .context("failed to insert record")?;
    Ok(id)
}

pub fn adopt(db_path: &std::path::Path, id: &str) -> Result<()> {
    let conn = open(db_path)?;
    // Read current outcome JSON and update adopted flag
    let outcome: String = conn
        .query_row(
            "SELECT outcome FROM records WHERE id = ?1",
            params![id],
            |row| row.get(0),
        )
        .with_context(|| format!("record not found: {id}"))?;

    let mut obj: serde_json::Value =
        serde_json::from_str(&outcome).unwrap_or(serde_json::json!({}));
    obj["adopted"] = serde_json::Value::Bool(true);
    let updated = serde_json::to_string(&obj).context("failed to serialize outcome")?;

    conn.execute(
        "UPDATE records SET outcome = ?1 WHERE id = ?2",
        params![updated, id],
    )
    .context("failed to update record")?;
    Ok(())
}

pub fn list_records(db_path: &std::path::Path, limit: u32) -> Result<Vec<Record>> {
    let conn = open(db_path)?;
    let mut stmt = conn
        .prepare(
            "SELECT id, timestamp, prompt, generation_params, r0_ref, r1_ref, outcome, asset_ref \
             FROM records ORDER BY timestamp DESC LIMIT ?1",
        )
        .context("failed to prepare statement")?;

    let rows = stmt
        .query_map(params![limit], |row| {
            Ok(Record {
                id: row.get(0)?,
                timestamp: row.get(1)?,
                prompt: row.get(2)?,
                generation_params: row.get(3)?,
                r0_ref: row.get(4)?,
                r1_ref: row.get(5)?,
                outcome: row.get(6)?,
                asset_ref: row.get(7)?,
            })
        })
        .context("failed to query records")?;

    let mut records = Vec::new();
    for row in rows {
        records.push(row.context("failed to read row")?);
    }
    Ok(records)
}

pub struct Stats {
    pub total: u64,
    pub adopted: u64,
    pub adoption_rate: f64,
    pub avg_edit_dist: Option<f64>,
}

pub fn stats(db_path: &std::path::Path) -> Result<Stats> {
    let conn = open(db_path)?;

    let total: u64 = conn
        .query_row("SELECT COUNT(*) FROM records", [], |row| row.get(0))
        .context("failed to count records")?;

    // Count adopted: outcome JSON contains "adopted": true
    let outcomes: Vec<String> = {
        let mut stmt = conn
            .prepare("SELECT outcome FROM records")
            .context("failed to prepare")?;
        let rows = stmt
            .query_map([], |row| row.get(0))
            .context("failed to query")?;
        let mut v = Vec::new();
        for r in rows {
            v.push(r?);
        }
        v
    };

    let mut adopted: u64 = 0;
    let mut edit_dists: Vec<f64> = Vec::new();
    for outcome_str in &outcomes {
        if let Ok(obj) = serde_json::from_str::<serde_json::Value>(outcome_str) {
            if obj.get("adopted").and_then(|v| v.as_bool()).unwrap_or(false) {
                adopted += 1;
            }
            if let Some(ed) = obj.get("edit_dist_phash").and_then(|v| v.as_f64()) {
                edit_dists.push(ed);
            }
        }
    }

    let adoption_rate = if total > 0 {
        adopted as f64 / total as f64
    } else {
        0.0
    };

    let avg_edit_dist = if edit_dists.is_empty() {
        None
    } else {
        Some(edit_dists.iter().sum::<f64>() / edit_dists.len() as f64)
    };

    Ok(Stats {
        total,
        adopted,
        adoption_rate,
        avg_edit_dist,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn temp_db() -> (TempDir, std::path::PathBuf) {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("ledger.db");
        (dir, db_path)
    }

    #[test]
    fn test_init_creates_db() {
        let (_dir, db_path) = temp_db();
        init(&db_path).unwrap();
        assert!(db_path.exists(), "DB file should exist after init");
    }

    #[test]
    fn test_insert_creates_record() {
        let (_dir, db_path) = temp_db();
        init(&db_path).unwrap();
        let id = insert(&db_path, "hello", "/tmp/r0", "{}", "{}").unwrap();
        assert!(!id.is_empty());

        let records = list_records(&db_path, 10).unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].id, id);
        assert_eq!(records[0].prompt, "hello");
        assert_eq!(records[0].r0_ref, "/tmp/r0");
    }

    #[test]
    fn test_adopt_sets_adopted_true() {
        let (_dir, db_path) = temp_db();
        init(&db_path).unwrap();
        let id = insert(&db_path, "adopt-test", "", "{}", "{}").unwrap();
        adopt(&db_path, &id).unwrap();

        let records = list_records(&db_path, 10).unwrap();
        assert_eq!(records.len(), 1);
        let outcome: serde_json::Value =
            serde_json::from_str(&records[0].outcome).unwrap();
        assert_eq!(outcome["adopted"], serde_json::Value::Bool(true));
    }

    #[test]
    fn test_list_empty_no_error() {
        let (_dir, db_path) = temp_db();
        init(&db_path).unwrap();
        let records = list_records(&db_path, 20).unwrap();
        assert!(records.is_empty());
    }

    #[test]
    fn test_stats_empty() {
        let (_dir, db_path) = temp_db();
        init(&db_path).unwrap();
        let s = stats(&db_path).unwrap();
        assert_eq!(s.total, 0);
        assert_eq!(s.adopted, 0);
        assert!((s.adoption_rate - 0.0).abs() < f64::EPSILON);
        assert!(s.avg_edit_dist.is_none());
    }
}
