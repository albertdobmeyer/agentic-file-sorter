"""File sorting — topic normalization, folder routing, file moving.

ALL files go into topic folders (a PDF about finance → finance/, not documents/).
"""

import pathlib
import re
import shutil

from afs.naming import generate_name, generate_name_from_phrase, deduplicate_path


# Canonical topic mapping — normalizes singular→plural, merges synonyms (~80 entries)
TOPIC_CANONICAL = {
    # Singular → plural
    "animal": "animals", "cat": "animals", "dog": "animals", "bear": "animals",
    "vehicle": "vehicles", "comic": "comics", "meme": "memes",
    "emotion": "emotions", "emoji": "emotions", "emojis": "emotions",
    "emoticon": "emotions", "face": "emotions", "faces": "emotions",
    "map": "maps", "game": "games", "video game": "games", "video games": "games",
    "sport": "sports", "celebrity": "celebrities", "actor": "celebrities",
    "person": "people", "man": "people", "woman": "people", "family": "people",
    "portrait": "portraits", "flag": "flags", "weapon": "weapons",
    "monster": "monsters", "creature": "creatures", "dinosaur": "dinosaurs",
    "book": "books", "cartoon": "cartoons",
    # Synonym merges
    "medical": "medicine", "healthcare": "medicine", "health": "medicine",
    "covid-19": "medicine", "pharmaceutical": "medicine",
    "political": "politics", "political satire": "politics", "government": "politics",
    "debate": "politics", "conspiracy": "politics",
    "social": "society", "crime": "society", "prison": "society",
    "poverty": "society", "gender": "society", "feminism": "society",
    "social media": "technology", "computer": "technology", "security": "technology",
    "urban": "architecture", "cityscape": "architecture",
    "news": "media", "magazine": "media", "advertising": "media",
    "film": "media", "theater": "entertainment",
    "comedy": "humor",
    "gambling": "finance", "currency": "finance", "economy": "finance",
    "economics": "finance", "cryptocurrency": "finance", "trade": "finance",
    "war": "military", "soldier": "military",
    "disaster": "events", "emergency": "events", "explosion": "events",
    "travel": "geography",
    "underwater": "nature", "gardening": "nature", "weather": "nature",
    "fire": "nature", "cave": "nature",
    "outdoor dining": "food", "cake": "food",
    "exercise": "fitness", "fitness": "sports",
    "reading": "books",
    "signage": "design", "logo": "design",
    "graffiti": "art", "photography": "art", "abstract": "art",
    "animation": "art", "anime": "art", "collage": "art",
    "pop culture": "culture", "indigenous": "culture", "holiday": "culture",
    "chinese": "culture",
    "belief": "spirituality", "astrology": "spirituality", "tarot": "spirituality",
    "spectrum": "science", "microbiology": "science", "data": "science",
    "data visualization": "science", "brain": "science", "space": "science",
    "prehistoric": "history", "civilization": "history", "royalty": "history",
    "biography": "history",
    "alphabet": "education", "language": "education",
    "beauty": "fashion", "costume": "fashion", "cosplay": "fashion",
    "magic": "fantasy", "horror": "fantasy",
    "alien": "science-fiction", "futurism": "science-fiction",
    "ethics": "philosophy",
    "text": "misc", "objects": "misc", "home": "misc",
}


def normalize_topic(topic: str) -> str:
    """Normalize a topic string: canonical mapping, kebab-case, max 2 words."""
    topic = topic.lower().strip()
    topic = TOPIC_CANONICAL.get(topic, topic)
    topic = re.sub(r"[^a-z0-9]+", "-", topic).strip("-")
    parts = topic.split("-")
    if len(parts) > 2:
        topic = "-".join(parts[:2])
    return topic if topic else "misc"


def get_destination(
    source: pathlib.Path,
    topic: str,
    keywords: list[str],
    output_dir: pathlib.Path,
    phrase: str = "",
) -> pathlib.Path:
    """Determine where a file should be moved.

    ALL files go into topic folders: output_dir/{topic}/{semantic-name}.ext
    """
    ext = source.suffix.lower()
    if phrase:
        semantic_name = generate_name_from_phrase(phrase, original_stem=source.stem)
    else:
        semantic_name = generate_name(keywords, original_stem=source.stem)
    normalized = normalize_topic(topic) if topic and topic != "unsorted" else "misc"
    folder = output_dir / normalized
    dest = folder / f"{semantic_name}{ext}"
    return deduplicate_path(dest)


def move_file(source: pathlib.Path, dest: pathlib.Path, dry_run: bool = False) -> bool:
    """Move a file to its destination, creating directories as needed."""
    if dry_run:
        return True

    dest.parent.mkdir(parents=True, exist_ok=True)

    if source.resolve() == dest.resolve():
        return False

    shutil.move(str(source), str(dest))
    return True


def scan_existing_folders(output_dir: pathlib.Path) -> list[str]:
    """List current subfolders in the output directory (excludes hidden and filtered)."""
    if not output_dir.exists():
        return []
    return [d.name for d in output_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name != "filtered"]


def flatten_directory(
    target_dir: pathlib.Path,
    dry_run: bool = False,
    on_event=None,
) -> dict:
    """Move all files from subfolders to the root of target_dir.

    Skips hidden dirs, filtered/, and manifest files.
    Removes empty folders after flattening.
    Returns summary: {files_moved, folders_removed, collisions}.
    """
    from afs.naming import deduplicate_path

    files_moved = 0
    collisions = 0
    folders_to_clean = set()

    # Find all files in subdirectories (not root-level files)
    for p in sorted(target_dir.rglob("*")):
        if not p.is_file():
            continue
        # Skip files already at root level
        rel = p.relative_to(target_dir)
        if len(rel.parts) <= 1:
            continue
        # Skip hidden dirs, filtered/, manifest
        if any(part.startswith(".") for part in rel.parts):
            continue
        if rel.parts[0] == "filtered":
            continue

        dest = target_dir / p.name
        if dest.exists() and dest.resolve() != p.resolve():
            dest = deduplicate_path(dest)
            collisions += 1

        moved = move_file(p, dest, dry_run=dry_run)
        if moved:
            files_moved += 1
            folders_to_clean.add(p.parent)

        if on_event:
            on_event({
                "event": "flatten-progress",
                "file": p.name,
                "from": str(rel.parent),
                "to": dest.name,
                "dry_run": dry_run,
            })

    # Remove empty folders (deepest first)
    folders_removed = 0
    if not dry_run:
        for folder in sorted(folders_to_clean, key=lambda p: len(p.parts), reverse=True):
            try:
                # Walk up removing empty dirs until we hit target_dir
                current = folder
                while current != target_dir:
                    if current.exists() and not any(current.iterdir()):
                        current.rmdir()
                        folders_removed += 1
                    current = current.parent
            except OSError:
                pass

    # Clear manifest since folder assignments are now invalid
    manifest_path = target_dir / ".afs-manifest.json"
    if manifest_path.exists() and not dry_run:
        manifest_path.unlink()

    return {
        "files_moved": files_moved,
        "folders_removed": folders_removed,
        "collisions": collisions,
    }
