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
    parent_id         TEXT,
    image_ref         TEXT,
    status            TEXT NOT NULL DEFAULT 'done'
);
"#;

// Idempotent migration: add derived column to existing DBs created before schema v2.
pub const MIGRATE_V2_SQL: &str =
    "ALTER TABLE records ADD COLUMN derived TEXT NOT NULL DEFAULT '{}';";

// Idempotent migration: add parent_id column (lineage) to DBs created before schema v3.
pub const MIGRATE_V3_SQL: &str = "ALTER TABLE records ADD COLUMN parent_id TEXT;";

// Idempotent migration: add image_ref column (input image path) to DBs created before schema v4.
pub const MIGRATE_V4_SQL: &str = "ALTER TABLE records ADD COLUMN image_ref TEXT;";

// Idempotent migration: add status column (lifecycle state) to DBs created before schema v5.
// Legacy rows default to 'done' since they predate the reservation queue.
pub const MIGRATE_V5_SQL: &str =
    "ALTER TABLE records ADD COLUMN status TEXT NOT NULL DEFAULT 'done';";

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
    /// path to the input image used to generate this record (None if none).
    pub image_ref: Option<String>,
    /// lifecycle state: 'reserved' | 'generating' | 'done' (legacy rows = 'done').
    pub status: String,
}
