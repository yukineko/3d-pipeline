//! CLI for the `dist` image-distance metrics (see README §dist).

use std::collections::BTreeMap;
use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};
use serde::Serialize;

#[derive(Parser)]
#[command(name = "dist", about = "Image distance metrics for render outputs")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Perceptual-hash (dHash) Hamming distance between two images.
    Phash { img1: PathBuf, img2: PathBuf },
    /// Normalized mean per-channel pixel difference between two images.
    Pixel { img1: PathBuf, img2: PathBuf },
    /// Print the identity noise floor for an image as JSON.
    Floor { img_path: PathBuf },
    /// Pair same-filename images in two dirs and report the max distances.
    Measure {
        /// Reference (baseline) directory.
        #[arg(long)]
        r0: PathBuf,
        /// Comparison directory.
        #[arg(long)]
        r1: PathBuf,
    },
}

#[derive(Serialize)]
struct MeasureSummary {
    phash_max: u32,
    pixel_diff_max: f64,
    pairs: usize,
}

/// Map filename -> full path for every regular file directly in `dir`.
fn files_by_name(dir: &std::path::Path) -> Result<BTreeMap<String, PathBuf>> {
    let mut map = BTreeMap::new();
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        if entry.file_type()?.is_file() {
            if let Some(name) = entry.file_name().to_str() {
                map.insert(name.to_string(), entry.path());
            }
        }
    }
    Ok(map)
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Phash { img1, img2 } => {
            println!("{}", dist::phash_distance(&img1, &img2)?);
        }
        Command::Pixel { img1, img2 } => {
            println!("{}", dist::pixel_diff(&img1, &img2)?);
        }
        Command::Floor { img_path } => {
            let nf = dist::noise_floor(&img_path)?;
            println!("{}", serde_json::to_string_pretty(&nf)?);
        }
        Command::Measure { r0, r1 } => {
            let a = files_by_name(&r0)?;
            let b = files_by_name(&r1)?;
            let mut phash_max = 0u32;
            let mut pixel_diff_max = 0.0f64;
            let mut pairs = 0usize;
            for (name, pa) in &a {
                let Some(pb) = b.get(name) else { continue };
                phash_max = phash_max.max(dist::phash_distance(pa, pb)?);
                pixel_diff_max = pixel_diff_max.max(dist::pixel_diff(pa, pb)?);
                pairs += 1;
            }
            let summary = MeasureSummary {
                phash_max,
                pixel_diff_max,
                pairs,
            };
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
    }
    Ok(())
}
