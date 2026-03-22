"""Orchestrator — three-step pipeline per the constitution.

Step 1  (per-file, vision model):  classify tier → CDR → preview → vision → semantic name
Step 2a (one call, reasoning model): holistic folder consolidation → merge map → execute merges
Step 2b (chunked, reasoning model): batch file assignment → moves
"""

import datetime
import json
import pathlib
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
) -> FileResult:
    """Step 1: Analyze and name a single file. No moves except Tier 3 and errors."""
    start = time.time()
    result = FileResult(source=str(path))

    tier = classify_tier(path)
    result.tier = tier

    # Tier 3: no analysis, route to filtered/ immediately
    if tier == 3:
        return _move_to_filtered(path, output_dir, dry_run, start)

    try:
        # Tier 1: CDR (re-render via PIL, stripping non-pixel data)
        if tier == 1 and sanitize_images:
            result.original = path.name  # preserve pre-CDR filename
            path = apply_cdr(path, convert_webp=convert_webp)
            result.source = str(path)  # path may have changed (webp → jpg)

        # Generate preview for vision model
        preview_path = generate_preview(path, tier)
        if not preview_path:
            return _move_to_errors(path, output_dir, dry_run, start,
                                   "preview generation failed", "preview_failed")

        # Vision analysis (single retry on timeout)
        analysis = analyze_vision(preview_path, filename_hint=path.stem, config=config)
        if analysis.get("error_type") == "model_timeout":
            analysis = analyze_vision(preview_path, filename_hint=path.stem, config=config)

        if "error" in analysis:
            _cleanup(preview_path)
            return _move_to_errors(path, output_dir, dry_run, start,
                                   analysis["error"],
                                   analysis.get("error_type", "unhandled"))

        topic = analysis["topic"]
        keywords = analysis["keywords"]
        confidence = analysis["confidence"]
        identified = None
        result.method = "vision"

        # Character identification (if analysis is generic)
        if needs_identification(topic, keywords, confidence, config=config):
            char_name = identify_character(preview_path, config=config)
            if char_name:
                identified = char_name
                keywords = enhance_with_character(keywords, char_name)
                confidence = max(confidence, 0.7)

        _cleanup(preview_path)

        # Store analysis results — no folder matching, no moves
        result.status = "named"
        result.topic = topic
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
    """Two-step pipeline: Step 1 names all files, Step 2 sorts them into folders."""
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

                dest = get_destination(source, normalized, result.keywords, output_dir)
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

    _write_manifest(batch, input_dir, output_dir, sanitize_images, prior_files,
                    config=cfg, skipped=skipped, dry_run=dry_run, step="complete",
                    consolidation=consol_data)

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
        cdr_applied = r.tier == 1 and sanitize_images
        entry = {
            "source": source_name,
            "name": pathlib.Path(r.dest).name if r.dest else source_name,
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


def _elapsed(start: float) -> int:
    return int((time.time() - start) * 1000)
