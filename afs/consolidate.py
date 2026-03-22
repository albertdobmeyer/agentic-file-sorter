"""Step 2a — Folder consolidation via reasoning model.

Evaluates all existing folder names holistically and produces a merge map
before file assignment. One cheap reasoning model call (~600 tokens).
"""

import pathlib
from dataclasses import dataclass, field
from typing import Callable

import requests

from afs.analyze import parse_json, _classify_request_error, ERR_PARSE_FAILURE
from afs.sorting import normalize_topic, move_file
from afs.naming import deduplicate_path


RESORT_SENTINEL = "RESORT"

# File-extension folder names that are junk (type-based, not topic-based)
EXTENSION_FOLDERS = {
    "jpg", "jpeg", "jfif", "png", "gif", "bmp", "webp", "tiff", "tif", "svg",
    "webm", "mp4", "mov", "avi", "mkv",
    "mp3", "wav", "flac", "ogg", "aac",
    "psd", "ai", "indd",
    "pdf", "doc", "docx", "xls", "xlsx", "csv", "ppt", "pptx",
    "exe", "msi", "zip", "rar", "7z", "tar", "gz",
}


@dataclass
class ConsolidationResult:
    merge_map: dict[str, str] = field(default_factory=dict)
    resort_folders: list[str] = field(default_factory=list)
    consolidated_folders: list[str] = field(default_factory=list)
    files_moved: int = 0
    folders_eliminated: int = 0
    resort_files: list[pathlib.Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def build_consolidation_prompt(
    folder_names: list[str],
    folder_file_counts: dict[str, int],
    config: dict | None = None,
) -> str:
    """Construct the reasoning model prompt for folder consolidation."""
    cfg = config or {}
    sorting = cfg.get("sorting", {})
    max_topics = sorting.get("max_topics", 25)
    max_topic_words = sorting.get("max_topic_words", 2)
    custom_folders = sorting.get("custom_folders", {})
    folder_aliases = sorting.get("folder_aliases", {})

    folder_lines = []
    for name in folder_names:
        count = folder_file_counts.get(name, 0)
        folder_lines.append(f"  {name}: {count}")
    folders_block = "\n".join(folder_lines)

    alias_block = ""
    if folder_aliases:
        pairs = [f"{k} → {v}" for k, v in folder_aliases.items()]
        alias_block = f"\nFOLDER ALIASES (always use the canonical name):\n  {', '.join(pairs)}\n"

    custom_block = ""
    if custom_folders:
        protected = ", ".join(custom_folders.keys())
        custom_block = f"\nPROTECTED FOLDERS (do NOT merge these away):\n  {protected}\n"

    return f"""/no_think
You are a file organization expert. You MUST consolidate these {len(folder_names)} folders down to approximately {max_topics}.

CURRENT FOLDERS (name: file count):
{folders_block}
{alias_block}{custom_block}
RULES:
1. You have {len(folder_names)} folders but MUST consolidate to {max_topics} or fewer. This requires merging MOST folders
2. The folder with the MOST files is the canonical name. Merge smaller into larger
3. ANY folder whose name is a sub-topic of another folder MUST be merged into the broader one:
   - science-education, science-fiction, data-centers → science
   - technology-room, rural-technology, military-technology → technology
   - stock-market, finance-reports, financial-crisis, investment-planning, credit-card → finance
   - construction-architecture → architecture
   - fantasy-worlds, fantasy-humor, horror-movie, monsters → fantasy
   - celebrities-movies, tv-shows, screencaps → entertainment
   - city-people, people → people
   - food-culture, cooking-culture, banana-eating, fast-food → food
   - space-exploration, spacecraft, satellite, moon, sky, astronomy, ufo → science
   - political-campaign, political-cartoon, political-rally, government-policy, meetups-politics, conspiracy-theories → politics
   - gaming-bars, gamer-room → gaming
   - social-media, newspaper, logos-brands, media → media
   - medical-devices, hospital → medicine
4. Folder names: plural, kebab-case, max {max_topic_words} words
5. "filtered" is RESERVED — never use it
6. Folders named after file extensions (jpg, png, webm, mp4, gif, jfif, psd, etc.) → "{RESORT_SENTINEL}"
7. Single-letter or meaningless folders (v, x, me, his, lit, misc) → "{RESORT_SENTINEL}"
8. OMIT folders that are already fine and need no change
9. When in doubt, merge into the BROADEST topic

Respond with ONLY a JSON object. Keys are folders to change, values are the target:
{{"pol": "politics", "political-rally": "politics", "sci": "science", "digital-art": "art", "jpg": "{RESORT_SENTINEL}"}}"""


def consolidate_folders(
    output_dir: pathlib.Path,
    all_known_folders: list[str],
    config: dict | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> ConsolidationResult:
    """Evaluate all folder names and produce a merge map via reasoning model.

    Returns ConsolidationResult with merge_map and metadata.
    """
    cfg = config or {}
    models = cfg.get("models", {})
    ollama_url = models.get("ollama_url", "http://localhost:11434")
    text_model = models.get("text_model", "qwen3:8b")
    text_timeout = models.get("text_timeout", 120)
    text_ctx = models.get("text_ctx", 8192)
    keep_alive = models.get("keep_alive", "30m")

    result = ConsolidationResult()

    if not all_known_folders:
        return result

    # Count files per folder on disk
    folder_file_counts = {}
    for name in all_known_folders:
        folder_path = output_dir / name
        if folder_path.is_dir():
            folder_file_counts[name] = sum(1 for f in folder_path.iterdir() if f.is_file())
        else:
            folder_file_counts[name] = 0

    prompt = build_consolidation_prompt(all_known_folders, folder_file_counts, config)

    # Call reasoning model
    raw = _call_reasoning_model(
        prompt, ollama_url, text_model, text_timeout, text_ctx, keep_alive,
    )

    if raw is None:
        # Retry once
        raw = _call_reasoning_model(
            prompt, ollama_url, text_model, text_timeout, text_ctx, keep_alive,
        )

    if raw is None:
        result.consolidated_folders = list(all_known_folders)
        result.errors.append("reasoning model returned no response")
        return result

    data = parse_json(raw)
    if not data or not isinstance(data, dict):
        result.consolidated_folders = list(all_known_folders)
        result.errors.append(f"unparseable response: {raw[:200]}")
        return result

    # Validate and normalize the merge map
    merge_map = {}
    for source, target in data.items():
        source = str(source).strip()
        target = str(target).strip()
        if not source or not target:
            continue
        if source == target:
            continue
        # Reject "filtered" as target
        if normalize_topic(target) == "filtered":
            continue
        if target.upper() == RESORT_SENTINEL:
            merge_map[source] = RESORT_SENTINEL
        else:
            merge_map[source] = normalize_topic(target)

    # Flatten transitive chains (A→B, B→C becomes A→C)
    merge_map = _flatten_merge_map(merge_map)

    result.merge_map = merge_map
    result.resort_folders = [k for k, v in merge_map.items() if v == RESORT_SENTINEL]

    # Compute consolidated folder list
    eliminated = set(merge_map.keys())
    remaining = [f for f in all_known_folders if f not in eliminated]
    # Add merge targets that aren't RESORT
    new_targets = {v for v in merge_map.values() if v != RESORT_SENTINEL}
    for t in sorted(new_targets):
        if t not in remaining:
            remaining.append(t)
    result.consolidated_folders = sorted(remaining)
    result.folders_eliminated = len(eliminated) - len(result.resort_folders)

    return result


def execute_merges(
    merge_map: dict[str, str],
    output_dir: pathlib.Path,
    dry_run: bool = False,
    on_event: Callable[[dict], None] | None = None,
) -> ConsolidationResult:
    """Physically move files from source folders to target folders.

    RESORT entries are collected but not moved — their files go into
    the resort queue for Step 1 processing.
    """
    result = ConsolidationResult(merge_map=merge_map)
    cleanup_empty = True

    for source_name, target_name in merge_map.items():
        source_dir = output_dir / source_name
        if not source_dir.is_dir():
            continue

        # RESORT: collect files but don't move
        if target_name == RESORT_SENTINEL:
            for f in source_dir.iterdir():
                if f.is_file():
                    result.resort_files.append(f)
            continue

        # Regular merge: move files from source to target
        target_dir = output_dir / target_name
        files_in_source = [f for f in source_dir.iterdir() if f.is_file()]

        for f in files_in_source:
            dest = target_dir / f.name
            dest = deduplicate_path(dest)
            moved = move_file(f, dest, dry_run=dry_run)
            if moved:
                result.files_moved += 1

        # Clean up empty source folder
        if cleanup_empty and not dry_run:
            _rmdir_if_empty(source_dir)

    return result


def _call_reasoning_model(
    prompt: str,
    ollama_url: str,
    text_model: str,
    text_timeout: int,
    text_ctx: int,
    keep_alive: str,
) -> str | None:
    """Single reasoning model call. Returns raw response or None."""
    try:
        resp = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": text_model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_ctx": text_ctx, "temperature": 0.1},
                "keep_alive": keep_alive,
            },
            timeout=(30, text_timeout),
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception:
        return None


def _flatten_merge_map(merge_map: dict[str, str]) -> dict[str, str]:
    """Resolve transitive chains: if A→B and B→C, result is A→C."""
    flat = {}
    for source, target in merge_map.items():
        seen = {source}
        final = target
        while final in merge_map and final not in seen:
            seen.add(final)
            final = merge_map[final]
        flat[source] = final
    return flat


def _rmdir_if_empty(path: pathlib.Path):
    """Remove a directory if it contains no files (recursive check)."""
    try:
        # Check for any files (not just direct children)
        has_files = any(path.rglob("*"))
        if not has_files:
            # Remove empty subdirectories first
            for d in sorted(path.rglob("*"), reverse=True):
                if d.is_dir():
                    d.rmdir()
            path.rmdir()
    except OSError:
        pass
