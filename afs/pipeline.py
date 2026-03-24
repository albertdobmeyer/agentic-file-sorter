"""Orchestrator — three-step pipeline per the constitution.

Step 1  (per-file, vision model):  classify tier → CDR → preview → vision → semantic name
Step 2a (one call, reasoning model): holistic folder consolidation → merge map → execute merges
Step 2b (chunked, reasoning model): batch file assignment → moves
"""

import contextlib
import ctypes
import datetime
import json
import pathlib
import platform
import time
import traceback
from collections import defaultdict
from typing import Callable

import requests

from afs.config import VERSION
from afs.types_ import FileResult, BatchResult, classify_tier
from afs.preview import apply_cdr, generate_preview
from afs.analyze import (
    analyze_vision,
    ERR_FILE_READ,
)
from afs.naming import deduplicate_path
from afs.sorting import normalize_topic, get_destination, move_file, scan_existing_folders
from afs.batch_sort import plan_folders, assign_files, resolve_ambiguous, verify_folders
from afs.consolidate import consolidate_folders, execute_merges, RESORT_SENTINEL


@contextlib.contextmanager
def _prevent_sleep():
    """Prevent OS sleep/hibernate during long-running processing.

    Windows: SetThreadExecutionState tells the OS not to sleep.
    Other platforms: no-op (graceful fallback).
    """
    if platform.system() == "Windows":
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            )
        except Exception:
            pass
        try:
            yield
        finally:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                pass
    else:
        yield


def _warm_model(model: str, config: dict | None = None, on_event: Callable | None = None):
    """Pre-load a model into Ollama's GPU memory."""
    cfg = config or {}
    ollama_url = cfg.get("models", {}).get("ollama_url", "http://localhost:11434")

    if on_event:
        on_event({"event": "warm", "model": model})

    try:
        requests.post(
            f"{ollama_url}/api/generate",
            json={"model": model, "prompt": "hi", "stream": False,
                  "options": {"num_predict": 1}},
            timeout=300,
        )
    except Exception:
        pass  # best-effort, don't block the pipeline


def process_file(
    path: pathlib.Path,
    output_dir: pathlib.Path,
    dry_run: bool = False,
    sanitize_images: bool = True,
    convert_webp: bool = True,
    config: dict | None = None,
    face_samples: dict[str, str] | None = None,
) -> FileResult:
    """Step 1: Analyze and name a single file. No moves except Tier 3 and errors."""
    from afs.photo import is_likely_photo, extract_photo_sequence

    start = time.time()
    result = FileResult(source=str(path))

    # Check if this file type should be grouped by extension (not analyzed)
    cfg_sort = (config or {}).get("sorting", {})
    group_by_type = set(cfg_sort.get("group_by_type", []))
    ext_lower = path.suffix.lower()
    if ext_lower in group_by_type:
        return _move_to_filtered(path, output_dir, dry_run, start)

    tier = classify_tier(path)
    result.tier = tier

    # Tier 3: no analysis, route to filtered/ immediately
    if tier == 3:
        return _move_to_filtered(path, output_dir, dry_run, start)

    try:
        # Photo detection: skip CDR for camera photos (preserves original bytes + EXIF)
        is_photo = False
        cfg = config or {}
        skip_cdr_photos = cfg.get("processing", {}).get("skip_cdr_photos", True)
        if tier == 1 and skip_cdr_photos:
            is_photo = is_likely_photo(path, config)
        result.photo_detected = is_photo

        # Tier 1: CDR (re-render via PIL, stripping non-pixel data)
        # Skipped for detected photos — original bytes preserved
        if tier == 1 and sanitize_images and not is_photo:
            result.original = path.name  # preserve pre-CDR filename
            path = apply_cdr(path, convert_webp=convert_webp)
            result.source = str(path)  # path may have changed (webp → jpg)

        # Generate preview for vision model
        preview_path = generate_preview(path, tier)
        if not preview_path:
            return _move_to_errors(path, output_dir, dry_run, start,
                                   "preview generation failed", "preview_failed")

        # Layer 2: Perceptual hash matching (fast, no model call)
        from afs.hashing import match_known_subjects, load_known_subjects
        hash_db = load_known_subjects()
        hash_matches = match_known_subjects(preview_path or path, hash_db) if hash_db.get("subjects") else []

        # Merge sample descriptions with hash match hints
        enriched_descriptions = dict(face_samples) if face_samples else {}
        for match_name, match_dist, match_entry in hash_matches[:3]:
            if match_name not in enriched_descriptions and match_entry.get("description"):
                enriched_descriptions[match_name] = f"{match_entry['description']} [hash match, distance={match_dist}]"

        # Vision analysis — all identification context included as text
        analysis = analyze_vision(preview_path, filename_hint=path.stem,
                                  config=config, photo_hint=is_photo,
                                  sample_descriptions=enriched_descriptions if enriched_descriptions else None)
        if analysis.get("error_type") == "model_timeout":
            analysis = analyze_vision(preview_path, filename_hint=path.stem,
                                      config=config, photo_hint=is_photo,
                                      sample_descriptions=enriched_descriptions if enriched_descriptions else None)

        if "error" in analysis:
            _cleanup(preview_path)
            return _move_to_errors(path, output_dir, dry_run, start,
                                   analysis["error"],
                                   analysis.get("error_type", "unhandled"))

        topic = analysis["topic"]
        phrase = analysis.get("phrase", "")
        keywords = analysis["keywords"]
        confidence = analysis["confidence"]
        identified = analysis.get("identified")
        result.method = "vision"

        # Validate identified: must match a selected sample, hash match, or known character
        # If enriched_descriptions provided, identified must be in that set
        if identified and enriched_descriptions:
            valid_names = {n.lower() for n in enriched_descriptions.keys()}
            if identified.lower() not in valid_names:
                identified = None  # not a known subject — discard hallucination

        # If identified, prepend to phrase and keywords
        if identified:
            if identified.lower() not in phrase.lower():
                phrase = f"{identified} {phrase}".strip()
            if identified.lower() not in [kw.lower() for kw in keywords]:
                keywords.insert(0, identified.lower())
                keywords = keywords[:5]
            confidence = max(confidence, 0.8)

        # Layer 4: Web search confirmation (optional, when uncertain)
        if not identified and confidence < 0.7:
            from afs.web_search import search_for_context
            search_result = search_for_context(phrase, keywords, config=config)
            if search_result and search_result.get("suggested_name"):
                identified = search_result["suggested_name"]
                if identified.lower() not in phrase.lower():
                    phrase = f"{identified} {phrase}".strip()
                if identified.lower() not in [kw.lower() for kw in keywords]:
                    keywords.insert(0, identified.lower())
                    keywords = keywords[:5]
                confidence = max(confidence, 0.7)

        # Photo sequence: append to phrase (always at end for consistent naming)
        if is_photo:
            seq = extract_photo_sequence(path.stem)
            if seq:
                phrase = f"{phrase} {seq}".strip() if phrase else seq
                keywords = [kw for kw in keywords if kw != seq]
                keywords.append(seq)

        _cleanup(preview_path)

        # Store analysis results — no folder matching, no moves
        result.status = "named"
        result.topic = topic
        result.phrase = phrase
        result.keywords = keywords
        result.confidence = confidence
        result.identified = identified
        result.elapsed_ms = _elapsed(start)

    except Exception as e:
        return _move_to_errors(path, output_dir, dry_run, start,
                               f"unhandled: {type(e).__name__}: {e}", "unhandled")

    return result


def _move_to_filtered(
    path: pathlib.Path,
    output_dir: pathlib.Path,
    dry_run: bool,
    start: float,
) -> FileResult:
    """Tier 3 files → filtered/{extension}/"""
    result = FileResult(source=str(path), method="filtered")
    ext = path.suffix.lower().lstrip(".")
    folder = output_dir / "filtered" / ext if ext else output_dir / "filtered" / "unknown"
    dest = folder / path.name
    dest = deduplicate_path(dest)
    moved = move_file(path, dest, dry_run=dry_run)
    result.dest = str(dest)
    result.status = "dry-run" if dry_run else ("moved" if moved else "renamed")
    result.topic = "filtered"
    result.tier = 3
    result.elapsed_ms = _elapsed(start)
    return result


def _move_to_errors(
    path: pathlib.Path,
    output_dir: pathlib.Path,
    dry_run: bool,
    start: float,
    error_msg: str,
    error_type: str = "unhandled",
) -> FileResult:
    """Failed files → filtered/errors/ with error recorded in manifest."""
    result = FileResult(source=str(path), method="filtered")
    folder = output_dir / "filtered" / "errors"
    dest = folder / path.name
    dest = deduplicate_path(dest)
    moved = move_file(path, dest, dry_run=dry_run)
    result.dest = str(dest)
    result.status = "dry-run" if dry_run else "error"
    result.topic = "filtered"
    result.error = error_msg
    result.error_type = error_type
    result.elapsed_ms = _elapsed(start)
    return result


def collect_files(
    input_dir: pathlib.Path,
    output_dir: pathlib.Path | None = None,
    max_files: int = 0,
) -> list[pathlib.Path]:
    """Collect all processable files, skipping hidden dirs, manifests, and filtered/."""
    files = []
    for p in sorted(input_dir.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(input_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] == "filtered":
            continue
        files.append(p)
    if max_files > 0:
        files = files[:max_files]
    return files


def process_batch(
    input_dir: pathlib.Path,
    output_dir: pathlib.Path,
    dry_run: bool = False,
    sanitize_images: bool = True,
    convert_webp: bool = True,
    on_event: Callable[[dict], None] | None = None,
    config: dict | None = None,
    force: bool = False,
    max_files: int = 0,
) -> BatchResult:
    """Three-step pipeline: Step 1 names files, Step 2a consolidates folders, Step 2b assigns."""
    with _prevent_sleep():
        return _process_batch_inner(
            input_dir, output_dir, dry_run, sanitize_images, convert_webp,
            on_event, config, force, max_files,
        )


def _process_batch_inner(
    input_dir: pathlib.Path,
    output_dir: pathlib.Path,
    dry_run: bool,
    sanitize_images: bool,
    convert_webp: bool,
    on_event: Callable[[dict], None] | None,
    config: dict | None,
    force: bool,
    max_files: int,
) -> BatchResult:
    """Inner pipeline logic, runs inside _prevent_sleep() context."""
    cfg = config or {}
    models = cfg.get("models", {})
    vision_model = models.get("vision_model", "llava:latest")
    text_model = models.get("text_model", "qwen3:8b")
    chunk_size = cfg.get("processing", {}).get("chunk_size", 30)

    start = time.time()
    files = collect_files(input_dir, output_dir, max_files=max_files)

    # Load prior manifest for resort-awareness (skip if --force)
    if force:
        prior_files, prior_timestamp = {}, ""
    else:
        prior_files, prior_timestamp = _load_prior_manifest(output_dir)

    # Load sample descriptions (text, not images) for identification
    face_samples: dict[str, str] = {}  # {name: description_text}
    selected_samples = cfg.get("processing", {}).get("selected_samples", [])
    if cfg.get("processing", {}).get("identify_faces", True) and selected_samples:
        from afs.samples import load_sample_descriptions, describe_sample, list_samples
        face_samples = load_sample_descriptions(config=cfg, selected=selected_samples)

        # Auto-generate descriptions for samples that don't have them yet
        available = list_samples(config=cfg)
        for name in selected_samples:
            name_lower = name.lower()
            if name_lower in available and name_lower not in face_samples:
                if on_event:
                    on_event({"event": "log", "message": f"Generating description for sample: {name_lower}"})
                result = describe_sample(name_lower, config=cfg)
                if result.get("description"):
                    face_samples[name_lower] = result["description"]

    # Check if Step 2 needs to resume (files named but never sorted/moved)
    _step2_resume = False
    _manifest_data = None
    if not force:
        manifest_path = output_dir / ".afs-manifest.json"
        if manifest_path.exists():
            try:
                _manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                # If there are named-but-not-moved files, Step 2 needs to run
                unsorted = sum(1 for f in _manifest_data.get("files", [])
                               if f.get("status") == "named" and f.get("keywords"))
                if unsorted > 0:
                    _step2_resume = True
            except Exception:
                pass

    # Filter out already-sorted files
    new_files = []
    skipped = 0
    for path in files:
        if _is_already_sorted(path, prior_files, prior_timestamp):
            skipped += 1
        else:
            new_files.append(path)

    batch = BatchResult(total=len(new_files))

    if on_event:
        on_event({
            "event": "start",
            "total": len(new_files),
            "skipped": skipped,
            "input": str(input_dir),
            "output": str(output_dir),
        })

    # === Step 1: Name each file (vision model, per-file) ===
    _warm_model(vision_model, config, on_event)
    named_results: list[FileResult] = []

    for i, path in enumerate(new_files, 1):
        try:
            result = process_file(
                path, output_dir,
                dry_run=dry_run,
                sanitize_images=sanitize_images,
                convert_webp=convert_webp,
                config=config,
                face_samples=face_samples,
            )
        except Exception as e:
            result = FileResult(source=str(path), method="filtered")
            result.status = "error"
            result.error = f"unhandled: {type(e).__name__}: {e}"
            result.error_type = "unhandled"
            result.elapsed_ms = 0
            traceback.print_exc()

        batch.results.append(result)

        if result.status == "named":
            named_results.append(result)
        elif result.status == "error":
            batch.errors += 1

        if result.method == "filtered":
            batch.filtered += 1
            if result.status in ("moved", "dry-run"):
                batch.moved += 1

        if on_event:
            event = {
                "event": "progress",
                "index": i,
                "total": len(new_files),
                "file": path.name,
                "status": result.status,
                "dest": result.dest,
                "topic": result.topic,
                "phrase": result.phrase,
                "keywords": result.keywords,
                "confidence": result.confidence,
                "method": result.method,
                "tier": result.tier,
                "ms": result.elapsed_ms,
            }
            if result.identified:
                event["identified"] = result.identified
            if result.photo_detected:
                event["photo_detected"] = True
            if result.error:
                event["error"] = result.error
                event["error_type"] = result.error_type
            on_event(event)

        # Checkpoint after every file
        _write_manifest(batch, input_dir, output_dir, sanitize_images, prior_files,
                        config=cfg, skipped=skipped, dry_run=dry_run,
                        step="naming", progress=(i, len(new_files)))

    # === Step 2a: Folder consolidation (reasoning model, one call) ===
    consolidation_result = None
    prior_folders = _get_prior_folders(prior_files)
    disk_folders = scan_existing_folders(output_dir)
    all_known_folders = sorted(set(prior_folders + disk_folders))

    if all_known_folders:
        _warm_model(text_model, config, on_event)

        if on_event:
            on_event({
                "event": "step2a-start",
                "folders": len(all_known_folders),
            })

        consolidation_result = consolidate_folders(
            output_dir, all_known_folders, config=cfg, on_event=on_event,
        )

        if consolidation_result and consolidation_result.merge_map:
            merge_report = execute_merges(
                consolidation_result.merge_map, output_dir,
                dry_run=dry_run, on_event=on_event,
            )
            consolidation_result.files_moved = merge_report.files_moved
            consolidation_result.resort_files = merge_report.resort_files

            # Update prior manifest entries for merged folders
            _update_manifest_for_merges(prior_files, consolidation_result.merge_map)

            # Resort files from junk folders go through Step 1
            if consolidation_result.resort_files and not dry_run:
                if on_event:
                    on_event({
                        "event": "resort-start",
                        "files": len(consolidation_result.resort_files),
                    })
                _warm_model(vision_model, config, on_event)
                for ri, rpath in enumerate(consolidation_result.resort_files, 1):
                    try:
                        rresult = process_file(
                            rpath, output_dir,
                            dry_run=dry_run,
                            sanitize_images=sanitize_images,
                            convert_webp=convert_webp,
                            config=config,
                            face_samples=face_samples,
                        )
                    except Exception as e:
                        rresult = FileResult(source=str(rpath), method="filtered")
                        rresult.status = "error"
                        rresult.error = f"unhandled: {type(e).__name__}: {e}"
                        rresult.error_type = "unhandled"
                        rresult.elapsed_ms = 0

                    batch.results.append(rresult)
                    batch.total += 1
                    if rresult.status == "named":
                        named_results.append(rresult)
                    elif rresult.status == "error":
                        batch.errors += 1
                    if rresult.method == "filtered":
                        batch.filtered += 1
                        if rresult.status in ("moved", "dry-run"):
                            batch.moved += 1

                    if on_event:
                        event = {
                            "event": "progress",
                            "index": ri,
                            "total": len(consolidation_result.resort_files),
                            "file": rpath.name,
                            "status": rresult.status,
                            "dest": rresult.dest,
                            "topic": rresult.topic,
                            "keywords": rresult.keywords,
                            "confidence": rresult.confidence,
                            "method": rresult.method,
                            "tier": rresult.tier,
                            "ms": rresult.elapsed_ms,
                        }
                        if rresult.identified:
                            event["identified"] = rresult.identified
                        if rresult.error:
                            event["error"] = rresult.error
                            event["error_type"] = rresult.error_type
                        on_event(event)

            if on_event:
                on_event({
                    "event": "step2a-done",
                    "merges": len(consolidation_result.merge_map),
                    "folders_eliminated": consolidation_result.folders_eliminated,
                    "resort_files": len(consolidation_result.resort_files),
                    "consolidated_folders": consolidation_result.consolidated_folders,
                })

            # Use consolidated folder list for Step 2b
            prior_folders = consolidation_result.consolidated_folders
        else:
            if on_event:
                on_event({
                    "event": "step2a-done",
                    "merges": 0,
                    "folders_eliminated": 0,
                    "resort_files": 0,
                    "consolidated_folders": all_known_folders,
                })
            prior_folders = all_known_folders

    # === Photo flat sorting: rename in-place, skip Step 2b ===
    photo_sorting = cfg.get("sorting", {}).get("photo_sorting", "flat")
    if photo_sorting == "flat":
        flat_photos = [r for r in named_results if r.photo_detected]
        named_results = [r for r in named_results if not r.photo_detected]

        from afs.naming import generate_name, generate_name_from_phrase
        for result in flat_photos:
            source = pathlib.Path(result.source)
            ext = source.suffix.lower()
            if result.phrase:
                semantic = generate_name_from_phrase(result.phrase, original_stem=source.stem)
            else:
                semantic = generate_name(result.keywords, original_stem=source.stem)
            if semantic == "unsorted" and result.photo_detected:
                semantic = "photo"
            dest = source.parent / f"{semantic}{ext}"
            # If dest is the source file itself (same name), skip dedup
            if dest.exists() and dest.resolve() == source.resolve():
                pass  # same file, move_file will return False
            else:
                dest = deduplicate_path(dest)
            moved = move_file(source, dest, dry_run=dry_run)
            result.dest = str(dest)
            result.status = "dry-run" if dry_run else ("moved" if moved else "renamed")
            result.topic = "photos"
            result.folder = "photos"
            batch.moved += 1

    # Resume Step 2 if Step 1 was completed but Step 2 never ran
    if not named_results and _step2_resume:
        for entry in _manifest_data.get("files", []):
            if entry.get("status") == "named" and entry.get("keywords"):
                # Resolve source to absolute path
                source_name = entry.get("source", "")
                source_path = input_dir / source_name
                if not source_path.exists():
                    source_path = output_dir / source_name
                r = FileResult(source=str(source_path))
                r.status = "named"
                r.phrase = entry.get("phrase", "")
                r.keywords = entry.get("keywords", [])
                r.topic = entry.get("topic", "")
                r.confidence = entry.get("confidence", 0.0)
                r.identified = entry.get("identified")
                r.tier = entry.get("tier", 1)
                r.photo_detected = entry.get("photo_detected", False)
                r.method = "vision"
                named_results.append(r)
                batch.results.append(r)
        batch.total = len(named_results)
        if on_event and named_results:
            on_event({"event": "log", "message": f"Resuming Step 2 with {len(named_results)} files from prior Step 1"})

    # === Step 2b: Plan → Assign → Resolve → Verify ===
    if named_results:
        _warm_model(text_model, config, on_event)

        if on_event:
            on_event({"event": "step2-start", "files": len(named_results), "prior_folders": len(prior_folders)})

        # Pass 1: Plan folders (ONE model call)
        folders = plan_folders(named_results, prior_folders, config=cfg, on_event=on_event)

        # Pass 2: Assign files (Python string matching, ZERO model calls)
        assignments = assign_files(named_results, folders)

        misc_count = sum(1 for f in assignments.values() if f == "misc")
        if on_event:
            on_event({"event": "step2b-assign", "assigned": len(assignments), "misc": misc_count, "folders": len(set(assignments.values()))})

        # Pass 3: Resolve ambiguous (model calls for misc files only, if >10%)
        if misc_count > len(assignments) * 0.1 and misc_count > 5:
            misc_results = [r for r in named_results if assignments.get(pathlib.Path(r.source).name) == "misc"]
            resolved = resolve_ambiguous(misc_results, folders, config=cfg, on_event=on_event)
            assignments.update(resolved)
            new_misc = sum(1 for f in assignments.values() if f == "misc")
            if on_event and resolved:
                on_event({"event": "step2b-resolve", "resolved": len(resolved), "remaining_misc": new_misc})

        # Pass 4: Verify (ONE model call to merge redundant folders)
        merge_map = verify_folders(assignments, config=cfg, on_event=on_event)
        if merge_map:
            assignments = {f: merge_map.get(folder, folder) for f, folder in assignments.items()}

        # Move files to assigned folders
        assignment_summary = defaultdict(int)
        for result in named_results:
            source = pathlib.Path(result.source)
            folder = assignments.get(source.name, "misc")

            normalized = normalize_topic(folder)
            if normalized == "filtered":
                normalized = "misc"

            dest = get_destination(source, normalized, result.keywords, output_dir, phrase=result.phrase)
            moved = move_file(source, dest, dry_run=dry_run)
            result.dest = str(dest)
            result.status = "dry-run" if dry_run else ("moved" if moved else "renamed")
            result.topic = normalized
            result.folder = normalized
            batch.moved += 1
            assignment_summary[normalized] += 1

        if on_event:
            on_event({
                "event": "step2-done",
                "assignments": dict(assignment_summary),
                "folders_created": len(assignment_summary),
            })

        _write_manifest(batch, input_dir, output_dir, sanitize_images, prior_files,
                        config=cfg, skipped=skipped, dry_run=dry_run, step="sorting")

    batch.elapsed_ms = _elapsed(start)

    if on_event:
        on_event({
            "event": "done",
            "total": batch.total,
            "moved": batch.moved,
            "errors": batch.errors,
            "filtered": batch.filtered,
            "skipped": skipped,
            "ms": batch.elapsed_ms,
        })

    # Build consolidation summary for manifest
    consol_data = None
    if consolidation_result and consolidation_result.merge_map:
        consol_data = {
            "merge_map": consolidation_result.merge_map,
            "folders_before": len(all_known_folders),
            "folders_after": len(consolidation_result.consolidated_folders),
            "files_moved": consolidation_result.files_moved,
            "files_resorted": len(consolidation_result.resort_files),
        }

    # Clean up empty folders after all moves
    if cfg.get("sorting", {}).get("cleanup_empty_folders", True) and not dry_run:
        _cleanup_empty_folders(output_dir)

    _write_manifest(batch, input_dir, output_dir, sanitize_images, prior_files,
                    config=cfg, skipped=skipped, dry_run=dry_run, step="complete",
                    consolidation=consol_data)

    return batch


def reface_batch(
    input_dir: pathlib.Path,
    output_dir: pathlib.Path,
    dry_run: bool = False,
    config: dict | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> BatchResult:
    """Re-identify files using text-description matching — no vision re-analysis.

    Reads the existing manifest, generates a preview for each file,
    runs a single-image vision call WITH sample descriptions as text context,
    and renames files if a new subject is identified. Does NOT re-sort into folders.

    Works on ALL file types (not just photos) — memes, cartoons, anything.
    """
    from afs.preview import generate_preview
    from afs.naming import generate_name_from_phrase, generate_name
    from afs.samples import load_sample_descriptions, describe_sample, list_samples
    from afs.hashing import match_known_subjects, load_known_subjects

    cfg = config or {}
    start = time.time()
    batch = BatchResult()

    # Load prior manifest (required)
    prior_files, prior_timestamp = _load_prior_manifest(output_dir)
    if not prior_files:
        if on_event:
            on_event({"event": "error", "error": "No manifest found — run process first"})
        return batch

    # Load sample descriptions (required)
    selected_samples = cfg.get("processing", {}).get("selected_samples", [])
    if not selected_samples:
        if on_event:
            on_event({"event": "error", "error": "No samples selected — use --samples name1,name2"})
        return batch

    sample_descriptions = load_sample_descriptions(config=cfg, selected=selected_samples)

    # Auto-generate descriptions for samples that don't have them yet
    available = list_samples(config=cfg)
    for name in selected_samples:
        name_lower = name.lower()
        if name_lower in available and name_lower not in sample_descriptions:
            if on_event:
                on_event({"event": "log", "message": f"Generating description for sample: {name_lower}"})
            result = describe_sample(name_lower, config=cfg)
            if result.get("description"):
                sample_descriptions[name_lower] = result["description"]

    if not sample_descriptions:
        if on_event:
            on_event({"event": "error", "error": "No sample descriptions available"})
        return batch

    # Load hash DB for Layer 2
    hash_db = load_known_subjects()

    # Process ALL manifest entries (not just photos)
    entries = {name: entry for name, entry in prior_files.items()
               if entry.get("status") in ("moved", "dry-run", "named", "renamed")
               and not entry.get("error")}

    if on_event:
        on_event({
            "event": "start",
            "total": len(entries),
            "skipped": len(prior_files) - len(entries),
            "input": str(input_dir),
            "output": str(output_dir),
        })

    batch.total = len(entries)
    updated = 0
    valid_sample_names = {n.lower() for n in sample_descriptions.keys()}

    for i, (name, entry) in enumerate(sorted(entries.items()), 1):
        file_name = entry.get("name", name)
        folder = entry.get("folder", "")

        # Find file on disk
        if folder and folder not in ("photos", "filtered"):
            file_path = output_dir / folder / file_name
        else:
            file_path = output_dir / file_name
        if not file_path.exists():
            file_path = input_dir / file_name
        if not file_path.exists():
            matches = list(output_dir.rglob(file_name))
            file_path = matches[0] if matches else None

        if not file_path or not file_path.exists():
            if on_event:
                on_event({"event": "progress", "index": i, "total": batch.total,
                          "file": file_name, "status": "SKIP", "error": "file not found"})
            continue

        # Layer 2: quick hash check
        hash_matches = match_known_subjects(file_path, hash_db) if hash_db.get("subjects") else []
        enriched = dict(sample_descriptions)
        for match_name, match_dist, match_entry in hash_matches[:3]:
            if match_name not in enriched and match_entry.get("description"):
                enriched[match_name] = f"{match_entry['description']} [hash match]"

        # Generate preview
        tier = entry.get("tier", 1)
        preview_path = generate_preview(file_path, tier)
        if not preview_path:
            continue

        # Single vision call with sample descriptions as text context
        analysis = analyze_vision(
            preview_path, filename_hint=file_path.stem,
            config=config, sample_descriptions=enriched,
        )
        _cleanup(preview_path)

        new_identified = analysis.get("identified")
        # Validate against selected samples
        if new_identified and new_identified.lower() not in valid_sample_names:
            new_identified = None

        old_identified = entry.get("identified") or ""

        if not new_identified or new_identified == old_identified:
            if on_event:
                on_event({"event": "progress", "index": i, "total": batch.total,
                          "file": file_name, "status": "UNCHANGED"})
            continue

        # Build updated phrase: prepend identified name to existing phrase
        old_phrase = entry.get("phrase", "")
        new_phrase = old_phrase
        if new_identified.lower() not in new_phrase.lower():
            new_phrase = f"{new_identified} {new_phrase}".strip()

        # Generate new filename from updated phrase
        ext = file_path.suffix.lower()
        if new_phrase:
            new_stem = generate_name_from_phrase(new_phrase, original_stem=file_path.stem)
        else:
            keywords = list(entry.get("keywords", []))
            keywords.insert(0, new_identified.lower())
            new_stem = generate_name(keywords[:5], original_stem=file_path.stem)

        new_dest = file_path.parent / f"{new_stem}{ext}"
        if new_dest.exists() and new_dest.resolve() == file_path.resolve():
            pass  # same file
        else:
            new_dest = deduplicate_path(new_dest)

        moved = move_file(file_path, new_dest, dry_run=dry_run)

        # Update manifest entry
        entry["name"] = new_dest.name
        entry["phrase"] = new_phrase
        entry["identified"] = new_identified
        updated += 1
        batch.moved += 1

        if on_event:
            on_event({"event": "progress", "index": i, "total": batch.total,
                      "file": file_name, "status": "RENAMED",
                      "dest": new_dest.name, "identified": new_identified})

    batch.elapsed_ms = _elapsed(start)

    if on_event:
        on_event({
            "event": "done",
            "total": batch.total,
            "moved": updated,
            "errors": batch.errors,
            "filtered": 0,
            "skipped": len(prior_files) - len(entries),
            "ms": batch.elapsed_ms,
        })

    # Rewrite manifest with updated entries
    manifest_path = output_dir / ".afs-manifest.json"
    if manifest_path.exists() and updated > 0:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for file_entry in manifest.get("files", []):
                source_name = pathlib.Path(file_entry.get("source", "")).name
                if source_name in prior_files:
                    up = prior_files[source_name]
                    file_entry["name"] = up.get("name", file_entry.get("name"))
                    file_entry["phrase"] = up.get("phrase", file_entry.get("phrase"))
                    file_entry["identified"] = up.get("identified")
            manifest["run"]["timestamp"] = datetime.datetime.now().isoformat(timespec="seconds")
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except Exception:
            pass

    return batch


# --- Resort-awareness ---


def _load_prior_manifest(
    output_dir: pathlib.Path,
) -> tuple[dict[str, dict], str]:
    """Load prior manifest for resort-awareness."""
    manifest_path = output_dir / ".afs-manifest.json"
    try:
        if not manifest_path.exists():
            return {}, ""
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        timestamp = manifest.get("run", {}).get("timestamp", "")
        files = manifest.get("files", [])
        entries = {}
        for entry in files:
            source = entry.get("source", "")
            if source:
                name = pathlib.Path(source).name
                entries[name] = entry
        return entries, timestamp
    except Exception:
        return {}, ""


def _is_already_sorted(
    path: pathlib.Path,
    prior_files: dict[str, dict],
    prior_timestamp: str,
) -> bool:
    """Check if a file was already sorted in a prior run."""
    if not prior_files or not prior_timestamp:
        return False

    entry = prior_files.get(path.name)
    if not entry:
        return False

    if entry.get("status") in ("error", None):
        return False

    try:
        run_time = datetime.datetime.fromisoformat(prior_timestamp)
        file_mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        return file_mtime <= run_time
    except (ValueError, OSError, TypeError):
        return False


def _get_prior_folders(prior_files: dict[str, dict]) -> list[str]:
    """Extract unique folder names from prior manifest entries."""
    folders = set()
    for entry in prior_files.values():
        folder = entry.get("folder", "")
        if folder and folder != "filtered":
            folders.add(folder)
    return sorted(folders)


def _update_manifest_for_merges(
    prior_files: dict[str, dict],
    merge_map: dict[str, str],
):
    """Update prior manifest entries when folders are merged during consolidation."""
    for entry in prior_files.values():
        old_folder = entry.get("folder", "")
        if old_folder in merge_map:
            new_folder = merge_map[old_folder]
            if new_folder != RESORT_SENTINEL:
                entry["folder"] = new_folder


# --- Manifest ---


def _write_manifest(
    batch: BatchResult,
    input_dir: pathlib.Path,
    output_dir: pathlib.Path,
    sanitize_images: bool,
    prior_files: dict[str, dict] | None = None,
    config: dict | None = None,
    skipped: int = 0,
    dry_run: bool = False,
    step: str = "complete",
    progress: tuple[int, int] | None = None,
    consolidation: dict | None = None,
):
    """Write .afs-manifest.json — the factory clipboard and agent handoff artifact."""
    cfg = config or {}
    models = cfg.get("models", {})

    file_entries = []
    errors = []
    processed_names: set[str] = set()

    for r in batch.results:
        source_name = pathlib.Path(r.source).name
        cdr_applied = r.tier == 1 and sanitize_images and not r.photo_detected
        entry = {
            "source": source_name,
            "name": pathlib.Path(r.dest).name if r.dest else source_name,
            "phrase": r.phrase,
            "keywords": r.keywords,
            "folder": r.folder,
            "status": r.status,
            "tier": r.tier,
            "confidence": r.confidence,
            "identified": r.identified,
            "error": r.error,
            "error_type": r.error_type,
            "elapsed_ms": r.elapsed_ms,
            "cdr": cdr_applied,
            "original": r.original,
            "photo_detected": r.photo_detected,
        }
        file_entries.append(entry)
        processed_names.add(source_name)
        if r.original:
            processed_names.add(r.original)
        if r.error:
            errors.append({"file": source_name, "error": r.error, "error_type": r.error_type})

    # Preserve prior manifest entries for files that were skipped
    if prior_files:
        for name, entry in prior_files.items():
            if name not in processed_names:
                entry.setdefault("cdr", None)
                entry.setdefault("original", None)
                entry.setdefault("error_type", None)
                entry.setdefault("elapsed_ms", 0)
                file_entries.append(entry)

    cdr_count = sum(1 for e in file_entries if e.get("cdr"))

    folders: dict[str, list[str]] = defaultdict(list)
    for entry in file_entries:
        folder = entry.get("folder", "")
        name = entry.get("name", "")
        status = entry.get("status", "")
        if folder and name:
            folders[folder].append(name)
        elif status in ("moved", "dry-run") and entry.get("tier") == 3:
            ext = pathlib.Path(name).suffix.lower().lstrip(".") if name else "unknown"
            folders[f"filtered/{ext}"].append(name)
        elif entry.get("error") and status in ("moved", "dry-run", "error"):
            folders["filtered/errors"].append(name)

    folder_summary = {}
    for name in sorted(folders):
        files = sorted(folders[name])
        folder_summary[name] = {"count": len(files), "files": files}

    named_count = sum(1 for e in file_entries if e.get("status") in ("moved", "dry-run", "named") and not e.get("error") and e.get("tier") != 3)
    sorted_count = sum(1 for e in file_entries if e.get("folder") and e["folder"] != "filtered")
    filtered_count = sum(1 for e in file_entries if e.get("tier") == 3 or (e.get("error") and e.get("status") in ("moved", "dry-run")))
    topic_folders = [f for f in folder_summary if not f.startswith("filtered")]
    avg_confidence = 0.0
    conf_entries = [e.get("confidence", 0) for e in file_entries if e.get("confidence", 0) > 0]
    if conf_entries:
        avg_confidence = round(sum(conf_entries) / len(conf_entries), 2)

    run_info = {
        "version": VERSION,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "input": str(input_dir),
        "output": str(output_dir),
        "elapsed_ms": batch.elapsed_ms,
        "dry_run": dry_run,
        "sanitize_images": sanitize_images,
        "vision_model": models.get("vision_model", "llava:latest"),
        "text_model": models.get("text_model", "qwen3:8b"),
        "chunk_size": cfg.get("processing", {}).get("chunk_size", 30),
        "step": step,
        "skipped": skipped,
    }
    if progress:
        run_info["progress"] = f"{progress[0]}/{progress[1]}"

    manifest = {
        "run": run_info,
        "stats": {
            "total": len(file_entries),
            "named": named_count,
            "sorted": sorted_count,
            "filtered": filtered_count,
            "errors": batch.errors,
            "skipped": skipped,
            "cdr_applied": cdr_count,
            "photos_detected": sum(1 for e in file_entries if e.get("photo_detected")),
            "topic_folders": len(topic_folders),
            "avg_confidence": avg_confidence,
        },
        "files": file_entries,
        "folders": folder_summary,
        "errors": errors,
    }

    if consolidation:
        manifest["consolidation"] = consolidation

    manifest_path = output_dir / ".afs-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _cleanup(path: pathlib.Path | None):
    """Remove a temp file."""
    if path:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _cleanup_empty_folders(output_dir: pathlib.Path):
    """Remove empty subdirectories in the output directory."""
    for d in sorted(output_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if d.is_dir() and d != output_dir:
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass


def _elapsed(start: float) -> int:
    return int((time.time() - start) * 1000)
