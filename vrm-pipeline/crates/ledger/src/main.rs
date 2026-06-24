mod db;
mod schema;
mod search;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use std::path::PathBuf;

fn default_db_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
    PathBuf::from(home).join(".vrm-pipeline").join("ledger.db")
}

#[derive(Parser)]
#[command(name = "ledger", about = "VRM pipeline record ledger")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Create the database
    Init,

    /// Insert a new record
    Insert {
        /// Prompt text
        #[arg(long)]
        prompt: String,

        /// r0 render output directory path
        #[arg(long = "r0-dir")]
        r0_dir: Option<String>,

        /// Generation parameters as JSON
        #[arg(long = "generation-params", default_value = "{}")]
        generation_params: String,

        /// Asset reference as JSON
        #[arg(long = "asset-ref", default_value = "{}")]
        asset_ref: String,
    },

    /// Mark a record as adopted
    Adopt {
        /// Record ID
        id: String,
    },

    /// List records
    List {
        /// Maximum number of records to show
        #[arg(long, default_value_t = 20)]
        limit: u32,
    },

    /// Show statistics
    Stats,

    /// Store embedding vector (from embed.py output) into derived.embed
    Embed {
        /// Record ID
        #[arg(long)]
        id: String,
        /// Path to embed.json produced by embed.py
        #[arg(long = "embed-json")]
        embed_json: PathBuf,
    },

    /// Store VLM tags (from tag.py output) into derived.tag
    Tag {
        /// Record ID
        #[arg(long)]
        id: String,
        /// Path to tags.json produced by tag.py
        #[arg(long = "tag-json")]
        tag_json: PathBuf,
    },

    /// Find similar records by cosine similarity of stored embeddings
    Similar {
        /// Path to embed.json to use as query
        #[arg(long = "embed-json")]
        embed_json: PathBuf,
        /// Number of top results to return
        #[arg(long, default_value_t = 5)]
        top_k: usize,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let db_path = default_db_path();

    match cli.command {
        Commands::Init => {
            db::init(&db_path)?;
            println!("Initialized ledger at {}", db_path.display());
        }

        Commands::Insert {
            prompt,
            r0_dir,
            generation_params,
            asset_ref,
        } => {
            let r0_ref = r0_dir.unwrap_or_default();
            let id = db::insert(&db_path, &prompt, &r0_ref, &generation_params, &asset_ref)?;
            println!("{id}");
        }

        Commands::Adopt { id } => {
            db::adopt(&db_path, &id)?;
            println!("Adopted record {id}");
        }

        Commands::List { limit } => {
            let records = db::list_records(&db_path, limit)?;
            if records.is_empty() {
                println!("No records found.");
            } else {
                println!(
                    "{:<38} {:<25} {:<40} {}",
                    "ID", "TIMESTAMP", "PROMPT", "r0_ref"
                );
                println!("{}", "-".repeat(120));
                for r in &records {
                    let prompt_short = if r.prompt.len() > 38 {
                        format!("{}...", &r.prompt[..35])
                    } else {
                        r.prompt.clone()
                    };
                    println!(
                        "{:<38} {:<25} {:<40} {}",
                        r.id, r.timestamp, prompt_short, r.r0_ref
                    );
                }
                println!("\n{} record(s) shown.", records.len());
            }
        }

        Commands::Stats => {
            let s = db::stats(&db_path)?;
            println!("Total records  : {}", s.total);
            println!("Adopted        : {}", s.adopted);
            println!("Adoption rate  : {:.1}%", s.adoption_rate * 100.0);
            match s.avg_edit_dist {
                Some(avg) => println!("Avg edit dist  : {avg:.4}"),
                None => println!("Avg edit dist  : N/A"),
            }
        }

        Commands::Embed { id, embed_json } => {
            let payload = std::fs::read_to_string(&embed_json)
                .with_context(|| format!("cannot read {}", embed_json.display()))?;
            let value: serde_json::Value =
                serde_json::from_str(&payload).context("embed-json is not valid JSON")?;
            db::set_derived_key(&db_path, &id, "embed", &value)?;
            println!("Saved embedding for {id}");
        }

        Commands::Tag { id, tag_json } => {
            let payload = std::fs::read_to_string(&tag_json)
                .with_context(|| format!("cannot read {}", tag_json.display()))?;
            let value: serde_json::Value =
                serde_json::from_str(&payload).context("tag-json is not valid JSON")?;
            db::set_derived_key(&db_path, &id, "tag", &value)?;
            println!("Saved tags for {id}");
        }

        Commands::Similar { embed_json, top_k } => {
            let payload = std::fs::read_to_string(&embed_json)
                .with_context(|| format!("cannot read {}", embed_json.display()))?;
            let query: serde_json::Value =
                serde_json::from_str(&payload).context("embed-json is not valid JSON")?;
            let query_vec = query["record_embedding"]
                .as_array()
                .context("embed-json missing record_embedding array")?
                .iter()
                .filter_map(|v| v.as_f64())
                .collect::<Vec<_>>();

            let all = db::all_embeddings(&db_path)?;
            let mut results = search::top_k_similar(&query_vec, &all, top_k);
            results.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap());
            for r in &results {
                println!("{:.4}  {}", r.score, r.id);
            }
        }
    }

    Ok(())
}
