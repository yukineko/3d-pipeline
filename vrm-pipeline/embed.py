"""
embed.py - DINOv2 / CLIP embedding extraction for VRM render outputs.

Usage:
    python embed.py \\
        --render-dir <dir>          # render.py output directory
        --model dinov2-small|clip   # model to use (default: dinov2-small)
        --output <path>             # output JSON path (default: <render-dir>/embed.json)
        [--faces face_front,face_34]  # restrict faces (default: all 7)
"""

import argparse
import json
import os
import sys

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_FACES = [
    "body_front",
    "body_side",
    "body_back",
    "face_front",
    "face_L",
    "face_R",
    "face_34",
]

MODEL_CONFIG = {
    "dinov2-small": {
        "hf_id": "facebook/dinov2-small",
        "dim": 768,
    },
    "clip": {
        "hf_id": "openai/clip-vit-base-patch32",
        "dim": 512,
    },
}


# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------

def select_device():
    """
    Select the best available compute device.
    MPS (Apple Silicon) -> CUDA -> CPU, with automatic fallback to CPU on
    errors at runtime.
    """
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    elif torch.cuda.is_available():
        return "cuda"
    else:
        return "cpu"


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def load_model(model_name: str, device: str):
    """
    Load and return (processor, model) for the given model_name.
    Raises ValueError for unknown model names.
    """
    if model_name not in MODEL_CONFIG:
        raise ValueError(
            f"Unknown model '{model_name}'. "
            f"Valid choices: {list(MODEL_CONFIG.keys())}"
        )

    cfg = MODEL_CONFIG[model_name]
    hf_id = cfg["hf_id"]

    if model_name == "dinov2-small":
        from transformers import AutoImageProcessor, AutoModel

        processor = AutoImageProcessor.from_pretrained(hf_id)
        model = AutoModel.from_pretrained(hf_id).to(device)
    elif model_name == "clip":
        from transformers import CLIPProcessor, CLIPModel

        processor = CLIPProcessor.from_pretrained(hf_id)
        model = CLIPModel.from_pretrained(hf_id).to(device)
    else:
        raise ValueError(f"Unhandled model_name: {model_name}")

    model.eval()
    return processor, model


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def embed_image(img, model_name: str, processor, model, device: str):
    """
    Run inference on a PIL Image and return the embedding as a list of floats.

    For DINOv2: uses the [CLS] token from last_hidden_state.
    For CLIP:   uses get_image_features().

    Raises RuntimeError if inference fails even on CPU fallback.
    """
    import torch

    def _run(dev):
        inputs = processor(images=img, return_tensors="pt").to(dev)
        with torch.no_grad():
            if model_name == "dinov2-small":
                outputs = model(**inputs)
                emb = outputs.last_hidden_state[:, 0, :].squeeze().cpu().numpy()
            elif model_name == "clip":
                emb = model.get_image_features(**inputs).squeeze().cpu().numpy()
            else:
                raise ValueError(f"Unhandled model_name: {model_name}")
        return emb.tolist()

    # Try on requested device; fall back to CPU on MPS errors
    try:
        return _run(device)
    except RuntimeError as exc:
        if device == "mps":
            print(
                f"[embed.py] WARNING: MPS inference failed ({exc}), "
                "falling back to CPU.",
                file=sys.stderr,
            )
            model.to("cpu")
            result = _run("cpu")
            # Move model back to MPS for subsequent calls if desired
            try:
                model.to(device)
            except Exception:
                pass
            return result
        raise


# ---------------------------------------------------------------------------
# Embedding computation for a directory
# ---------------------------------------------------------------------------

def compute_embeddings(render_dir: str, model_name: str, face_names: list):
    """
    Load images from render_dir, compute per-face embeddings and a mean
    record_embedding.

    Returns a dict ready to be serialised as the output JSON.
    Missing face files are silently skipped.
    """
    from PIL import Image

    device = select_device()
    print(f"[embed.py] Using device: {device}", file=sys.stderr)

    processor, model = load_model(model_name, device)

    dim = MODEL_CONFIG[model_name]["dim"]
    face_embeddings = {}

    for face in face_names:
        img_path = os.path.join(render_dir, face + ".webp")
        if not os.path.isfile(img_path):
            print(
                f"[embed.py] Skipping '{face}': file not found ({img_path})",
                file=sys.stderr,
            )
            continue

        try:
            img = Image.open(img_path).convert("RGB")
        except Exception as exc:
            print(
                f"[embed.py] WARNING: Could not open '{img_path}': {exc}",
                file=sys.stderr,
            )
            continue

        try:
            emb = embed_image(img, model_name, processor, model, device)
        except Exception as exc:
            print(
                f"[embed.py] WARNING: Embedding failed for '{face}': {exc}",
                file=sys.stderr,
            )
            continue

        face_embeddings[face] = emb
        print(f"[embed.py] Embedded {face} ({len(emb)}d)", file=sys.stderr)

    if not face_embeddings:
        print(
            "[embed.py] WARNING: No faces were successfully embedded. "
            "record_embedding will be empty.",
            file=sys.stderr,
        )
        record_embedding = []
    else:
        # Compute mean vector across all embedded faces
        import numpy as np

        matrix = np.array(list(face_embeddings.values()), dtype=float)
        record_embedding = matrix.mean(axis=0).tolist()

    return {
        "model": model_name,
        "dim": dim,
        "faces": face_embeddings,
        "record_embedding": record_embedding,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract DINOv2 or CLIP embeddings from VRM render outputs."
    )
    parser.add_argument(
        "--render-dir",
        required=True,
        help="Directory produced by render.py (contains *.webp files).",
    )
    parser.add_argument(
        "--model",
        default="dinov2-small",
        choices=list(MODEL_CONFIG.keys()),
        help="Embedding model to use (default: dinov2-small).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Output JSON path. "
            "Defaults to <render-dir>/embed.json when omitted."
        ),
    )
    parser.add_argument(
        "--faces",
        default=None,
        help=(
            "Comma-separated list of face names to embed "
            "(default: all 7 faces). "
            "Example: --faces face_front,face_34"
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()

    render_dir = os.path.abspath(args.render_dir)
    if not os.path.isdir(render_dir):
        print(
            f"[embed.py] ERROR: --render-dir '{render_dir}' does not exist.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve output path
    if args.output is not None:
        output_path = os.path.abspath(args.output)
    else:
        output_path = os.path.join(render_dir, "embed.json")

    # Resolve face list
    if args.faces:
        face_names = [f.strip() for f in args.faces.split(",") if f.strip()]
        unknown = [f for f in face_names if f not in ALL_FACES]
        if unknown:
            print(
                f"[embed.py] WARNING: Unknown face name(s): {unknown}. "
                f"Valid names: {ALL_FACES}",
                file=sys.stderr,
            )
    else:
        face_names = ALL_FACES

    # Compute
    result = compute_embeddings(render_dir, args.model, face_names)

    # Write
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"[embed.py] Embeddings written to {output_path}", file=sys.stderr)

    # Echo JSON summary to stdout for caller consumption
    summary = {
        "model": result["model"],
        "dim": result["dim"],
        "faces_embedded": list(result["faces"].keys()),
        "output": output_path,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
