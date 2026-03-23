"""Face recognition via sample images — zero new dependencies.

Users place face samples in faces/ directory. Two conventions supported:
  - Flat:   faces/albert.jpg              → one sample for "albert"
  - Subdir: faces/tori/green.jpg, red.jpg → multiple samples for "tori"

The vision model receives the target photo + all reference images and
identifies which named people appear. Uses Ollama's multi-image API.
"""

import base64
import io
import pathlib
from typing import Callable

import requests
from PIL import Image

from afs.analyze import parse_json
from afs.config import PROJECT_ROOT
from afs.types_ import TIER_1_EXTENSIONS


# Max dimensions for face sample thumbnails (keeps API payload reasonable)
_SAMPLE_MAX_SIZE = (512, 512)


def has_face_samples(config: dict | None = None) -> bool:
    """Quick check: does the faces directory exist and contain image files?"""
    faces_dir = _get_faces_dir(config)
    if not faces_dir.is_dir():
        return False
    for entry in faces_dir.iterdir():
        if entry.is_file() and entry.suffix.lower() in TIER_1_EXTENSIONS:
            return True
        if entry.is_dir() and not entry.name.startswith("."):
            if any(f.suffix.lower() in TIER_1_EXTENSIONS
                   for f in entry.iterdir() if f.is_file()):
                return True
    return False


def load_face_samples(config: dict | None = None) -> dict[str, list[str]]:
    """Load face sample images, returning {name: [base64_image, ...]}.

    Supports two conventions:
      - Flat file:   faces/albert.jpg → {"albert": [b64]}
      - Subdirectory: faces/tori/*.jpg → {"tori": [b64_1, b64_2, ...]}

    Images are resized to 512x512 max and encoded as base64 PNG.
    Returns empty dict if directory doesn't exist or has no images.
    """
    faces_dir = _get_faces_dir(config)
    if not faces_dir.is_dir():
        return {}

    samples: dict[str, list[str]] = {}

    for entry in sorted(faces_dir.iterdir()):
        if entry.name.startswith("."):
            continue

        if entry.is_dir():
            # Subdirectory convention: faces/tori/*.jpg
            name = entry.name.lower().replace("_", "-").replace(" ", "-")
            images = []
            for f in sorted(entry.iterdir()):
                if f.is_file() and f.suffix.lower() in TIER_1_EXTENSIONS:
                    b64 = _encode_sample(f)
                    if b64:
                        images.append(b64)
            if images:
                samples[name] = images

        elif entry.is_file() and entry.suffix.lower() in TIER_1_EXTENSIONS:
            # Flat convention: faces/albert.jpg
            name = entry.stem.lower().replace("_", "-").replace(" ", "-")
            b64 = _encode_sample(entry)
            if b64:
                samples.setdefault(name, []).append(b64)

    return samples


def identify_faces(
    preview_path: pathlib.Path,
    face_samples: dict[str, list[str]],
    config: dict | None = None,
) -> list[str]:
    """Send target image + reference faces to vision model for matching.

    Returns list of matched person names (may be empty).
    """
    if not face_samples:
        return []

    cfg = config or {}
    models = cfg.get("models", {})
    ollama_url = models.get("ollama_url", "http://localhost:11434")
    vision_model = models.get("vision_model", "llava:latest")
    vision_timeout = models.get("vision_timeout", 180)
    keep_alive = models.get("keep_alive", "30m")

    # Build images array: target first, then all reference samples
    try:
        target_b64 = base64.b64encode(preview_path.read_bytes()).decode("utf-8")
    except Exception:
        return []

    images = [target_b64]
    ref_lines = []
    names = list(face_samples.keys())
    img_idx = 2  # target is Image 1

    for name in names:
        sample_list = face_samples[name]
        if len(sample_list) == 1:
            ref_lines.append(f"Image {img_idx} is a reference photo of {name}")
            images.append(sample_list[0])
            img_idx += 1
        else:
            start = img_idx
            for b64 in sample_list:
                images.append(b64)
                img_idx += 1
            end = img_idx - 1
            ref_lines.append(f"Images {start}-{end} are reference photos of {name} (different appearances)")

    ref_block = "\n".join(ref_lines)

    prompt = f"""/no_think
Image 1 is a TARGET photo. The remaining images are reference faces:
{ref_block}

Which of the named people (if any) appear in Image 1 (the TARGET)?

RULES:
- ONLY identify people you are confident about
- Compare FACIAL FEATURES (eyes, nose, mouth, face shape) not hair color or clothing
- A person may look different across reference images — that is intentional
- Return ONLY a JSON object: {{"people": ["name1", "name2"]}}
- If no named people appear, return: {{"people": []}}
- Do NOT guess — if unsure, do not include the name"""

    try:
        resp = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": vision_model,
                "prompt": prompt,
                "images": images,
                "stream": False,
                "options": {"num_ctx": 4096, "temperature": 0.1},
                "keep_alive": keep_alive,
            },
            timeout=(30, int(vision_timeout * 1.5)),
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
    except Exception:
        return []

    data = parse_json(raw)
    if not data or not isinstance(data, dict):
        return []

    people = data.get("people", [])
    if not isinstance(people, list):
        return []

    # Validate: only return names that match our samples
    valid_names = {n.lower() for n in names}
    return [str(p).lower().strip() for p in people
            if str(p).lower().strip() in valid_names]


def _encode_sample(path: pathlib.Path) -> str | None:
    """Encode a face sample image to base64 PNG, resized to 512x512 max."""
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail(_SAMPLE_MAX_SIZE, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return None


def _get_faces_dir(config: dict | None = None) -> pathlib.Path:
    """Resolve the samples/faces directory from config or default.

    Priority: config samples_dir > config faces_dir > samples/ > faces/
    """
    cfg = config or {}
    # New config key first
    samples_dir = cfg.get("processing", {}).get("samples_dir", "")
    if samples_dir:
        return pathlib.Path(samples_dir)
    # Legacy config key
    faces_dir = cfg.get("processing", {}).get("faces_dir", "")
    if faces_dir:
        return pathlib.Path(faces_dir)
    # Default: samples/ preferred, faces/ as fallback
    samples_path = PROJECT_ROOT / "samples"
    if samples_path.is_dir():
        return samples_path
    faces_path = PROJECT_ROOT / "faces"
    if faces_path.is_dir():
        return faces_path
    return samples_path
