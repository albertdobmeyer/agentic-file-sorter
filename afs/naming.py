"""Semantic filename generation — synthesizes original filename with analysis keywords."""

import re
from pathlib import Path


# Words that describe the medium, not the content — always filtered
META_NOISE = {
    "img", "image", "images", "screenshot", "photo", "photograph", "photography",
    "pic", "picture", "camera", "dsc", "dcim", "pxl", "gopr",
    "copy", "final", "new", "old", "untitled", "download", "tmp",
    "person", "subject", "individual", "indistinct",
}

# Synonym groups — when multiple synonyms appear, keep only the first
_SYNONYM_GROUPS = [
    {"dark", "darkness", "void", "night", "black"},
    {"blurry", "blurred", "indistinct", "unfocused", "out-of-focus"},
    {"lamp", "light", "spotlight", "led-light", "desk-lamp"},
    {"room", "indoor", "indoors", "interior", "inside"},
]


def _dedup_synonyms(slugs: list[str]) -> list[str]:
    """Remove synonym duplicates — keep the first occurrence from each group."""
    result = []
    used_groups: set[int] = set()
    for slug in slugs:
        matched_group = None
        for i, group in enumerate(_SYNONYM_GROUPS):
            if slug in group:
                matched_group = i
                break
        if matched_group is not None:
            if matched_group in used_groups:
                continue  # skip synonym
            used_groups.add(matched_group)
        result.append(slug)
    return result


def _extract_filename_words(filename_stem: str) -> list[str]:
    """Extract meaningful words from an original filename.

    'birthday-party' → ['birthday', 'party']
    'IMG_2847' → []  (nothing useful)
    """
    raw = re.split(r"[-_ .]+", filename_stem)

    words = []
    for w in raw:
        w_lower = w.lower()
        if len(w) <= 1:
            continue
        if w_lower.isdigit():
            continue  # all numbers filtered — sequence numbers handled by photo.extract_photo_sequence
        if w_lower in META_NOISE:
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
    Filters meta-words and deduplicates synonyms.
    """
    if not keywords and not original_stem:
        return "unsorted"

    kw_slugs = []
    kw_seen = set()
    for kw in keywords[:max_parts + 2]:  # read extra to compensate for filtering
        slug = re.sub(r"[^a-z0-9]+", "-", kw.lower().strip()).strip("-")
        if not slug or slug in kw_seen:
            continue
        # Filter meta-noise words and pure numbers (sequence handled separately)
        parts = slug.split("-")
        if all(p in META_NOISE or p.isdigit() for p in parts):
            continue
        kw_slugs.append(slug)
        kw_seen.add(slug)

    # Deduplicate synonyms
    kw_slugs = _dedup_synonyms(kw_slugs)

    if original_stem:
        orig_words = _extract_filename_words(original_stem)
        for w in orig_words:
            already_covered = any(w in existing for existing in kw_seen)
            if not already_covered and len(kw_slugs) < max_parts:
                kw_slugs.append(w)
                kw_seen.add(w)

    # Trim to max
    kw_slugs = kw_slugs[:max_parts]

    if not kw_slugs:
        if original_stem:
            slug = re.sub(r"[^a-z0-9]+", "-", original_stem.lower().strip()).strip("-")
            return slug if slug else "unsorted"
        return "unsorted"

    return "-".join(kw_slugs)


# Words preserved as connective tissue in phrases
CONNECTIVE_WORDS = {
    "in", "at", "with", "under", "on", "of", "and", "the", "a", "an",
    "by", "for", "to", "from", "near", "over", "through",
}


def generate_name_from_phrase(
    phrase: str,
    original_stem: str = "",
    max_words: int = 7,
) -> str:
    """Generate a kebab-case filename from a natural-language phrase.

    Preserves connective words for natural reading.
    'shepherd sleeping under tree in alps' → 'shepherd-sleeping-under-tree-in-alps'

    Falls back to generate_name() if phrase is empty.
    """
    if not phrase:
        if original_stem:
            return generate_name([], original_stem)
        return "unsorted"

    # Normalize: lowercase, keep only letters/numbers/spaces
    phrase = phrase.lower().strip()
    phrase = re.sub(r"[^a-z0-9\s]", "", phrase)

    # Split into words
    words = phrase.split()

    # Filter meta-noise (but keep connectives)
    filtered = []
    for w in words:
        if w in CONNECTIVE_WORDS:
            filtered.append(w)
        elif w in META_NOISE or w.isdigit() or len(w) <= 1:
            continue
        else:
            filtered.append(w)

    # Don't start or end with a connective
    while filtered and filtered[0] in CONNECTIVE_WORDS:
        filtered.pop(0)
    while filtered and filtered[-1] in CONNECTIVE_WORDS:
        filtered.pop()

    # Enforce max words
    if len(filtered) > max_words:
        # Trim from end, but don't leave a trailing connective
        filtered = filtered[:max_words]
        while filtered and filtered[-1] in CONNECTIVE_WORDS:
            filtered.pop()

    # Deduplicate synonyms
    filtered = _dedup_synonyms(filtered)

    if not filtered:
        if original_stem:
            return generate_name([], original_stem)
        return "unsorted"

    return "-".join(filtered)


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
