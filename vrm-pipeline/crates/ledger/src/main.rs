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
    /// Path to ledger SQLite DB (default: ~/.vrm-pipeline/ledger.db)
    #[arg(long, global = true)]
    db: Option<PathBuf>,

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

        /// id of the record this one is derived from (lineage parent)
        #[arg(long = "parent-id")]
        parent_id: Option<String>,

        /// path to the input image used to generate this record
        #[arg(long = "image-ref")]
        image_ref: Option<String>,
    },

    /// Print a single record as JSON
    Get {
        /// Record ID
        #[arg(long)]
        id: String,
    },

    /// Print the derivation lineage as an ASCII tree
    Tree {
        /// Show only the subtree rooted at this record ID (default: whole forest)
        #[arg(long)]
        root: Option<String>,
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

    /// Update outcome edit distances for a record (R0→R1)
    UpdateOutcome {
        /// Record ID
        #[arg(long)]
        id: String,
        /// R0→R1 pHash edit distance (average hamming distance across faces)
        #[arg(long = "edit-dist-phash")]
        edit_dist_phash: Option<f64>,
        /// R0→R1 embedding cosine distance
        #[arg(long = "edit-dist-embed")]
        edit_dist_embed: Option<f64>,
    },

    /// Set the r1_ref path for a record
    SetR1Ref {
        /// Record ID
        #[arg(long)]
        id: String,
        /// Path to R1 render directory
        #[arg(long)]
        path: String,
    },

    /// Get the stored embedding vector for a record (JSON)
    GetEmbedding {
        /// Record ID
        #[arg(long)]
        id: String,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let db_path = cli.db.clone().unwrap_or_else(default_db_path);

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
            parent_id,
            image_ref,
        } => {
            let r0_ref = r0_dir.unwrap_or_default();
            let id = db::insert(
                &db_path,
                &prompt,
                &r0_ref,
                &generation_params,
                &asset_ref,
                parent_id.as_deref(),
                image_ref.as_deref(),
            )?;
            println!("{id}");
        }

        Commands::Get { id } => {
            let record = db::get_record(&db_path, &id)?;
            println!("{}", serde_json::to_string_pretty(&record)?);
        }

        Commands::Tree { root } => {
            let records = db::all_records(&db_path)?;
            print_tree(&records, root.as_deref());
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

        Commands::UpdateOutcome { id, edit_dist_phash, edit_dist_embed } => {
            if let Some(phash) = edit_dist_phash {
                db::update_outcome_phash(&db_path, &id, phash)?;
                println!("Updated outcome for {id}: edit_dist_phash={phash}");
            }
            if let Some(embed) = edit_dist_embed {
                db::update_outcome_embed(&db_path, &id, embed)?;
                println!("Updated outcome for {id}: edit_dist_embed={embed}");
            }
        }

        Commands::SetR1Ref { id, path } => {
            db::set_r1_ref(&db_path, &id, &path)?;
            println!("Set r1_ref for {id}: {path}");
        }

        Commands::GetEmbedding { id } => {
            match db::get_embedding(&db_path, &id)? {
                Some(vec) => {
                    let json = serde_json::json!({"id": id, "record_embedding": vec});
                    println!("{}", serde_json::to_string(&json).unwrap());
                }
                None => {
                    eprintln!("No embedding found for {id}");
                    std::process::exit(1);
                }
            }
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

/// Render records as an ASCII derivation tree. Roots are records whose parent_id
/// is absent (or points outside the set). With `root`, only that subtree prints.
fn print_tree(records: &[schema::Record], root: Option<&str>) {
    use std::collections::{HashMap, HashSet};

    let ids: HashSet<&str> = records.iter().map(|r| r.id.as_str()).collect();
    let mut children: HashMap<&str, Vec<&schema::Record>> = HashMap::new();
    let mut roots: Vec<&schema::Record> = Vec::new();
    for r in records {
        match r.parent_id.as_deref() {
            Some(pid) if ids.contains(pid) => children.entry(pid).or_default().push(r),
            _ => roots.push(r),
        }
    }

    if let Some(root_id) = root {
        match records.iter().find(|r| r.id == root_id) {
            Some(r) => walk(r, "", true, true, &children),
            None => eprintln!("No record found with id {root_id}"),
        }
        return;
    }

    if roots.is_empty() {
        println!("No records found.");
        return;
    }
    for r in &roots {
        walk(r, "", true, true, &children);
    }
}

fn walk(
    r: &schema::Record,
    prefix: &str,
    is_root: bool,
    is_last: bool,
    children: &std::collections::HashMap<&str, Vec<&schema::Record>>,
) {
    let connector = if is_root {
        ""
    } else if is_last {
        "└─ "
    } else {
        "├─ "
    };
    let prompt = r.prompt.replace('\n', " ");
    let prompt_short = if prompt.chars().count() > 50 {
        format!("{}…", prompt.chars().take(50).collect::<String>())
    } else {
        prompt
    };
    let short_id = r.id.get(..8).unwrap_or(&r.id);
    println!("{prefix}{connector}{short_id}  {prompt_short}");

    if let Some(kids) = children.get(r.id.as_str()) {
        let child_prefix = if is_root {
            String::new()
        } else if is_last {
            format!("{prefix}   ")
        } else {
            format!("{prefix}│  ")
        };
        for (i, c) in kids.iter().enumerate() {
            walk(c, &child_prefix, false, i == kids.len() - 1, children);
        }
    }
}
