"""Preview generation and CDR (Content Disarm & Reconstruction) for AFS.

Two public functions:
- apply_cdr(): Re-render Tier 1 images via PIL, stripping non-pixel data
- generate_preview(): Produce a temp PNG for vision model analysis
"""

import pathlib
import subprocess
import tempfile


def apply_cdr(path: pathlib.Path, convert_webp: bool = True) -> pathlib.Path:
    """Re-render a Tier 1 image via PIL (CDR). Returns (possibly new) file path.

    PIL decodes pixel data and writes a new file from scratch. Non-pixel data
    (EXIF, embedded payloads, steganographic content) is stripped as a side effect.

    If convert_webp is True and the file is .webp, it's saved as .jpg instead.
    """
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")

        if convert_webp and path.suffix.lower() == ".webp":
            new_path = path.with_suffix(".jpg")
            img.save(str(new_path), format="JPEG", quality=95)
            path.unlink(missing_ok=True)
            return new_path

        # Re-render in place — same format, fresh pixel data only
        fmt = _pil_format(path.suffix.lower())
        img.save(str(path), format=fmt)

    return path


def generate_preview(path: pathlib.Path, tier: int) -> pathlib.Path | None:
    """Generate a temp PNG preview for the vision model.

    Tier 1: Resize image to temp PNG
    Tier 2: Extract representative frame via PIL (PSD/GIF) or ffmpeg (video)
    Tier 3: Not called (pipeline routes to filtered/)
    """
    if tier == 1:
        return _preview_image(path)
    if tier == 2:
        return _preview_tier2(path)
    return None


def _preview_image(path: pathlib.Path) -> pathlib.Path | None:
    """Generate preview from a PIL-openable image (Tier 1)."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            img = img.convert("RGB")
            max_dim = 1280
            if max(img.size) > max_dim:
                img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            return _save_temp_png(img)
    except Exception:
        return None


def _preview_tier2(path: pathlib.Path) -> pathlib.Path | None:
    """Generate preview from Tier 2 files (animated GIF, video)."""
    ext = path.suffix.lower()

    # Animated GIF — PIL can open the first frame
    if ext == ".gif":
        try:
            from PIL import Image
            with Image.open(path) as img:
                img = img.convert("RGB")
                max_dim = 1280
                if max(img.size) > max_dim:
                    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
                return _save_temp_png(img)
        except Exception:
            pass

    # Video — ffmpeg frame extraction
    if ext in {".mp4", ".webm", ".mov", ".avi", ".mkv"}:
        return _preview_video(path)

    return None


def _preview_video(path: pathlib.Path) -> pathlib.Path | None:
    """Extract a representative frame from video via ffmpeg."""
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        tmp_path = pathlib.Path(tmp.name)

        result = subprocess.run(
            ["ffmpeg", "-i", str(path), "-vframes", "1", "-y", str(tmp_path)],
            capture_output=True, timeout=15,
        )
        if result.returncode == 0 and tmp_path.stat().st_size > 0:
            return tmp_path

        tmp_path.unlink(missing_ok=True)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _save_temp_png(img) -> pathlib.Path:
    """Save a PIL Image to a temp PNG file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    tmp_path = pathlib.Path(tmp.name)
    img.save(str(tmp_path), format="PNG")
    return tmp_path


def _pil_format(ext: str) -> str:
    """Map file extension to PIL save format."""
    return {
        ".jpg": "JPEG", ".jpeg": "JPEG",
        ".png": "PNG",
        ".bmp": "BMP",
        ".webp": "WEBP",
        ".tiff": "TIFF", ".tif": "TIFF",
        ".gif": "GIF",
    }.get(ext, "PNG")
