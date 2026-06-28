import AppKit
import SwiftUI

/// Loads a representative thumbnail from a record's render directory (r0_ref).
///
/// The Rust/Blender pipeline renders canonical face/body views as WebP
/// (render/vrm.py: face_front, body_front, face_L/R/34, body_side/back).
/// NSImage decodes WebP natively via ImageIO on macOS 11+. We prefer a
/// front portrait, then a body front, then any image in the directory.
enum ThumbnailLoader {
    static let cache = NSCache<NSString, NSImage>()

    private static let preferredFaces = [
        "face_front", "body_front", "face_34", "face_L", "face_R", "body_side", "body_back",
    ]
    private static let extensions = ["webp", "png", "jpg", "jpeg"]

    /// Resolve the best thumbnail file path within `dir`, or nil if none.
    static func representativePath(inDir dir: String) -> String? {
        guard !dir.isEmpty else { return nil }
        let fm = FileManager.default
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: dir, isDirectory: &isDir), isDir.boolValue else { return nil }

        for face in preferredFaces {
            for ext in extensions {
                let p = (dir as NSString).appendingPathComponent("\(face).\(ext)")
                if fm.fileExists(atPath: p) { return p }
            }
        }
        if let items = try? fm.contentsOfDirectory(atPath: dir) {
            for item in items.sorted() {
                let lower = item.lowercased()
                if extensions.contains(where: { lower.hasSuffix(".\($0)") }) {
                    return (dir as NSString).appendingPathComponent(item)
                }
            }
        }
        return nil
    }

    static func image(forDir dir: String) -> NSImage? {
        guard let path = representativePath(inDir: dir) else { return nil }
        if let cached = cache.object(forKey: path as NSString) { return cached }
        guard let img = NSImage(contentsOfFile: path) else { return nil }
        cache.setObject(img, forKey: path as NSString)
        return img
    }
}

/// A square thumbnail well: shows the record's render thumbnail, or a cube
/// placeholder when the render dir is empty/missing (never crashes).
struct ThumbnailView: View {
    let dir: String
    var cornerRadius: CGFloat = 6
    @State private var image: NSImage?

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: cornerRadius)
                .fill(Color.secondary.opacity(0.12))
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .interpolation(.medium)
                    .aspectRatio(contentMode: .fill)
            } else {
                Image(systemName: "cube.transparent")
                    .foregroundStyle(.tertiary)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius))
        .task(id: dir) {
            let dir = self.dir
            let loaded = await Task.detached(priority: .utility) {
                ThumbnailLoader.image(forDir: dir)
            }.value
            self.image = loaded
        }
    }
}
