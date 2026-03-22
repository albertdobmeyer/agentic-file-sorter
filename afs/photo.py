"""Photo detection — distinguish camera photos from memes/screenshots.

Photos get CDR skipped (original bytes preserved) because re-rendering
can degrade high-res images and strips valuable EXIF metadata.

Detection signals (ordered by reliability):
1. EXIF camera metadata (Make/Model tags)
2. Resolution threshold (default 4MP — memes are <2MP, phones start at 8MP)
3. Camera filename pattern (IMG_, DSC_, PXL_, etc.)
"""

import pathlib
import re

from PIL import Image


# EXIF tags for camera identification
_EXIF_MAKE = 0x010F   # Tag 271: camera manufacturer
_EXIF_MODEL = 0x0110  # Tag 272: camera model

# Camera filename patterns (compiled once)
CAMERA_FILENAME_RE = re.compile(
    r"^("
    r"IMG[-_]|"           # iOS, Android, Samsung
    r"DSC[-_N]|"          # Sony, Nikon (DSC_, DSCN)
    r"DCIM[-_]?|"         # Generic DCIM cameras
    r"DSCF|"              # Fujifilm
    r"P\d{4,}|"           # Panasonic (P1000xxxx)
    r"_MG_|"              # Canon (underscore prefix)
    r"PXL_|"              # Google Pixel
    r"GOPR|"              # GoPro
    r"DJI_|"              # DJI drones
    r"SAM_|"              # Samsung older models
    r"MVIMG_"             # Android motion photo
    r")",
    re.IGNORECASE,
)

# Sequence extraction: captures date/number portions from camera filenames
_SEQUENCE_RE = re.compile(
    r"^(?:IMG|DSC|DSCN|DSCF|PXL|GOPR|DJI|SAM|MVIMG|DCIM|_MG)"
    r"[-_]?"
    r"(\d{8}[-_]?\d{1,4}|\d{3,6})",
    re.IGNORECASE,
)


def is_likely_photo(path: pathlib.Path, config: dict | None = None) -> bool:
    """Detect if a file is likely a camera photo (vs meme/screenshot).

    Uses three signals in order: EXIF metadata, resolution, filename pattern.
    Returns False on any error (safe default: apply CDR).
    """
    cfg = config or {}
    threshold_mp = cfg.get("processing", {}).get("photo_threshold_mp", 4.0)

    try:
        with Image.open(path) as img:
            # Signal 1: EXIF camera metadata (most reliable)
            exif = img.getexif()
            if exif:
                make = exif.get(_EXIF_MAKE, "")
                model = exif.get(_EXIF_MODEL, "")
                if make or model:
                    return True

            # Signal 2: Resolution check (memes are <2MP, photos are 8-50MP)
            w, h = img.size
            megapixels = (w * h) / 1_000_000
            if megapixels >= threshold_mp:
                return True

    except Exception:
        pass  # corrupt file, non-image — fall through to filename check

    # Signal 3: Camera filename pattern
    stem = path.stem
    if CAMERA_FILENAME_RE.match(stem):
        # Exclude screenshots
        if stem.lower().startswith("screenshot"):
            return False
        return True

    return False


def extract_photo_sequence(stem: str) -> str | None:
    """Extract the date/sequence portion from a camera filename.

    Examples:
        IMG_20250312_073 → "20250312-073"
        DSC_4521         → "4521"
        PXL_20250312     → "20250312"
        spongebob-meme   → None
    """
    match = _SEQUENCE_RE.match(stem)
    if not match:
        return None

    seq = match.group(1)
    # Normalize separators to hyphen
    seq = re.sub(r"[_\s]+", "-", seq).strip("-")
    return seq if seq else None
