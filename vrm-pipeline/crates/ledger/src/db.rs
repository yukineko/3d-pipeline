use anyhow::{Context, Result};
use chrono::Utc;
use rusqlite::{Connection, params};
use uuid::Uuid;

use crate::schema::{CREATE_TABLE_SQL, MIGRATE_V2_SQL, MIGRATE_V3_SQL, MIGRATE_V4_SQL, Record};

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
    // Idempotent migrations: ignore error if column already exists.
    let _ = conn.execute_batch(MIGRATE_V2_SQL);
    let _ = conn.execute_batch(MIGRATE_V3_SQL);
    let _ = conn.execute_batch(MIGRATE_V4_SQL);
    Ok(())
}

pub fn insert(
    db_path: &std::path::Path,
    prompt: &str,
    r0_ref: &str,
    generation_params: &str,
    asset_ref: &str,
    parent_id: Option<&str>,
    image_ref: Option<&str>,
) -> Result<String> {
    let conn = open(db_path)?;
    let id = Uuid::new_v4().to_string();
    let timestamp = Utc::now().to_rfc3339();
    conn.execute(
        "INSERT INTO records (id, timestamp, prompt, generation_params, r0_ref, asset_ref, parent_id, image_ref) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
        params![id, timestamp, prompt, generation_params, r0_ref, asset_ref, parent_id, image_ref],
    )
    .context("failed to insert record")?;
    Ok(id)
}

/// Fetch a single record by id.
pub fn get_record(db_path: &std::path::Path, id: &str) -> Result<Record> {
    let conn = open(db_path)?;
    let record = conn
        .query_row(
            "SELECT id, timestamp, prompt, generation_params, r0_ref, r1_ref, outcome, asset_ref, derived, parent_id, image_ref \
             FROM records WHERE id = ?1",
            params![id],
            row_to_record,
        )
        .with_context(|| format!("record not found: {id}"))?;
    Ok(record)
}

/// All records, oldest first — convenient for building a lineage tree.
pub fn all_records(db_path: &std::path::Path) -> Result<Vec<Record>> {
    let conn = open(db_path)?;
    let mut stmt = conn
        .prepare(
            "SELECT id, timestamp, prompt, generation_params, r0_ref, r1_ref, outcome, asset_ref, derived, parent_id, image_ref \
             FROM records ORDER BY timestamp ASC",
        )
        .context("failed to prepare statement")?;
    let rows = stmt
        .query_map([], row_to_record)
        .context("failed to query records")?;
    let mut records = Vec::new();
    for row in rows {
        records.push(row.context("failed to read row")?);
    }
    Ok(records)
}

fn row_to_record(row: &rusqlite::Row) -> rusqlite::Result<Record> {
    Ok(Record {
        id: row.get(0)?,
        timestamp: row.get(1)?,
        prompt: row.get(2)?,
        generation_params: row.get(3)?,
        r0_ref: row.get(4)?,
        r1_ref: row.get(5)?,
        outcome: row.get(6)?,
        asset_ref: row.get(7)?,
        derived: row.get::<_, Option<String>>(8)?.unwrap_or_else(|| "{}".to_string()),
        parent_id: row.get(9)?,
        image_ref: row.get(10)?,
    })
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
            "SELECT id, timestamp, prompt, generation_params, r0_ref, r1_ref, outcome, asset_ref, derived, parent_id, image_ref \
             FROM records ORDER BY timestamp DESC LIMIT ?1",
        )
        .context("failed to prepare statement")?;

    let rows = stmt
        .query_map(params![limit], row_to_record)
        .context("failed to query records")?;

    let mut records = Vec::new();
    for row in rows {
        records.push(row.context("failed to read row")?);
    }
    Ok(records)
}

pub fn set_derived_key(
    db_path: &std::path::Path,
    id: &str,
    key: &str,
    value: &serde_json::Value,
) -> Result<()> {
    let conn = open(db_path)?;
    let raw: String = conn
        .query_row(
            "SELECT derived FROM records WHERE id = ?1",
            params![id],
            |row| row.get::<_, Option<String>>(0),
        )
        .with_context(|| format!("record not found: {id}"))?
        .unwrap_or_else(|| "{}".to_string());

    let mut obj: serde_json::Value =
        serde_json::from_str(&raw).unwrap_or(serde_json::json!({}));
    obj[key] = value.clone();
    let updated = serde_json::to_string(&obj).context("serialize derived")?;

    conn.execute(
        "UPDATE records SET derived = ?1 WHERE id = ?2",
        params![updated, id],
    )
    .context("failed to update derived")?;
    Ok(())
}

pub struct EmbeddingRow {
    pub id: String,
    pub vec: Vec<f64>,
}

pub fn all_embeddings(db_path: &std::path::Path) -> Result<Vec<EmbeddingRow>> {
    let conn = open(db_path)?;
    let mut stmt = conn
        .prepare("SELECT id, derived FROM records")
        .context("prepare all_embeddings")?;
    let rows = stmt
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?))
        })
        .context("query all_embeddings")?;

    let mut out = Vec::new();
    for row in rows {
        let (id, raw) = row.context("read row")?;
        let derived_str = raw.unwrap_or_else(|| "{}".to_string());
        let derived: serde_json::Value =
            serde_json::from_str(&derived_str).unwrap_or(serde_json::json!({}));
        if let Some(arr) = derived["embed"]["record_embedding"].as_array() {
            let vec: Vec<f64> = arr.iter().filter_map(|v| v.as_f64()).collect();
            if !vec.is_empty() {
                out.push(EmbeddingRow { id, vec });
            }
        }
    }
    Ok(out)
}

pub fn set_r1_ref(db_path: &std::path::Path, id: &str, r1_ref: &str) -> Result<()> {
    let conn = open(db_path)?;
    let affected = conn.execute(
        "UPDATE records SET r1_ref = ?1 WHERE id = ?2",
        params![r1_ref, id],
    )
    .context("failed to set r1_ref")?;
    if affected == 0 {
        anyhow::bail!("record not found: {id}");
    }
    Ok(())
}

/// Set the r0_ref (canonical render dir) for an existing record. Lets a manual
/// capture be attached to a record created without renders (e.g. a VRoid import).
pub fn set_r0_ref(db_path: &std::path::Path, id: &str, r0_ref: &str) -> Result<()> {
    let conn = open(db_path)?;
    let affected = conn.execute(
        "UPDATE records SET r0_ref = ?1 WHERE id = ?2",
        params![r0_ref, id],
    )
    .context("failed to set r0_ref")?;
    if affected == 0 {
        anyhow::bail!("record not found: {id}");
    }
    Ok(())
}

pub fn get_embedding(db_path: &std::path::Path, id: &str) -> Result<Option<Vec<f64>>> {
    let conn = open(db_path)?;
    let derived: String = conn
        .query_row(
            "SELECT derived FROM records WHERE id = ?1",
            params![id],
            |row| row.get::<_, Option<String>>(0),
        )
        .with_context(|| format!("record not found: {id}"))?
        .unwrap_or_else(|| "{}".to_string());

    let obj: serde_json::Value = serde_json::from_str(&derived).unwrap_or(serde_json::json!({}));
    if let Some(arr) = obj["embed"]["record_embedding"].as_array() {
        let vec: Vec<f64> = arr.iter().filter_map(|v| v.as_f64()).collect();
        if !vec.is_empty() {
            return Ok(Some(vec));
        }
    }
    Ok(None)
}

pub fn update_outcome_embed(db_path: &std::path::Path, id: &str, edit_dist_embed: f64) -> Result<()> {
    let conn = open(db_path)?;
    let outcome: String = conn
        .query_row(
            "SELECT outcome FROM records WHERE id = ?1",
            params![id],
            |row| row.get(0),
        )
        .with_context(|| format!("record not found: {id}"))?;

    let mut obj: serde_json::Value =
        serde_json::from_str(&outcome).unwrap_or(serde_json::json!({}));
    obj["edit_dist_embed"] = serde_json::json!(edit_dist_embed);
    let updated = serde_json::to_string(&obj).context("failed to serialize outcome")?;

    conn.execute(
        "UPDATE records SET outcome = ?1 WHERE id = ?2",
        params![updated, id],
    )
    .context("failed to update outcome")?;
    Ok(())
}

pub fn update_outcome_phash(db_path: &std::path::Path, id: &str, edit_dist_phash: f64) -> Result<()> {
    let conn = open(db_path)?;
    let outcome: String = conn
        .query_row(
            "SELECT outcome FROM records WHERE id = ?1",
            params![id],
            |row| row.get(0),
        )
        .with_context(|| format!("record not found: {id}"))?;

    let mut obj: serde_json::Value =
        serde_json::from_str(&outcome).unwrap_or(serde_json::json!({}));
    obj["edit_dist_phash"] = serde_json::json!(edit_dist_phash);
    let updated = serde_json::to_string(&obj).context("failed to serialize outcome")?;

    conn.execute(
        "UPDATE records SET outcome = ?1 WHERE id = ?2",
        params![updated, id],
    )
    .context("failed to update outcome")?;
    Ok(())
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
        let id = insert(&db_path, "hello", "/tmp/r0", "{}", "{}", None, None).unwrap();
        assert!(!id.is_empty());

        let records = list_records(&db_path, 10).unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].id, id);
        assert_eq!(records[0].prompt, "hello");
        assert_eq!(records[0].r0_ref, "/tmp/r0");
    }

    #[test]
    fn test_insert_with_parent_and_get_record() {
        let (_dir, db_path) = temp_db();
        init(&db_path).unwrap();
        let root = insert(&db_path, "chair", "", "{}", "{}", None, None).unwrap();
        let child = insert(&db_path, "chair v2", "", "{}", "{}", Some(&root), None).unwrap();

        let r = get_record(&db_path, &child).unwrap();
        assert_eq!(r.parent_id.as_deref(), Some(root.as_str()));

        let root_rec = get_record(&db_path, &root).unwrap();
        assert_eq!(root_rec.parent_id, None);
    }

    #[test]
    fn test_insert_with_image_ref_roundtrips() {
        let (_dir, db_path) = temp_db();
        init(&db_path).unwrap();
        let id = insert(
            &db_path,
            "with-image",
            "",
            "{}",
            "{}",
            None,
            Some("/tmp/input.png"),
        )
        .unwrap();

        let r = get_record(&db_path, &id).unwrap();
        assert_eq!(r.image_ref.as_deref(), Some("/tmp/input.png"));

        // a record without an image keeps image_ref None
        let id2 = insert(&db_path, "no-image", "", "{}", "{}", None, None).unwrap();
        let r2 = get_record(&db_path, &id2).unwrap();
        assert_eq!(r2.image_ref, None);
    }

    #[test]
    fn test_adopt_sets_adopted_true() {
        let (_dir, db_path) = temp_db();
        init(&db_path).unwrap();
        let id = insert(&db_path, "adopt-test", "", "{}", "{}", None, None).unwrap();
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
    fn test_set_derived_key_stores_and_updates() {
        let (_dir, db_path) = temp_db();
        init(&db_path).unwrap();
        let id = insert(&db_path, "embed-test", "", "{}", "{}", None, None).unwrap();

        let v1 = serde_json::json!({"record_embedding": [1.0, 2.0]});
        set_derived_key(&db_path, &id, "embed", &v1).unwrap();

        let records = list_records(&db_path, 10).unwrap();
        let derived: serde_json::Value = serde_json::from_str(&records[0].derived).unwrap();
        assert_eq!(derived["embed"]["record_embedding"][0], 1.0);

        // second key must not overwrite first
        let tags = serde_json::json!({"hair_color": "blue"});
        set_derived_key(&db_path, &id, "tag", &tags).unwrap();

        let records2 = list_records(&db_path, 10).unwrap();
        let d2: serde_json::Value = serde_json::from_str(&records2[0].derived).unwrap();
        assert_eq!(d2["embed"]["record_embedding"][0], 1.0);
        assert_eq!(d2["tag"]["hair_color"], "blue");
    }

    #[test]
    fn test_set_derived_key_unknown_id_errors() {
        let (_dir, db_path) = temp_db();
        init(&db_path).unwrap();
        let v = serde_json::json!({});
        let result = set_derived_key(&db_path, "no-such-id", "embed", &v);
        assert!(result.is_err());
    }

    #[test]
    fn test_all_embeddings_returns_rows_with_embed() {
        let (_dir, db_path) = temp_db();
        init(&db_path).unwrap();
        let id1 = insert(&db_path, "a", "", "{}", "{}", None, None).unwrap();
        let id2 = insert(&db_path, "b", "", "{}", "{}", None, None).unwrap();

        let v = serde_json::json!({"record_embedding": [0.1, 0.2, 0.3]});
        set_derived_key(&db_path, &id1, "embed", &v).unwrap();
        // id2 has no embed

        let rows = all_embeddings(&db_path).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].id, id1);
        assert_eq!(rows[0].vec, vec![0.1, 0.2, 0.3]);
        let _ = id2;
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
