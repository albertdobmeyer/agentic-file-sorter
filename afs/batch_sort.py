"""Step 2 — Folder planning, file assignment, and verification.

Four clean passes:
  1. plan_folders()      → ONE model call, returns ~25 folder names
  2. assign_files()      → ZERO model calls, Python string matching
  3. resolve_ambiguous() → 1-3 model calls for files that fell into misc
  4. verify_folders()    → ONE model call to merge redundant folders
"""

import pathlib
from collections import Counter, defaultdict
from typing import Callable

import requests

from afs.types_ import FileResult
from afs.analyze import parse_json
from afs.sorting import normalize_topic


# ──────────────── Pass 1: Plan Folders ────────────────


def plan_folders(
    named_results: list[FileResult],
    prior_folders: list[str],
    config: dict | None = None,
    on_event: Callable | None = None,
) -> list[str]:
    """ONE reasoning model call. Returns list of folder names.

    Sends unique keywords (not filenames) — compact, fits in one call.
    """
    cfg = config or {}
    models = cfg.get("models", {})
    sorting = cfg.get("sorting", {})
    ollama_url = models.get("ollama_url", "http://localhost:11434")
    text_model = models.get("text_model", "qwen3:8b")
    text_timeout = max(models.get("text_timeout", 120), 300)
    text_ctx = models.get("text_ctx", 8192)
    keep_alive = models.get("keep_alive", "30m")
    max_topics = sorting.get("max_topics", 25)
    max_topic_words = sorting.get("max_topic_words", 2)

    # Extract unique keywords (appearing 3+ times)
    kw_freq: Counter = Counter()
    for r in named_results:
        for kw in r.keywords:
            normalized = normalize_topic(kw.lower().strip())
            if normalized:
                kw_freq[normalized] += 1

    common = [kw for kw, cnt in kw_freq.most_common(300) if cnt >= 3]
    if not common:
        common = [kw for kw, _ in kw_freq.most_common(50)]

    kw_list = ", ".join(common)

    # Prior folders hint
    prior_hint = ""
    if prior_folders:
        prior_hint = f"\nExisting folders (reuse if appropriate): {', '.join(prior_folders[:20])}\n"

    prompt = f"""/no_think
These keywords describe {len(named_results)} image files:
{kw_list}
{prior_hint}
Create 20-{max_topics} broad topic folders to organize files with these keywords.
Folder names: plural, kebab-case, max {max_topic_words} words.

GOOD folders: broad categories like animals, memes, politics, science, nature, technology, religion, art
BAD folders: narrow keywords like frog, chart, moon, costume, statue, night

Respond with ONLY a JSON array:
["animals", "memes", "politics", "science", "nature", ...]"""

    if on_event:
        on_event({"event": "step2b-plan", "status": "calling", "keywords": len(common)})

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
        raw = resp.json().get("response", "")
    except Exception as e:
        if on_event:
            on_event({"event": "step2b-plan", "status": "error", "error": str(e)})
        return _fallback_folders(kw_freq, max_topics)

    data = parse_json(raw)

    # Accept array or dict with folder keys
    folders = []
    if isinstance(data, list):
        folders = [normalize_topic(str(f).strip()) for f in data if f]
    elif isinstance(data, dict):
        folders = [normalize_topic(str(k).strip()) for k in data.keys() if k]

    folders = [f for f in folders if f and f != "filtered"]

    if not folders:
        if on_event:
            on_event({"event": "step2b-plan", "status": "error", "error": f"unparseable: {raw[:80]}"})
        return _fallback_folders(kw_freq, max_topics)

    if "misc" not in folders:
        folders.append("misc")

    if on_event:
        on_event({"event": "step2b-plan", "status": "done", "folders": len(folders), "folder_names": folders})

    return folders


def _fallback_folders(kw_freq: Counter, max_topics: int) -> list[str]:
    """Emergency fallback: use top keywords as folder names."""
    folders = []
    for kw, _ in kw_freq.most_common(max_topics):
        normalized = normalize_topic(kw)
        if normalized and normalized != "filtered" and normalized not in folders:
            folders.append(normalized)
    if "misc" not in folders:
        folders.append("misc")
    return folders


# ──────────────── Pass 2: Assign Files ────────────────


def assign_files(
    named_results: list[FileResult],
    folders: list[str],
) -> dict[str, str]:
    """Python string matching. ZERO model calls. Returns {filename: folder}."""
    from afs.sorting import TOPIC_CANONICAL

    # Build word→folder index for fast lookup
    folder_words: dict[str, str] = {}
    for folder in folders:
        for part in folder.split("-"):
            if part and part != "misc":
                folder_words[part] = folder

    # Build canonical→folder index (maps synonyms to matching folders)
    canonical_to_folder: dict[str, str] = {}
    for raw_word, canonical in TOPIC_CANONICAL.items():
        canonical_norm = normalize_topic(canonical)
        if canonical_norm in folders:
            canonical_to_folder[raw_word] = canonical_norm
            canonical_to_folder[canonical_norm] = canonical_norm

    assignments: dict[str, str] = {}

    for result in named_results:
        source_name = pathlib.Path(result.source).name
        best_folder = _match_file_to_folder(result, folders, folder_words, canonical_to_folder)
        assignments[source_name] = best_folder

    return assignments


def _match_file_to_folder(
    result: FileResult,
    folders: list[str],
    folder_words: dict[str, str],
    canonical_to_folder: dict[str, str],
) -> str:
    """Score each folder for a file. Returns best match or 'misc'."""
    scores: dict[str, int] = defaultdict(int)

    # Collect all words from filename + keywords + topic
    file_words = set()
    stem = pathlib.Path(result.source).stem.lower()
    file_words.update(w for w in stem.replace("-", " ").replace("_", " ").split() if len(w) > 2)
    file_words.update(normalize_topic(kw.lower()) for kw in result.keywords if kw)
    if result.topic:
        file_words.add(normalize_topic(result.topic.lower()))

    for word in file_words:
        # Exact match: word == folder
        if word in folders:
            scores[word] += 3
            continue

        # Canonical mapping: word maps to a known folder via TOPIC_CANONICAL
        if word in canonical_to_folder:
            scores[canonical_to_folder[word]] += 3
            continue

        # Word is part of a folder name
        if word in folder_words:
            scores[folder_words[word]] += 2
            continue

        # Folder name contains the word or vice versa
        for folder in folders:
            if folder == "misc":
                continue
            if word in folder or folder in word:
                scores[folder] += 1
            elif any(part in word or word in part for part in folder.split("-")):
                scores[folder] += 1

    if scores:
        return max(scores, key=scores.get)

    return "misc"


# ──────────────── Pass 3: Resolve Ambiguous ────────────────


def resolve_ambiguous(
    misc_files: list[FileResult],
    folders: list[str],
    config: dict | None = None,
    on_event: Callable | None = None,
    chunk_size: int = 50,
) -> dict[str, str]:
    """Model call for files that fell into misc. Returns {filename: folder}."""
    if not misc_files:
        return {}

    cfg = config or {}
    models = cfg.get("models", {})
    ollama_url = models.get("ollama_url", "http://localhost:11434")
    text_model = models.get("text_model", "qwen3:8b")
    text_timeout = max(models.get("text_timeout", 120), 180)
    text_ctx = models.get("text_ctx", 8192)
    keep_alive = models.get("keep_alive", "30m")

    folder_list = ", ".join(f for f in folders if f != "misc")
    assignments: dict[str, str] = {}

    # Process in chunks
    for i in range(0, len(misc_files), chunk_size):
        chunk = misc_files[i:i + chunk_size]
        file_lines = [f"  {pathlib.Path(r.source).stem}" for r in chunk]
        file_block = "\n".join(file_lines)

        prompt = f"""/no_think
These files could not be automatically sorted. Assign each to the best folder.

Files:
{file_block}

Folders: {folder_list}

Respond with ONLY a JSON object: {{"filename": "folder", ...}}
Keep filenames exactly as shown. Every file must be assigned."""

        if on_event:
            on_event({"event": "step2b-resolve", "chunk": (i // chunk_size) + 1, "files": len(chunk)})

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
            raw = resp.json().get("response", "")
        except Exception:
            continue

        data = parse_json(raw)
        if isinstance(data, dict):
            for stem, folder in data.items():
                folder_norm = normalize_topic(str(folder).strip())
                if folder_norm and folder_norm != "filtered":
                    # Match stem back to full filename
                    for r in chunk:
                        if pathlib.Path(r.source).stem.lower() == str(stem).lower():
                            assignments[pathlib.Path(r.source).name] = folder_norm
                            break

    return assignments


# ──────────────── Pass 4: Verify ────────────────


def verify_folders(
    assignments: dict[str, str],
    config: dict | None = None,
    on_event: Callable | None = None,
) -> dict[str, str]:
    """ONE model call to merge redundant folders. Returns merge map or empty dict."""
    cfg = config or {}
    models = cfg.get("models", {})
    ollama_url = models.get("ollama_url", "http://localhost:11434")
    text_model = models.get("text_model", "qwen3:8b")
    text_timeout = models.get("text_timeout", 120)
    text_ctx = models.get("text_ctx", 8192)
    keep_alive = models.get("keep_alive", "30m")

    folder_counts: Counter = Counter(assignments.values())
    if len(folder_counts) <= 3:
        return {}

    summary = ", ".join(f"{f}: {c}" for f, c in folder_counts.most_common())

    total_files = len(assignments)
    prompt = f"""/no_think
Review these {len(folder_counts)} folders for {total_files} files:
{summary}

ONLY merge folders that are truly redundant or synonymous (e.g. "comics" and "cartoons" → "cartoons").
Do NOT merge folders just because they have few files — small folders are fine.
"misc" and "filtered" should never be merged.

Respond with ONLY a JSON merge map: {{"source": "target", ...}}
Return empty {{}} if no changes needed."""

    if on_event:
        on_event({"event": "step2c-verify", "status": "calling", "folders": len(folder_counts)})

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
        raw = resp.json().get("response", "")
    except Exception:
        return {}

    data = parse_json(raw)
    if not isinstance(data, dict):
        return {}

    merge_map = {}
    for source, target in data.items():
        s = normalize_topic(str(source).strip())
        t = normalize_topic(str(target).strip())
        if s and t and s != t and t != "filtered" and s != "misc":
            merge_map[s] = t

    if on_event:
        on_event({"event": "step2c-verify", "status": "done", "merges": len(merge_map)})

    return merge_map
