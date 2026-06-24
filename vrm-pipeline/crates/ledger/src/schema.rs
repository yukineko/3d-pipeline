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
    asset_ref         TEXT NOT NULL DEFAULT '{}'
);
"#;

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
}
