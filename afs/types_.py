"""Shared types and tier classification for AFS.

Tier 1: Re-renderable images — CDR via PIL (decode pixels, write new file)
Tier 2: Irreplaceable visual files — frame extraction, original kept as-is
Tier 3: Everything else — no analysis, sorted by extension into filtered/
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileResult:
    source: str
    dest: str | None = None
    status: str = "error"  # moved | renamed | dry-run | error
    topic: str = ""
    keywords: list[str] = field(default_factory=list)
    confidence: float = 0.0
    method: str = ""  # vision | filtered
    identified: str | None = None
    folder: str = ""
    error: str | None = None
    error_type: str | None = None  # structured error category
    elapsed_ms: int = 0
    tier: int = 0
    original: str | None = None  # original filename before CDR renames
    photo_detected: bool = False  # True if is_likely_photo() identified a camera photo


@dataclass
class BatchResult:
    total: int = 0
    moved: int = 0
    errors: int = 0
    filtered: int = 0
    results: list[FileResult] = field(default_factory=list)
    elapsed_ms: int = 0


DANGEROUS_EXTENSIONS = {
    ".exe", ".dll", ".bat", ".cmd", ".ps1", ".vbs", ".scr", ".msi",
    ".com", ".pif", ".wsf", ".hta",
}

# Tier 1: Re-renderable images (CDR candidates)
# Note: static single-frame GIF is Tier 1, animated GIF is Tier 2 (checked at runtime)
TIER_1_EXTENSIONS = {".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".webp", ".tiff", ".tif"}

# Tier 2: Irreplaceable visual files (frame extraction, original untouched)
# .gif is handled specially — static = Tier 1, animated = Tier 2
TIER_2_EXTENSIONS = {".gif", ".mp4", ".webm", ".mov", ".avi", ".mkv"}


def classify_tier(path: Path) -> int:
    """Classify a file into processing tiers per the constitution.

    Tier 1: Re-renderable images (CDR candidates)
    Tier 2: Irreplaceable visual files (frame extraction)
    Tier 3: Everything else (no analysis, goes to filtered/)
    """
    ext = path.suffix.lower()

    if ext in TIER_1_EXTENSIONS:
        return 1

    if ext == ".gif":
        # Static GIF = Tier 1, animated GIF = Tier 2
        try:
            from PIL import Image
            with Image.open(path) as img:
                return 2 if getattr(img, "n_frames", 1) > 1 else 1
        except Exception:
            return 3

    if ext in TIER_2_EXTENSIONS:
        return 2

    return 3
