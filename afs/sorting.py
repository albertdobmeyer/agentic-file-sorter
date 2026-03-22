"""File sorting — topic normalization, folder routing, file moving.

ALL files go into topic folders (a PDF about finance → finance/, not documents/).
"""

import pathlib
import re
import shutil

from afs.naming import generate_name, deduplicate_path


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
) -> pathlib.Path:
    """Determine where a file should be moved.

    ALL files go into topic folders: output_dir/{topic}/{semantic-name}.ext
    """
    ext = source.suffix.lower()
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
    """List current subfolders in the output directory for folder matching."""
    if not output_dir.exists():
        return []
    return [d.name for d in output_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
