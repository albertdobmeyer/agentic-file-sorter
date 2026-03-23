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
    needs_identification,
    identify_character,
    enhance_with_character,
    ERR_FILE_READ,
)
from afs.naming import deduplicate_path
from afs.sorting import normalize_topic, get_destination, move_file, scan_existing_folders
from afs.batch_sort import step2_batch_sort, _single_call
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
    face_samples: dict[str, list[str]] | None = None,
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

        # Vision analysis (single retry on timeout)
        analysis = analyze_vision(preview_path, filename_hint=path.stem,
                                  config=config, photo_hint=is_photo)
        if analysis.get("error_type") == "model_timeout":
            analysis = analyze_vision(preview_path, filename_hint=path.stem,
                                      config=config, photo_hint=is_photo)

        if "error" in analysis:
            _cleanup(preview_path)
            return _move_to_errors(path, output_dir, dry_run, start,
                                   analysis["error"],
                                   analysis.get("error_type", "unhandled"))

        topic = analysis["topic"]
        phrase = analysis.get("phrase", "")
        keywords = analysis["keywords"]
        confidence = analysis["confidence"]
        identified = None
        result.method = "vision"

        # Character identification (memes/cartoons — not photos)
        if not is_photo and needs_identification(topic, keywords, confidence, config=config):
            char_name = identify_character(preview_path, config=config)
            if char_name:
                identified = char_name
                phrase, keywords = enhance_with_character(phrase, keywords, char_name)
                confidence = max(confidence, 0.7)

        # Sample identification (any file type when samples are loaded)
        if face_samples:
            from afs.faces import identify_faces
            matched_names = identify_faces(preview_path, face_samples, config=config)
            if matched_names:
                identified = ", ".join(matched_names)
                # Prepend names to phrase
                names_str = " and ".join(matched_names)
                if names_str.lower() not in phrase.lower():
                    phrase = f"{names_str} {phrase}".strip()
                # Also update keywords for Step 2
                for name in reversed(matched_names):
                    if name.lower() not in [kw.lower() for kw in keywords]:
                        keywords.insert(0, name.lower())
                keywords = keywords[:5]
                confidence = max(confidence, 0.8)

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

    # Load face samples once (reused across all files)
    face_samples: dict[str, list[str]] = {}
    if cfg.get("processing", {}).get("identify_faces", True):
        from afs.faces import load_face_samples
        face_samples = load_face_samples(config=cfg)

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

    # === Step 2b: Batch sort — file assignment (reasoning model, chunked) ===
    if named_results:
        _warm_model(text_model, config, on_event)

        chunks = (len(named_results) + chunk_size - 1) // chunk_size
        if on_event:
            on_event({
                "event": "step2-start",
                "files": len(named_results),
                "chunks": chunks,
                "prior_folders": len(prior_folders),
            })

        assignments = step2_batch_sort(
            named_results, prior_folders, config=cfg, on_event=on_event,
        )

        if assignments is None:
            err_type = getattr(_single_call, "last_error_type", None) or "unknown"
            err_msg = getattr(_single_call, "last_error", None) or "reasoning model returned no valid assignments"
            if on_event:
                on_event({
                    "event": "step2-error",
                    "error": err_msg,
                    "error_type": err_type,
                })
            for result in named_results:
                source = pathlib.Path(result.source)
                dest = output_dir / "filtered" / "errors" / source.name
                dest = deduplicate_path(dest)
                move_file(source, dest, dry_run=dry_run)
                result.dest = str(dest)
                result.status = "dry-run" if dry_run else "moved"
                result.topic = "filtered"
                result.error = f"step 2 failure: {err_msg}"
                result.error_type = err_type
                result.folder = ""
                batch.errors += 1
                batch.filtered += 1
            _write_manifest(batch, input_dir, output_dir, sanitize_images, prior_files,
                            config=cfg, skipped=skipped, dry_run=dry_run, step="sorting")
        else:
            assignment_summary = defaultdict(int)
            for result in named_results:
                source = pathlib.Path(result.source)
                folder = assignments.get(source.name)

                if not folder:
                    dest = output_dir / "filtered" / "errors" / source.name
                    dest = deduplicate_path(dest)
                    move_file(source, dest, dry_run=dry_run)
                    result.dest = str(dest)
                    result.status = "dry-run" if dry_run else "moved"
                    result.topic = "filtered"
                    result.error = "step 2: no folder assigned"
                    result.error_type = "unassigned"
                    result.folder = ""
                    batch.errors += 1
                    batch.filtered += 1
                    continue

                normalized = normalize_topic(folder)
                if normalized == "filtered":
                    dest = output_dir / "filtered" / "errors" / source.name
                    dest = deduplicate_path(dest)
                    move_file(source, dest, dry_run=dry_run)
                    result.dest = str(dest)
                    result.status = "dry-run" if dry_run else "moved"
                    result.topic = "filtered"
                    result.error = "step 2: reserved folder name 'filtered'"
                    result.error_type = "reserved_name"
                    result.folder = ""
                    batch.errors += 1
                    batch.filtered += 1
                    continue

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
    """Re-run face identification only — skip vision analysis, reuse manifest keywords.

    Loads the prior manifest, re-runs identify_faces() on photos with updated
    face samples, and renames files if the identification changed.
    """
    from afs.faces import load_face_samples, identify_faces, has_face_samples
    from afs.preview import generate_preview
    from afs.naming import generate_name

    cfg = config or {}
    start = time.time()
    batch = BatchResult()

    # Load prior manifest (required)
    prior_files, prior_timestamp = _load_prior_manifest(output_dir)
    if not prior_files:
        if on_event:
            on_event({"event": "error", "error": "No manifest found — run process first"})
        return batch

    # Load face samples (required)
    face_samples = load_face_samples(config=cfg)
    if not face_samples:
        if on_event:
            on_event({"event": "error", "error": "No face samples found in faces/ directory"})
        return batch

    # Filter to photo entries only
    photo_entries = {name: entry for name, entry in prior_files.items()
                     if entry.get("photo_detected")}

    if on_event:
        on_event({
            "event": "start",
            "total": len(photo_entries),
            "skipped": len(prior_files) - len(photo_entries),
            "input": str(input_dir),
            "output": str(output_dir),
        })

    batch.total = len(photo_entries)
    updated = 0

    for i, (name, entry) in enumerate(sorted(photo_entries.items()), 1):
        # Find the file on disk (may have been renamed)
        file_name = entry.get("name", name)
        folder = entry.get("folder", "")
        if folder and folder != "photos":
            file_path = output_dir / folder / file_name
        else:
            file_path = output_dir / file_name

        # Try input dir if not found in output
        if not file_path.exists():
            file_path = input_dir / file_name
        if not file_path.exists():
            # Search recursively
            matches = list(output_dir.rglob(file_name))
            file_path = matches[0] if matches else None

        if not file_path or not file_path.exists():
            batch.errors += 1
            if on_event:
                on_event({"event": "progress", "index": i, "total": batch.total,
                          "file": file_name, "status": "ERROR", "error": "file not found"})
            continue

        # Generate preview for face identification
        tier = entry.get("tier", 1)
        preview_path = generate_preview(file_path, tier)
        if not preview_path:
            batch.errors += 1
            continue

        # Run face identification with updated samples
        matched_names = identify_faces(preview_path, face_samples, config=cfg)
        _cleanup(preview_path)

        old_identified = entry.get("identified") or ""
        new_identified = ", ".join(matched_names) if matched_names else ""

        # Check if identification changed
        if new_identified == old_identified:
            if on_event:
                on_event({"event": "progress", "index": i, "total": batch.total,
                          "file": file_name, "status": "UNCHANGED"})
            continue

        # Update keywords with new face matches
        keywords = list(entry.get("keywords", []))
        # Remove old face names from keywords
        if old_identified:
            old_names = {n.strip().lower() for n in old_identified.split(",")}
            keywords = [kw for kw in keywords if kw.lower() not in old_names]
        # Prepend new face names
        for fname in reversed(matched_names):
            if fname.lower() not in [kw.lower() for kw in keywords]:
                keywords.insert(0, fname.lower())
        keywords = keywords[:5]

        # Generate new filename
        ext = file_path.suffix.lower()
        new_stem = generate_name(keywords, original_stem=file_path.stem)
        new_dest = file_path.parent / f"{new_stem}{ext}"
        new_dest = deduplicate_path(new_dest)

        moved = move_file(file_path, new_dest, dry_run=dry_run)

        # Update manifest entry
        entry["name"] = new_dest.name
        entry["keywords"] = keywords
        entry["identified"] = new_identified or None
        updated += 1
        batch.moved += 1

        if on_event:
            ident_tag = f" [{new_identified}]" if new_identified else ""
            on_event({"event": "progress", "index": i, "total": batch.total,
                      "file": file_name, "status": "RENAMED",
                      "dest": str(new_dest), "identified": new_identified})

    batch.elapsed_ms = _elapsed(start)

    if on_event:
        on_event({
            "event": "done",
            "total": batch.total,
            "moved": updated,
            "errors": batch.errors,
            "filtered": 0,
            "skipped": len(prior_files) - len(photo_entries),
            "ms": batch.elapsed_ms,
        })

    # Rewrite manifest with updated entries
    manifest_path = output_dir / ".afs-manifest.json"
    if manifest_path.exists() and (updated > 0 or dry_run):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            # Update file entries in manifest
            for file_entry in manifest.get("files", []):
                source = file_entry.get("source", "")
                name = pathlib.Path(source).name
                if name in prior_files:
                    updated_entry = prior_files[name]
                    file_entry["name"] = updated_entry.get("name", file_entry.get("name"))
                    file_entry["keywords"] = updated_entry.get("keywords", file_entry.get("keywords"))
                    file_entry["identified"] = updated_entry.get("identified", file_entry.get("identified"))
            manifest["run"]["timestamp"] = datetime.datetime.now().isoformat(timespec="seconds")
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except Exception:
            pass  # best-effort manifest update

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
