"""Perceptual hashing for visual similarity matching — Pillow only.

Computes perceptual hashes (pHash) from images and compares them against
a pre-built database of known subjects (memes, characters, celebrities).
Zero external dependencies — uses only Pillow.

The hash database (data/known-subjects.json) is built during development
using CLIP or other tools, but at runtime only Pillow is needed.
"""

import json
import pathlib

from PIL import Image

from afs.config import PROJECT_ROOT


_DB_PATH = PROJECT_ROOT / "data" / "known-subjects.json"
_HASH_SIZE = 16  # 16x16 = 256-bit hash


def compute_phash(image_path: pathlib.Path, hash_size: int = _HASH_SIZE) -> str | None:
    """Compute perceptual hash of an image. Returns hex string or None on error.

    Algorithm:
    1. Convert to grayscale
    2. Resize to hash_size x hash_size (destroys detail, preserves structure)
    3. Compute average pixel value
    4. Each pixel above average = 1, below = 0
    5. Convert bit string to hex
    """
    try:
        with Image.open(image_path) as img:
            img = img.convert("L").resize((hash_size, hash_size), Image.LANCZOS)
            pixels = list(img.getdata())
            avg = sum(pixels) / len(pixels)
            bits = "".join("1" if p > avg else "0" for p in pixels)
            return hex(int(bits, 2))
    except Exception:
        return None


def compute_phash_from_bytes(image_bytes: bytes, hash_size: int = _HASH_SIZE) -> str | None:
    """Compute perceptual hash from raw image bytes."""
    import io
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("L").resize((hash_size, hash_size), Image.LANCZOS)
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        bits = "".join("1" if p > avg else "0" for p in pixels)
        return hex(int(bits, 2))
    except Exception:
        return None


def hamming_distance(hash1: str, hash2: str) -> int:
    """Count differing bits between two hex hash strings."""
    try:
        h1, h2 = int(hash1, 16), int(hash2, 16)
        return bin(h1 ^ h2).count("1")
    except (ValueError, TypeError):
        return 999  # max distance on error


def load_known_subjects(db_path: pathlib.Path | None = None) -> dict:
    """Load the known-subjects database. Returns empty dict if not found."""
    path = db_path or _DB_PATH
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def match_known_subjects(
    image_path: pathlib.Path,
    db: dict | None = None,
    max_distance: int = 25,
) -> list[tuple[str, int, dict]]:
    """Match an image against the known-subjects database.

    Returns list of (name, distance, entry) tuples, sorted by distance.
    Only returns matches within max_distance (lower = more similar).

    Typical thresholds:
    - 0-10: very strong match (nearly identical)
    - 10-20: strong match (same character, different pose)
    - 20-30: weak match (similar style/composition)
    - 30+: no match
    """
    if db is None:
        db = load_known_subjects()

    subjects = db.get("subjects", {})
    if not subjects:
        return []

    target_hash = compute_phash(image_path)
    if not target_hash:
        return []

    matches = []
    for name, entry in subjects.items():
        threshold = entry.get("hash_threshold", max_distance)
        for ref_hash in entry.get("hashes", []):
            dist = hamming_distance(target_hash, ref_hash)
            if dist <= threshold:
                matches.append((name, dist, entry))
                break  # one match per subject is enough

    return sorted(matches, key=lambda x: x[1])


def save_known_subjects(db: dict, db_path: pathlib.Path | None = None):
    """Write the known-subjects database."""
    path = db_path or _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(db, indent=2) + "\n", encoding="utf-8")
