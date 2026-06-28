//! Image distance metrics for comparing render outputs.
//!
//! Two complementary signals are provided:
//! - [`phash_distance`]: a perceptual (difference-) hash Hamming distance that is
//!   robust to small re-encode / resampling jitter but sensitive to structural
//!   change.
//! - [`pixel_diff`]: a normalized mean absolute per-channel pixel difference that
//!   captures fine-grained color/brightness drift.

use std::path::Path;

use image::imageops::FilterType;

/// Identity noise floor for an image: the distance metrics measured against the
/// image itself.
///
/// For an unchanged image both fields are zero (the baseline floor). Real
/// re-encode / re-render jitter calibration would build on top of
/// [`phash_distance`] / [`pixel_diff`] by comparing an image to a re-encoded or
/// re-rendered copy of itself; this struct is the trivial unchanged-image
/// baseline those calibrations sit above.
#[derive(Debug, Clone, serde::Serialize)]
pub struct NoiseFloor {
    /// Maximum perceptual-hash Hamming distance observed (0 for an unchanged image).
    pub phash_max: u32,
    /// Maximum normalized pixel difference observed (~0 for an unchanged image).
    pub pixel_diff_max: f64,
}

/// Compute a 64-bit difference hash (dHash) of the image at `path`.
///
/// The image is converted to grayscale and resized to 9x8; for each of the 8
/// rows and each of the 8 adjacent column pairs a bit is set when the left pixel
/// is darker than the right one.
fn dhash(path: &Path) -> anyhow::Result<u64> {
    let img = image::open(path)?.to_luma8();
    let small = image::imageops::resize(&img, 9, 8, FilterType::Triangle);
    let mut hash: u64 = 0;
    let mut bit = 0u32;
    for y in 0..8u32 {
        for x in 0..8u32 {
            let left = small.get_pixel(x, y)[0];
            let right = small.get_pixel(x + 1, y)[0];
            if left < right {
                hash |= 1u64 << bit;
            }
            bit += 1;
        }
    }
    Ok(hash)
}

/// Perceptual-hash (dHash) Hamming distance between two images.
///
/// Returns 0 for identical images and a large value for structurally different
/// ones (e.g. horizontally mirrored gradients).
pub fn phash_distance(a: &Path, b: &Path) -> anyhow::Result<u32> {
    let ha = dhash(a)?;
    let hb = dhash(b)?;
    Ok((ha ^ hb).count_ones())
}

/// Normalized mean absolute per-channel pixel difference between two images.
///
/// Both images are loaded as RGB8; if their dimensions differ, `b` is resized to
/// `a`'s dimensions. The result is the mean over all pixels and channels of the
/// absolute channel difference, divided by 255.0 — so 0.0 for identical images
/// and large (up to 1.0) for white-vs-black.
pub fn pixel_diff(a: &Path, b: &Path) -> anyhow::Result<f64> {
    let ia = image::open(a)?.to_rgb8();
    let mut ib = image::open(b)?.to_rgb8();
    if ia.dimensions() != ib.dimensions() {
        let (w, h) = ia.dimensions();
        ib = image::imageops::resize(&ib, w, h, FilterType::Triangle);
    }
    let mut sum = 0.0f64;
    let mut count = 0u64;
    for (pa, pb) in ia.pixels().zip(ib.pixels()) {
        for c in 0..3 {
            sum += (pa[c] as f64 - pb[c] as f64).abs();
            count += 1;
        }
    }
    if count == 0 {
        return Ok(0.0);
    }
    Ok(sum / count as f64 / 255.0)
}

/// Compute the identity [`NoiseFloor`] for the image at `p`.
///
/// This is the unchanged-image baseline floor: both metrics are measured against
/// the image itself, yielding zero. Real re-encode / re-render jitter
/// calibration would build on [`phash_distance`] / [`pixel_diff`] by comparing
/// against re-encoded or re-rendered copies.
pub fn noise_floor(p: &Path) -> anyhow::Result<NoiseFloor> {
    Ok(NoiseFloor {
        phash_max: phash_distance(p, p)?,
        pixel_diff_max: pixel_diff(p, p)?,
    })
}
