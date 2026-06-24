use std::path::Path;

use image::{ImageBuffer, Luma, Rgb};
use tempfile::TempDir;

/// Save a solid-color RGB image to disk as PNG (using image v0.25) and return the path.
fn save_solid_png(
    dir: &Path,
    filename: &str,
    r: u8,
    g: u8,
    b: u8,
    w: u32,
    h: u32,
) -> std::path::PathBuf {
    let buf: ImageBuffer<Rgb<u8>, Vec<u8>> = ImageBuffer::from_fn(w, h, |_, _| Rgb([r, g, b]));
    let path = dir.join(filename);
    buf.save(&path).expect("save test image");
    path
}

/// Save a horizontal gradient PNG: left=dark, right=bright.
fn save_gradient_png(dir: &Path, filename: &str, w: u32, h: u32, bright: bool) -> std::path::PathBuf {
    let buf: ImageBuffer<Luma<u8>, Vec<u8>> = ImageBuffer::from_fn(w, h, |x, _y| {
        let v = if bright {
            (x * 255 / w.max(1)) as u8
        } else {
            255 - (x * 255 / w.max(1)) as u8
        };
        Luma([v])
    });
    let path = dir.join(filename);
    buf.save(&path).expect("save test image");
    path
}

#[test]
fn phash_same_image_is_zero() {
    let dir = TempDir::new().unwrap();
    let p = save_gradient_png(dir.path(), "a.png", 64, 64, true);
    let d = dist::phash_distance(&p, &p).unwrap();
    assert_eq!(d, 0, "same image phash distance should be 0, got {}", d);
}

#[test]
fn phash_different_images_gt_zero() {
    let dir = TempDir::new().unwrap();
    // Left-bright vs right-bright gradient → opposite patterns → different hashes
    let p1 = save_gradient_png(dir.path(), "grad_bright.png", 64, 64, true);
    let p2 = save_gradient_png(dir.path(), "grad_dark.png", 64, 64, false);
    let d = dist::phash_distance(&p1, &p2).unwrap();
    assert!(
        d > 0,
        "mirrored gradients phash distance should be >0, got {}",
        d
    );
}

#[test]
fn pixel_diff_same_image_is_zero() {
    let dir = TempDir::new().unwrap();
    let p = save_solid_png(dir.path(), "green.png", 0, 200, 0, 32, 32);
    let d = dist::pixel_diff(&p, &p).unwrap();
    assert!(
        d < 1e-9,
        "same image pixel_diff should be ~0, got {}",
        d
    );
}

#[test]
fn pixel_diff_different_images_gt_zero() {
    let dir = TempDir::new().unwrap();
    let p1 = save_solid_png(dir.path(), "white.png", 255, 255, 255, 32, 32);
    let p2 = save_solid_png(dir.path(), "black.png", 0, 0, 0, 32, 32);
    let d = dist::pixel_diff(&p1, &p2).unwrap();
    assert!(d > 0.0, "white vs black pixel_diff should be >0, got {}", d);
}

#[test]
fn noise_floor_same_image_is_zero() {
    let dir = TempDir::new().unwrap();
    let p = save_gradient_png(dir.path(), "test.png", 64, 64, true);
    let nf = dist::noise_floor(&p).unwrap();
    assert_eq!(
        nf.phash_max, 0,
        "noise_floor phash_max should be 0 for same image"
    );
    assert!(
        nf.pixel_diff_max < 1e-9,
        "noise_floor pixel_diff_max should be ~0, got {}",
        nf.pixel_diff_max
    );
}
