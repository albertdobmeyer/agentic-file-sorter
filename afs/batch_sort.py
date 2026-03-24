"""Step 2 — Plan-then-assign folder sorting.

New workflow (v2):
  plan_folders()                → ONE reasoning call, returns folder plan with keyword triggers
  assign_files_procedurally()   → ZERO model calls, Python keyword matching
  verify_folder_assignments()   → ONE reasoning call (optional), merges redundant folders

Legacy workflow (v1, kept for backward compat):
  step2_batch_sort()            → chunked reasoning calls (87 calls for 3000 files)
"""

import pathlib
import re
from collections import Counter
from typing import Callable

import requests

from afs.types_ import FileResult
from afs.analyze import parse_json, _classify_request_error, ERR_PARSE_FAILURE
from afs.sorting import normalize_topic


# ──────────────── v2: Plan-Then-Assign ────────────────


def plan_folders(
    named_results: list[FileResult],
    prior_folders: list[str],
    config: dict | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict[str, list[str]]:
    """Plan topic folders from keyword frequency analysis. ONE reasoning model call.

    Returns {folder_name: [trigger_keywords, ...]}
    """
    cfg = config or {}
    models = cfg.get("models", {})
    sorting = cfg.get("sorting", {})
    ollama_url = models.get("ollama_url", "http://localhost:11434")
    text_model = models.get("text_model", "qwen3:8b")
    text_timeout = models.get("text_timeout", 120)
    text_ctx = models.get("text_ctx", 8192)
    keep_alive = models.get("keep_alive", "30m")
    max_topics = sorting.get("max_topics", 25)
    max_topic_words = sorting.get("max_topic_words", 2)
    custom_folders = sorting.get("custom_folders", {})
    folder_aliases = sorting.get("folder_aliases", {})

    # Extract keyword frequencies
    kw_freq: Counter = Counter()
    for result in named_results:
        for kw in result.keywords:
            kw_freq[normalize_topic(kw.lower().strip())] += 1

    # Filter to keywords appearing 2+ times (reduces noise)
    common = [(kw, cnt) for kw, cnt in kw_freq.most_common() if cnt >= 2 and kw]
    if not common:
        common = [(kw, cnt) for kw, cnt in kw_freq.most_common(100)]

    # Build keyword frequency block
    kw_lines = [f"  {kw}: {cnt}" for kw, cnt in common[:500]]
    kw_block = "\n".join(kw_lines)

    # Prior folders context
    prior_block = ""
    if prior_folders:
        prior_block = f"\nEXISTING FOLDERS (reuse these if appropriate):\n  {', '.join(prior_folders)}\n"

    # Custom folders
    custom_block = ""
    if custom_folders:
        lines = [f'  "{name}" (triggers: {", ".join(triggers)})' for name, triggers in custom_folders.items()]
        custom_block = "\nCUSTOM FOLDERS (always include these):\n" + "\n".join(lines) + "\n"

    # Alias block
    alias_block = ""
    if folder_aliases:
        pairs = [f"{k} → {v}" for k, v in folder_aliases.items()]
        alias_block = f"\nFOLDER ALIASES:\n  {', '.join(pairs)}\n"

    prompt = f"""/no_think
You are a file organization expert. Create topic folders for a collection of {len(named_results)} files.

These are the most common content keywords (keyword: frequency):
{kw_block}
{prior_block}{custom_block}{alias_block}
RULES:
1. Create {max_topics} or fewer topic folders that cover ALL these keywords
2. Folder names: plural, kebab-case, max {max_topic_words} words
3. Each folder MUST have keyword triggers — which keywords map to it
4. Prefer BROAD topics: "science" not "quantum-physics", "politics" not "campaigns"
5. "filtered" is RESERVED — never use it
6. Every common keyword should map to exactly one folder
7. Include a "misc" folder for keywords that don't fit elsewhere

Respond with ONLY a JSON object:
{{"folders": {{"memes": ["meme", "humor", "satire", "cartoon", "comic"], "science": ["space", "planet", "moon"], ...}}}}"""

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
        return _fallback_folder_plan(common, max_topics)

    data = parse_json(raw)
    if not data or "folders" not in data:
        if on_event:
            on_event({"event": "step2b-plan", "status": "error", "error": "unparseable response"})
        return _fallback_folder_plan(common, max_topics)

    folder_plan = {}
    for folder_name, triggers in data["folders"].items():
        normalized = normalize_topic(str(folder_name).strip())
        if normalized == "filtered":
            continue
        if isinstance(triggers, list):
            folder_plan[normalized] = [str(t).lower().strip() for t in triggers]
        else:
            folder_plan[normalized] = [str(triggers).lower().strip()]

    # Inject custom folders
    for name, triggers in custom_folders.items():
        folder_plan[normalize_topic(name)] = [t.lower() for t in triggers]

    # Ensure "misc" exists
    if "misc" not in folder_plan:
        folder_plan["misc"] = []

    if on_event:
        on_event({
            "event": "step2b-plan",
            "status": "done",
            "folders": len(folder_plan),
            "folder_names": sorted(folder_plan.keys()),
        })

    return folder_plan


def assign_files_procedurally(
    named_results: list[FileResult],
    folder_plan: dict[str, list[str]],
) -> dict[str, str]:
    """Assign files to folders using Python keyword matching. ZERO model calls.

    Returns {filename: folder_name}
    """
    # Build reverse index: keyword → folder
    keyword_to_folder: dict[str, str] = {}
    for folder, triggers in folder_plan.items():
        for trigger in triggers:
            keyword_to_folder[trigger.lower()] = folder

    assignments: dict[str, str] = {}

    for result in named_results:
        source_name = pathlib.Path(result.source).name

        # Score each folder by keyword matches
        scores: dict[str, int] = {}
        for kw in result.keywords:
            kw_lower = normalize_topic(kw.lower().strip())
            if kw_lower in keyword_to_folder:
                folder = keyword_to_folder[kw_lower]
                scores[folder] = scores.get(folder, 0) + 1
            else:
                # Partial match: check if keyword contains a trigger or vice versa
                for trigger, folder in keyword_to_folder.items():
                    if trigger in kw_lower or kw_lower in trigger:
                        scores[folder] = scores.get(folder, 0) + 1
                        break

        if scores:
            assignments[source_name] = max(scores, key=scores.get)
        else:
            # Try topic from Step 1
            if result.topic and result.topic != "unsorted":
                normalized = normalize_topic(result.topic)
                if normalized in folder_plan:
                    assignments[source_name] = normalized
                else:
                    assignments[source_name] = "misc"
            else:
                assignments[source_name] = "misc"

    return assignments


def verify_folder_assignments(
    assignments: dict[str, str],
    config: dict | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict[str, str]:
    """Verify folder structure and merge redundant folders. ONE reasoning model call.

    Returns merge map {source_folder: target_folder} or empty dict.
    """
    cfg = config or {}
    models = cfg.get("models", {})
    ollama_url = models.get("ollama_url", "http://localhost:11434")
    text_model = models.get("text_model", "qwen3:8b")
    text_timeout = models.get("text_timeout", 120)
    text_ctx = models.get("text_ctx", 8192)
    keep_alive = models.get("keep_alive", "30m")

    # Build folder summary
    folder_counts: Counter = Counter(assignments.values())
    summary_lines = [f"  {folder}: {count} files" for folder, count in folder_counts.most_common()]
    summary_block = "\n".join(summary_lines)

    prompt = f"""/no_think
Review this folder structure for {len(assignments)} files across {len(folder_counts)} folders:

{summary_block}

RULES:
- Merge folders that overlap or mean the same thing (e.g., "comics" + "cartoons" → "cartoons")
- Any folder with fewer than 3 files should merge into the closest larger folder
- "filtered" and "misc" are special — do NOT merge them
- Return ONLY a JSON object with folders to merge: {{"source": "target", ...}}
- Return empty {{}} if no changes needed"""

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
    if not data or not isinstance(data, dict):
        return {}

    # Validate merge map
    merge_map = {}
    for source, target in data.items():
        source = normalize_topic(str(source).strip())
        target = normalize_topic(str(target).strip())
        if source and target and source != target and target != "filtered":
            merge_map[source] = target

    if on_event:
        on_event({
            "event": "step2c-verify",
            "status": "done",
            "merges": len(merge_map),
        })

    return merge_map


def _fallback_folder_plan(common_keywords: list[tuple[str, int]], max_topics: int) -> dict[str, list[str]]:
    """Emergency fallback: group keywords by first letter/topic without model."""
    plan: dict[str, list[str]] = {"misc": []}
    for kw, _ in common_keywords[:max_topics * 5]:
        normalized = normalize_topic(kw)
        if normalized not in plan:
            plan[normalized] = []
        plan[normalized].append(kw)
    return plan


# ──────────────── v1: Legacy Chunked Sort (kept for backward compat) ────────────────


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
2. NEVER create a folder with only 1 file. Every folder MUST have at least 2 files. If a file is the only one in its topic, put it in the closest related folder
3. Folder names: plural, kebab-case, max {max_topic_words} words (e.g. "animals", "reaction-memes")
4. MAXIMUM {max_topics} topic folders total. Fewer is better — merge aggressively
5. "filtered" is RESERVED — never use it
6. If existing folders are listed, ALWAYS reuse them. DO NOT create synonyms
7. DO NOT create folders that mean the same thing. Merge related topics
8. Prefer BROAD topics. "politics" not "campaigns". "animals" not "cute-dogs"
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
