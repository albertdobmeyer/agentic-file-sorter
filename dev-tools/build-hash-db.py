"""Build the known-subjects hash database from a curated image library.

DEV-ONLY — not shipped in the repo. Requires Pillow (already installed).
Optionally uses CLIP for embedding validation (pip install transformers torch).

Usage:
    python dev-tools/build-hash-db.py <image-library-dir>

Image library structure:
    library/
      pepe-the-frog/
        classic.png
        sad.jpg
        smug.png
      wojak/
        original.png
        doomer.jpg
      elon-musk/
        headshot.jpg

Each subdirectory = one subject. Multiple images = multiple hash variants.
Output: data/known-subjects.json
"""

import json
import pathlib
import sys

# Add project root to path
PROJECT_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from afs.hashing import compute_phash, hamming_distance, save_known_subjects

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}


def build_database(library_dir: pathlib.Path, use_descriptions: bool = True) -> dict:
    """Build known-subjects.json from a curated image library."""
    db = {"version": 1, "subjects": {}}

    for entry in sorted(library_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue

        name = entry.name.lower().replace("_", "-").replace(" ", "-")
        print(f"  Processing: {name}/")

        hashes = []
        sample_count = 0

        for img_file in sorted(entry.iterdir()):
            if not img_file.is_file():
                continue
            if img_file.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            phash = compute_phash(img_file)
            if phash:
                # Check for duplicate hashes (skip if too similar to existing)
                is_dup = False
                for existing_hash in hashes:
                    if hamming_distance(phash, existing_hash) < 5:
                        is_dup = True
                        break
                if not is_dup:
                    hashes.append(phash)
                    sample_count += 1
                    print(f"    + {img_file.name} → {phash[:12]}...")

        if not hashes:
            print(f"    (no valid images, skipping)")
            continue

        # Determine category from directory name or metadata
        category = _guess_category(name)

        # Compute optimal hash threshold based on intra-subject variance
        if len(hashes) > 1:
            max_internal_dist = 0
            for i, h1 in enumerate(hashes):
                for h2 in hashes[i+1:]:
                    d = hamming_distance(h1, h2)
                    max_internal_dist = max(max_internal_dist, d)
            # Threshold = max internal distance + margin
            threshold = min(max_internal_dist + 10, 30)
        else:
            threshold = 15  # conservative default for single sample

        db["subjects"][name] = {
            "category": category,
            "aliases": _generate_aliases(name),
            "description": "",  # filled by describe step or manually
            "hashes": hashes,
            "hash_threshold": threshold,
            "sample_count": sample_count,
        }

        print(f"    → {sample_count} hashes, threshold={threshold}, category={category}")

    return db


def _guess_category(name: str) -> str:
    """Guess subject category from name."""
    meme_indicators = {"pepe", "wojak", "trollface", "chad", "gigachad", "soyjak", "npc", "doge", "amogus", "nyan"}
    celeb_indicators = {"musk", "trump", "obama", "biden", "kardashian", "swift", "beyonce", "drake"}
    game_indicators = {"mario", "luigi", "sonic", "pikachu", "link", "kirby", "master-chief"}
    hero_indicators = {"spider", "batman", "superman", "iron-man", "thanos", "joker", "hulk"}

    name_lower = name.lower()
    for word in meme_indicators:
        if word in name_lower:
            return "meme"
    for word in celeb_indicators:
        if word in name_lower:
            return "celebrity"
    for word in game_indicators:
        if word in name_lower:
            return "video-game"
    for word in hero_indicators:
        if word in name_lower:
            return "superhero"
    return "character"


def _generate_aliases(name: str) -> list[str]:
    """Generate common aliases for a subject name."""
    aliases = [name]
    # Remove "the" prefix
    if name.startswith("the-"):
        aliases.append(name[4:])
    # Split compound names
    parts = name.split("-")
    if len(parts) >= 2:
        aliases.append(parts[0])  # first name
    return list(set(aliases))


def main():
    if len(sys.argv) < 2:
        print("Usage: python dev-tools/build-hash-db.py <image-library-dir>")
        print()
        print("Image library structure:")
        print("  library/")
        print("    pepe-the-frog/")
        print("      classic.png")
        print("      sad.jpg")
        print("    wojak/")
        print("      original.png")
        sys.exit(1)

    library_dir = pathlib.Path(sys.argv[1])
    if not library_dir.is_dir():
        print(f"Error: {library_dir} is not a directory")
        sys.exit(1)

    print(f"\n  Building hash database from: {library_dir}\n")
    db = build_database(library_dir)

    output_path = PROJECT_ROOT / "data" / "known-subjects.json"
    save_known_subjects(db, output_path)

    total_subjects = len(db["subjects"])
    total_hashes = sum(len(s["hashes"]) for s in db["subjects"].values())
    print(f"\n  Done: {total_subjects} subjects, {total_hashes} hashes")
    print(f"  Saved to: {output_path}\n")


if __name__ == "__main__":
    main()
