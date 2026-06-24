use std::path::Path;

use anyhow::{Context, Result};
use img_hash::{HasherConfig, HashAlg};
use walkdir::WalkDir;

// img_hash re-exports its bundled image v0.23 as `img_hash::image`.
// We use it only for constructing the type expected by `Hasher::hash_image`.
use img_hash::image as img023;

// ---- public types ----------------------------------------------------------

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NoiseFloor {
    pub phash_max: u32,
    pub pixel_diff_max: f64,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct FaceResult {
    pub name: String,
    pub phash: u32,
    pub pixel_diff: f64,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DirPairResult {
    pub faces: Vec<FaceResult>,
    pub phash_mean: f64,
    pub pixel_diff_mean: f64,
}

// ---- helpers ---------------------------------------------------------------

fn make_hasher() -> img_hash::Hasher {
    HasherConfig::new()
        .hash_alg(HashAlg::Gradient)
        .hash_size(8, 8)
        .to_hasher()
}

/// Load an image using `image` v0.25 (which supports PNG, WebP, etc.)
/// and return it as a `image 0.23` RgbaImage so img_hash can process it.
fn load_as_img023_rgba(path: &Path) -> Result<img023::RgbaImage> {
    // Load with image v0.25
    let dyn_img = image::open(path)
        .with_context(|| format!("failed to open image: {}", path.display()))?;
    let rgba = dyn_img.to_rgba8();
    let (w, h) = rgba.dimensions();
    let raw: Vec<u8> = rgba.into_raw();
    // Reconstruct as img_hash::image (v0.23) RgbaImage
    let img023_buf = img023::ImageBuffer::<img023::Rgba<u8>, Vec<u8>>::from_raw(w, h, raw)
        .ok_or_else(|| anyhow::anyhow!("failed to create img023 buffer from raw pixels"))?;
    Ok(img023_buf)
}

// ---- public API ------------------------------------------------------------

/// Compute the perceptual hash distance between two images.
/// Returns 0 for identical images, >0 for different images.
pub fn phash_distance(img1: &Path, img2: &Path) -> Result<u32> {
    let hasher = make_hasher();
    let i1 = load_as_img023_rgba(img1)?;
    let i2 = load_as_img023_rgba(img2)?;
    let h1 = hasher.hash_image(&i1);
    let h2 = hasher.hash_image(&i2);
    Ok(h1.dist(&h2))
}

/// Compute the normalized pixel difference between two images (0.0 = identical, 1.0 = maximally different).
/// Images are compared after converting to RGBA8. Mismatched dimensions → error.
pub fn pixel_diff(img1: &Path, img2: &Path) -> Result<f64> {
    let a = image::open(img1)
        .with_context(|| format!("failed to open image: {}", img1.display()))?
        .to_rgba8();
    let b = image::open(img2)
        .with_context(|| format!("failed to open image: {}", img2.display()))?
        .to_rgba8();
    pixel_diff_rgba(&a, &b)
}

fn pixel_diff_rgba(
    a: &image::ImageBuffer<image::Rgba<u8>, Vec<u8>>,
    b: &image::ImageBuffer<image::Rgba<u8>, Vec<u8>>,
) -> Result<f64> {
    anyhow::ensure!(
        a.dimensions() == b.dimensions(),
        "image dimensions mismatch: {:?} vs {:?}",
        a.dimensions(),
        b.dimensions()
    );
    let (w, h) = a.dimensions();
    let total_pixels = w as u64 * h as u64;
    if total_pixels == 0 {
        return Ok(0.0);
    }
    let sum: u64 = a
        .pixels()
        .zip(b.pixels())
        .map(|(pa, pb)| {
            pa.0.iter()
                .zip(pb.0.iter())
                .map(|(&x, &y)| (x as i32 - y as i32).unsigned_abs() as u64)
                .sum::<u64>()
        })
        .sum();
    // max possible diff: 4 channels × 255 per pixel
    let max_diff = total_pixels * 4 * 255;
    Ok(sum as f64 / max_diff as f64)
}

/// Compute the noise floor by hashing the same image twice.
/// Since both hashes are identical, phash_max will always be 0
/// and pixel_diff_max will be 0 for the same image compared to itself.
pub fn noise_floor(img_path: &Path) -> Result<NoiseFloor> {
    let phash = phash_distance(img_path, img_path)?;
    let pd = pixel_diff(img_path, img_path)?;
    Ok(NoiseFloor {
        phash_max: phash,
        pixel_diff_max: pd,
    })
}

/// Pair up same-named .webp files from r0_dir and r1_dir and compute distances.
pub fn measure_dir_pair(r0_dir: &Path, r1_dir: &Path) -> Result<DirPairResult> {
    let hasher = make_hasher();
    let mut faces = Vec::new();

    for entry in WalkDir::new(r0_dir).min_depth(1).max_depth(1) {
        let entry = entry?;
        if entry.file_type().is_file() {
            let fname = entry.file_name().to_string_lossy().to_string();
            if !fname.ends_with(".webp") {
                continue;
            }
            let r1_path = r1_dir.join(&fname);
            if !r1_path.exists() {
                continue;
            }
            let r0_path = entry.path();

            let i0_rgba = load_as_img023_rgba(r0_path)?;
            let i1_rgba = load_as_img023_rgba(&r1_path)?;

            let h0 = hasher.hash_image(&i0_rgba);
            let h1 = hasher.hash_image(&i1_rgba);
            let phash = h0.dist(&h1);

            // For pixel_diff, load with image v0.25
            let a = image::open(r0_path)
                .with_context(|| format!("failed to open: {}", r0_path.display()))?
                .to_rgba8();
            let b = image::open(&r1_path)
                .with_context(|| format!("failed to open: {}", r1_path.display()))?
                .to_rgba8();
            let pd = pixel_diff_rgba(&a, &b)?;

            faces.push(FaceResult {
                name: fname,
                phash,
                pixel_diff: pd,
            });
        }
    }

    let n = faces.len() as f64;
    let (phash_mean, pixel_diff_mean) = if faces.is_empty() {
        (0.0, 0.0)
    } else {
        (
            faces.iter().map(|f| f.phash as f64).sum::<f64>() / n,
            faces.iter().map(|f| f.pixel_diff).sum::<f64>() / n,
        )
    };

    Ok(DirPairResult {
        faces,
        phash_mean,
        pixel_diff_mean,
    })
}
