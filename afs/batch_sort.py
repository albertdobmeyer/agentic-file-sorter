"""Step 2 — Batch sort via reasoning model.

Takes all named files from Step 1 and assigns each to a topic folder
in a single batch-aware pass (or chunked passes for large batches).
"""

import pathlib
import re
from typing import Callable

import requests

from afs.types_ import FileResult
from afs.analyze import parse_json, _classify_request_error, ERR_PARSE_FAILURE
from afs.sorting import normalize_topic


def step2_batch_sort(
    named_results: list[FileResult],
    prior_folders: list[str],
    config: dict | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict[str, str] | None:
    """Assign all named files to topic folders via the reasoning model.

    Returns {real_filename: topic_folder} mapping, or None on total failure.
    """
    cfg = config or {}
    chunk_size = cfg.get("processing", {}).get("chunk_size", 30)

    if len(named_results) <= chunk_size:
        result = _single_call(named_results, prior_folders, config)
        if on_event:
            on_event({
                "event": "step2-chunk",
                "chunk": 1,
                "of": 1,
                "assigned": len(result) if result else 0,
                "folders_so_far": len(set(result.values())) if result else 0,
            })
        return result

    # Chunk large batches
    all_assignments: dict[str, str] = {}
    accumulated_folders = list(prior_folders)
    total_chunks = (len(named_results) + chunk_size - 1) // chunk_size

    for chunk_idx, i in enumerate(range(0, len(named_results), chunk_size), 1):
        chunk = named_results[i:i + chunk_size]
        chunk_assignments = _single_call(chunk, accumulated_folders, config)

        # Retry once on total failure
        if not chunk_assignments:
            chunk_assignments = _single_call(chunk, accumulated_folders, config)

        if chunk_assignments:
            # Per-chunk validation: check for dropped files, retry immediately
            assigned_names = set(chunk_assignments.keys())
            missing = [r for r in chunk
                       if pathlib.Path(r.source).name not in assigned_names]
            if missing:
                retry = _single_call(missing, accumulated_folders, config)
                if retry:
                    chunk_assignments.update(retry)

            all_assignments.update(chunk_assignments)
            # Feed new folders to next chunk for consistency
            new_folders = set(chunk_assignments.values()) - set(accumulated_folders)
            accumulated_folders.extend(sorted(new_folders))

        if on_event:
            on_event({
                "event": "step2-chunk",
                "chunk": chunk_idx,
                "of": total_chunks,
                "assigned": len(chunk_assignments) if chunk_assignments else 0,
                "folders_so_far": len(set(accumulated_folders)),
            })

    # Final sweep: any still-unassigned files get one last shot
    if all_assignments:
        assigned_names = set(all_assignments.keys())
        unassigned = [r for r in named_results
                      if pathlib.Path(r.source).name not in assigned_names]
        if unassigned:
            retry_assignments = _single_call(unassigned, accumulated_folders, config)
            if retry_assignments:
                all_assignments.update(retry_assignments)

    return all_assignments if all_assignments else None


def build_sort_prompt(
    named_results: list[FileResult],
    prior_folders: list[str],
    key_map: dict[str, str] | None = None,
    config: dict | None = None,
) -> str:
    """Construct the reasoning model prompt for batch folder assignment."""
    cfg = config or {}
    sorting = cfg.get("sorting", {})
    max_topics = sorting.get("max_topics", 25)
    max_topic_words = sorting.get("max_topic_words", 2)
    custom_folders = sorting.get("custom_folders", {})
    folder_aliases = sorting.get("folder_aliases", {})

    # Build reverse lookup: real_name -> json_key
    name_to_key: dict[str, str] = {}
    if key_map:
        name_to_key = {v: k for k, v in key_map.items()}

    file_lines = []
    for result in named_results:
        source = pathlib.Path(result.source)
        display_name = name_to_key.get(source.name, source.name)
        # Use phrase if available (more context for folder assignment), fall back to keywords
        if hasattr(result, 'phrase') and result.phrase:
            desc = result.phrase
        else:
            normalized_kw = [normalize_topic(kw) for kw in result.keywords] if result.keywords else []
            desc = ", ".join(normalized_kw) if normalized_kw else "no description"
        file_lines.append(f'  "{display_name}": {desc}')

    files_block = "\n".join(file_lines)

    existing_block = ""
    if prior_folders:
        folder_list = ", ".join(prior_folders)
        existing_block = (
            f"\nEXISTING FOLDERS (reuse these for matching content — do NOT create synonyms):\n"
            f"  {folder_list}\n"
        )

    custom_block = ""
    if custom_folders:
        lines = []
        for folder_name, triggers in custom_folders.items():
            lines.append(f'  "{folder_name}" (triggers: {", ".join(triggers)})')
        custom_block = "\nCUSTOM FOLDERS (use when keywords match any trigger):\n" + "\n".join(lines) + "\n"

    alias_block = ""
    if folder_aliases:
        pairs = [f"{k} → {v}" for k, v in folder_aliases.items()]
        alias_block = f"\nFOLDER ALIASES (use the canonical name, not the alias):\n  {', '.join(pairs)}\n"

    return f"""/no_think
You are a file organization expert. Assign each file to a topic folder.

FILES TO SORT:
{files_block}
{existing_block}{custom_block}{alias_block}
RULES:
1. Each file MUST appear in your response — do NOT skip any file
2. Folder names: plural, kebab-case, max {max_topic_words} words (e.g. "animals", "reaction-memes")
3. MAXIMUM {max_topics} topic folders total. Fewer is better — merge aggressively
4. "filtered" is RESERVED — never use it
5. If existing folders are listed, ALWAYS reuse them. DO NOT create synonyms (e.g. if "politics" exists, do NOT create "government" or "campaigns")
6. DO NOT create folders that mean the same thing. Merge: politics/government/campaigns → politics. animals/cats/pets → animals
7. Prefer BROAD topics. "politics" not "election-campaigns". "animals" not "cute-dogs". "science" not "quantum-physics"
8. Every folder should have at LEAST 2 files. Do NOT create a folder for a single file — merge it into the closest existing folder instead
9. If custom folders are listed, use them when keywords match any trigger word

Respond with ONLY a JSON object mapping each filename to its folder:
{{"filename1.ext": "topic-folder", "filename2.ext": "topic-folder"}}"""


def normalize_filename_for_json(name: str) -> str:
    """Normalize a filename into a safe, model-friendly JSON key."""
    stem = pathlib.Path(name).stem
    ext = pathlib.Path(name).suffix.lower()

    slug = re.sub(r"[\s_]+", "-", stem.strip())
    slug = re.sub(r"[^a-zA-Z0-9\-()\.]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-").lower()

    return f"{slug}{ext}" if slug else f"file{ext}"


def build_json_key_map(named_results: list[FileResult]) -> dict[str, str]:
    """Build a mapping from normalized JSON keys back to real filenames."""
    key_map: dict[str, str] = {}
    seen: dict[str, int] = {}

    for result in named_results:
        real_name = pathlib.Path(result.source).name
        key = normalize_filename_for_json(real_name)

        if key in seen:
            seen[key] += 1
            stem = pathlib.Path(key).stem
            ext = pathlib.Path(key).suffix
            key = f"{stem}-{seen[key]}{ext}"
        else:
            seen[key] = 1

        key_map[key] = real_name

    return key_map


# --- Internal ---


def _single_call(
    named_results: list[FileResult],
    prior_folders: list[str],
    config: dict | None = None,
) -> dict[str, str] | None:
    """Single reasoning model call for a batch of files."""
    cfg = config or {}
    models = cfg.get("models", {})
    ollama_url = models.get("ollama_url", "http://localhost:11434")
    text_model = models.get("text_model", "qwen3:8b")
    text_timeout = models.get("text_timeout", 120)
    text_ctx = models.get("text_ctx", 8192)
    keep_alive = models.get("keep_alive", "30m")

    key_map = build_json_key_map(named_results)
    prompt = build_sort_prompt(named_results, prior_folders, key_map, config)

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
        err_type, _ = _classify_request_error(e)
        # Store error info on the function for the caller to inspect
        _single_call.last_error_type = err_type
        _single_call.last_error = str(e)
        return None

    data = parse_json(raw)
    if not data or not isinstance(data, dict):
        _single_call.last_error_type = ERR_PARSE_FAILURE
        _single_call.last_error = f"unparseable: {raw[:200]}"
        return None

    # Map normalized keys back to real filenames, normalize folder names
    assignments = {}
    for json_key, folder in data.items():
        json_key = str(json_key).strip()
        folder = str(folder).strip()
        if not json_key or not folder:
            continue

        real_name = key_map.get(json_key)
        if not real_name:
            for k, v in key_map.items():
                if k.lower() == json_key.lower():
                    real_name = v
                    break
        if real_name:
            assignments[real_name] = normalize_topic(folder)

    _single_call.last_error_type = None
    _single_call.last_error = None
    return assignments if assignments else None


# Initialize error state
_single_call.last_error_type = None
_single_call.last_error = None
