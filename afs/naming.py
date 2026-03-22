"""Semantic filename generation — synthesizes original filename with analysis keywords."""

import re
from pathlib import Path


def _extract_filename_words(filename_stem: str) -> list[str]:
    """Extract meaningful words from an original filename.

    'birthday-party' → ['birthday', 'party']
    'IMG_2847' → []  (nothing useful)
    """
    raw = re.split(r"[-_ .]+", filename_stem)

    noise = {
        "img", "image", "screenshot", "photo", "pic", "dsc", "dcim",
        "copy", "final", "new", "old", "untitled", "download", "tmp",
    }
    words = []
    for w in raw:
        w_lower = w.lower()
        if len(w) <= 1:
            continue
        if w_lower.isdigit() and len(w) > 4:
            continue
        if w_lower in noise:
            continue
        words.append(w_lower)

    return words


def generate_name(
    keywords: list[str],
    original_stem: str = "",
    max_parts: int = 5,
) -> str:
    """Generate a kebab-case filename from keywords + original filename words.

    Max 5 parts (2-5 word semantic names).
    """
    if not keywords and not original_stem:
        return "unsorted"

    kw_slugs = []
    kw_seen = set()
    for kw in keywords[:max_parts]:
        slug = re.sub(r"[^a-z0-9]+", "-", kw.lower().strip()).strip("-")
        if slug and slug not in kw_seen:
            kw_slugs.append(slug)
            kw_seen.add(slug)

    if original_stem:
        orig_words = _extract_filename_words(original_stem)
        for w in orig_words:
            already_covered = any(w in existing for existing in kw_seen)
            if not already_covered and len(kw_slugs) < max_parts:
                kw_slugs.append(w)
                kw_seen.add(w)

    if not kw_slugs:
        if original_stem:
            slug = re.sub(r"[^a-z0-9]+", "-", original_stem.lower().strip()).strip("-")
            return slug if slug else "unsorted"
        return "unsorted"

    return "-".join(kw_slugs)


def deduplicate_path(dest: Path) -> Path:
    """If dest exists, append -2, -3, etc. until unique."""
    if not dest.exists():
        return dest

    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
