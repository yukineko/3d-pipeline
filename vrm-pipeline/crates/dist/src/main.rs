use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "dist", about = "VRM pipeline image distance tools")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Compute perceptual hash distance between two images
    Phash {
        img1: PathBuf,
        img2: PathBuf,
    },
    /// Compute normalized pixel difference between two images (0.0–1.0)
    Pixel {
        img1: PathBuf,
        img2: PathBuf,
    },
    /// Compute noise floor for a single image (hash it twice, distance should be 0)
    Floor {
        img_path: PathBuf,
    },
    /// Measure distance between paired .webp files in two directories
    Measure {
        #[arg(long)]
        r0: PathBuf,
        #[arg(long)]
        r1: PathBuf,
        /// Optional ledger DB path (currently unused; results printed to stdout as JSON)
        #[arg(long)]
        ledger: Option<PathBuf>,
        /// Optional record ID (currently unused; results printed to stdout as JSON)
        #[arg(long)]
        record_id: Option<String>,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Commands::Phash { img1, img2 } => {
            let dist = dist::phash_distance(&img1, &img2)?;
            println!("{}", dist);
        }
        Commands::Pixel { img1, img2 } => {
            let d = dist::pixel_diff(&img1, &img2)?;
            println!("{:.6}", d);
        }
        Commands::Floor { img_path } => {
            let nf = dist::noise_floor(&img_path)?;
            println!("{}", serde_json::to_string_pretty(&nf)?);
        }
        Commands::Measure {
            r0,
            r1,
            ledger: _,
            record_id: _,
        } => {
            let result = dist::measure_dir_pair(&r0, &r1)?;
            println!("{}", serde_json::to_string_pretty(&result)?);
        }
    }
    Ok(())
}
