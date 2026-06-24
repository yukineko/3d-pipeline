use serde::{Deserialize, Serialize};

pub const CREATE_TABLE_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS records (
    id                TEXT PRIMARY KEY,
    timestamp         TEXT NOT NULL,
    prompt            TEXT NOT NULL DEFAULT '',
    generation_params TEXT NOT NULL DEFAULT '{}',
    r0_ref            TEXT NOT NULL DEFAULT '',
    r1_ref            TEXT,
    outcome           TEXT NOT NULL DEFAULT '{}',
    asset_ref         TEXT NOT NULL DEFAULT '{}',
    derived           TEXT NOT NULL DEFAULT '{}',
    parent_id         TEXT
);
"#;

// Idempotent migration: add derived column to existing DBs created before schema v2.
pub const MIGRATE_V2_SQL: &str =
    "ALTER TABLE records ADD COLUMN derived TEXT NOT NULL DEFAULT '{}';";

// Idempotent migration: add parent_id column (lineage) to DBs created before schema v3.
pub const MIGRATE_V3_SQL: &str = "ALTER TABLE records ADD COLUMN parent_id TEXT;";

#[derive(Debug, Serialize, Deserialize)]
pub struct Record {
    pub id: String,
    pub timestamp: String,
    pub prompt: String,
    pub generation_params: String,
    pub r0_ref: String,
    pub r1_ref: Option<String>,
    pub outcome: String,
    pub asset_ref: String,
    pub derived: String,
    /// id of the record this one was derived from (None for roots).
    pub parent_id: Option<String>,
}
