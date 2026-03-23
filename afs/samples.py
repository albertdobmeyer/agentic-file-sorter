"""Sample-based identification via text descriptions — zero new dependencies.

Instead of sending sample IMAGES alongside every target (clogging context),
samples are pre-described as text by the vision model (one-time, cached).
During processing, descriptions are included as TEXT in the vision prompt.

Users place reference samples in samples/ directory:
  - Flat:   samples/hatman.png              → one sample for "hatman"
  - Subdir: samples/tori/green.jpg, red.jpg → multiple samples for "tori"
"""

import base64
import io
import json
import pathlib
from typing import Callable

import requests
from PIL import Image

from afs.analyze import parse_json
from afs.config import PROJECT_ROOT
from afs.types_ import TIER_1_EXTENSIONS


_SAMPLE_MAX_SIZE = (512, 512)
_METADATA_FILE = "samples.json"


# --- Public API ---


def has_samples(config: dict | None = None) -> bool:
    """Quick check: does the samples directory exist and contain image files?"""
    samples_dir = _get_samples_dir(config)
    if not samples_dir.is_dir():
        return False
    for entry in samples_dir.iterdir():
        if entry.is_file() and entry.suffix.lower() in TIER_1_EXTENSIONS:
            return True
        if entry.is_dir() and not entry.name.startswith("."):
            if any(f.suffix.lower() in TIER_1_EXTENSIONS
                   for f in entry.iterdir() if f.is_file()):
                return True
    return False


def list_samples(config: dict | None = None) -> dict[str, int]:
    """Return {name: image_count} for all sample groups."""
    samples_dir = _get_samples_dir(config)
    if not samples_dir.is_dir():
        return {}
    scanned = _scan_samples_dir(samples_dir)
    return {name: len(paths) for name, paths in scanned.items()}


def load_sample_descriptions(
    config: dict | None = None,
    selected: list[str] | None = None,
) -> dict[str, str]:
    """Load cached text descriptions for selected samples.

    Returns {name: description_text}. Only returns selected samples.
    If no descriptions cached, returns empty (run check-samples first).
    """
    if not selected:
        return {}

    samples_dir = _get_samples_dir(config)
    metadata = _load_metadata(samples_dir)
    selected_lower = {s.lower() for s in selected}

    result = {}
    for name, entry in metadata.items():
        if name in selected_lower and entry.get("description"):
            result[name] = entry["description"]

    return result


def describe_sample(
    name: str,
    config: dict | None = None,
    on_event: Callable | None = None,
) -> dict:
    """Analyze a sample group with the vision model. Stores description in metadata.

    Returns {name, description, quality, sample_count}.
    """
    cfg = config or {}
    models = cfg.get("models", {})
    ollama_url = models.get("ollama_url", "http://localhost:11434")
    vision_model = models.get("vision_model", "llava:latest")
    vision_timeout = models.get("vision_timeout", 180)
    keep_alive = models.get("keep_alive", "30m")

    samples_dir = _get_samples_dir(config)
    scanned = _scan_samples_dir(samples_dir)

    if name not in scanned:
        return {"name": name, "error": "Sample group not found"}

    paths = scanned[name]

    # Use the first (best) sample image for description
    b64 = _encode_sample(paths[0])
    if not b64:
        return {"name": name, "error": "Cannot read sample image"}

    prompt = """/no_think
Describe this image in detail for identification purposes.
Focus on: distinctive visual features, colors, shapes, patterns, style.
If this is a person: describe face shape, hair, skin tone, distinguishing marks.
If this is a character: describe art style, colors, distinctive features.
If this is an object: describe shape, color, texture, distinguishing details.

Respond with ONLY a JSON object:
{
  "description": "2-3 sentence detailed description for identification",
  "subject_type": "person | character | object | scene",
  "quality_score": 0.0 to 1.0,
  "suggestion": "one sentence on how to improve this sample (or 'good sample' if fine)"
}"""

    try:
        resp = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": vision_model,
                "prompt": prompt,
                "images": [b64],
                "stream": False,
                "options": {"num_ctx": 2048, "temperature": 0.1},
                "keep_alive": keep_alive,
            },
            timeout=(30, vision_timeout),
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
    except Exception as e:
        return {"name": name, "error": str(e)}

    data = parse_json(raw)
    if not data:
        return {"name": name, "error": "Unparseable response"}

    description = data.get("description", "")
    if isinstance(description, list):
        description = " ".join(str(d) for d in description)
    quality_score = float(data.get("quality_score", 0.5))
    suggestion = data.get("suggestion", "")
    subject_type = data.get("subject_type", "unknown")

    # Map score to rating
    if quality_score >= 0.8:
        rating = "green"
    elif quality_score >= 0.5:
        rating = "blue"
    elif quality_score >= 0.3:
        rating = "orange"
    else:
        rating = "red"

    # Check resolution of first sample
    try:
        with Image.open(paths[0]) as img:
            w, h = img.size
        if min(w, h) < 128:
            suggestion = f"Low resolution ({w}x{h}). {suggestion}"
            if rating == "green":
                rating = "blue"
    except Exception:
        pass

    # Store in metadata
    metadata = _load_metadata(samples_dir)
    metadata[name] = {
        "samples": [p.name for p in paths],
        "description": description,
        "subject_type": subject_type,
        "quality": {
            "rating": rating,
            "score": round(quality_score, 2),
            "suggestion": suggestion,
        },
    }
    _save_metadata(samples_dir, metadata)

    return {
        "name": name,
        "description": description,
        "subject_type": subject_type,
        "rating": rating,
        "score": quality_score,
        "suggestion": suggestion,
        "sample_count": len(paths),
    }


def describe_all_samples(config: dict | None = None, on_event: Callable | None = None) -> list[dict]:
    """Describe all sample groups. Returns list of results."""
    samples_dir = _get_samples_dir(config)
    scanned = _scan_samples_dir(samples_dir)
    results = []
    for name in sorted(scanned):
        result = describe_sample(name, config=config, on_event=on_event)
        results.append(result)
        if on_event:
            on_event({"event": "check-sample", "name": name, **result})
    return results


# --- Backward compatibility aliases ---

def has_face_samples(config=None):
    return has_samples(config)

def load_face_samples(config=None):
    """Legacy: load all sample images as base64 (for old multi-image approach)."""
    samples_dir = _get_samples_dir(config)
    if not samples_dir.is_dir():
        return {}
    scanned = _scan_samples_dir(samples_dir)
    result: dict[str, list[str]] = {}
    for name, paths in scanned.items():
        images = []
        for p in paths[:3]:
            b64 = _encode_sample(p)
            if b64:
                images.append(b64)
        if images:
            result[name] = images
    return result

def identify_faces(preview_path, face_samples, config=None):
    """Legacy: kept for backward compatibility. Returns empty — use text descriptions instead."""
    return []


# --- Internal ---


def _scan_samples_dir(samples_dir: pathlib.Path) -> dict[str, list[pathlib.Path]]:
    """Scan samples directory, returning {name: [path, ...]}."""
    samples: dict[str, list[pathlib.Path]] = {}
    if not samples_dir.is_dir():
        return samples
    for entry in sorted(samples_dir.iterdir()):
        if entry.name.startswith(".") or entry.name == _METADATA_FILE:
            continue
        if entry.is_dir():
            name = entry.name.lower().replace("_", "-").replace(" ", "-")
            files = [f for f in sorted(entry.iterdir())
                     if f.is_file() and f.suffix.lower() in TIER_1_EXTENSIONS]
            if files:
                samples[name] = files
        elif entry.is_file() and entry.suffix.lower() in TIER_1_EXTENSIONS:
            name = entry.stem.lower().replace("_", "-").replace(" ", "-")
            samples.setdefault(name, []).append(entry)
    return samples


def _load_metadata(samples_dir: pathlib.Path) -> dict:
    """Load samples.json metadata file."""
    path = samples_dir / _METADATA_FILE
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_metadata(samples_dir: pathlib.Path, metadata: dict):
    """Write samples.json metadata file."""
    path = samples_dir / _METADATA_FILE
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def _encode_sample(path: pathlib.Path) -> str | None:
    """Encode a sample image to base64 PNG, resized to 512x512 max."""
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail(_SAMPLE_MAX_SIZE, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return None


def _get_samples_dir(config: dict | None = None) -> pathlib.Path:
    """Resolve the samples directory from config or default."""
    cfg = config or {}
    samples_dir = cfg.get("processing", {}).get("samples_dir", "")
    if samples_dir:
        return pathlib.Path(samples_dir)
    faces_dir = cfg.get("processing", {}).get("faces_dir", "")
    if faces_dir:
        return pathlib.Path(faces_dir)
    samples_path = PROJECT_ROOT / "samples"
    if samples_path.is_dir():
        return samples_path
    faces_path = PROJECT_ROOT / "faces"
    if faces_path.is_dir():
        return faces_path
    return samples_path
