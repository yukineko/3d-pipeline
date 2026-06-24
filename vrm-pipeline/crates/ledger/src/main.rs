mod db;
mod schema;

use anyhow::Result;
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
    }

    Ok(())
}
