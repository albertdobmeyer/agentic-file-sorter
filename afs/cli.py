"""Agentic File Sorter — CLI and event formatting.

Usage:
    python afs.py process <input_dir> [--output DIR] [--dry-run] [--force]
    python afs.py status
    python afs.py --json process <input_dir>
"""

import argparse
import json
import pathlib
import shutil
import sys

import requests

from afs.config import load_config, VERSION


# Exit codes
EXIT_OK = 0
EXIT_BAD_INPUT = 1
EXIT_OLLAMA_DOWN = 2
EXIT_ALL_FAILED = 3


def main():
    parser = argparse.ArgumentParser(
        prog="afs",
        description="Agentic File Sorter — secure semantic naming for downloaded media",
    )
    parser.add_argument("--json", action="store_true", help="NDJSON output mode (one JSON object per line)")
    parser.add_argument("--version", action="version", version=f"afs {VERSION}")
    sub = parser.add_subparsers(dest="command")

    # process command
    proc = sub.add_parser("process", help="Process files in a directory")
    proc.add_argument("input", help="Input directory to process")
    proc.add_argument("--output", "-o", help="Output directory (default: same as input)")
    proc.add_argument("--dry-run", action="store_true", help="Preview without moving files")
    proc.add_argument("--force", action="store_true", help="Ignore prior manifest — reprocess all files")
    proc.add_argument("--max-files", type=int, default=0, help="Process only the first N files (0 = all)")
    proc.add_argument("--config", type=str, default=None, help="Path to afs-config.json (default: project root)")
    proc.add_argument(
        "--no-sanitize", action="store_true",
        help="Skip CDR re-rendering (treat Tier 1 as Tier 2)",
    )
    proc.add_argument(
        "--no-convert-webp", action="store_true",
        help="Keep WebP files as-is during CDR (don't convert to JPG)",
    )
    proc.add_argument(
        "--reface", action="store_true",
        help="Re-run face identification only (skip vision analysis, reuse manifest keywords)",
    )
    proc.add_argument(
        "--samples", type=str, default="",
        help="Comma-separated sample names to compare against (e.g. --samples tori,pepe). Without this, no sample identification runs.",
    )

    # status command
    stat = sub.add_parser("status", help="Check Ollama connectivity and model availability")
    stat.add_argument("--config", type=str, default=None, help="Path to afs-config.json")

    # flatten command
    flat = sub.add_parser("flatten", help="Move all files from subfolders to root")
    flat.add_argument("input", help="Directory to flatten")
    flat.add_argument("--dry-run", action="store_true", help="Preview without moving files")

    # dashboard command
    dash = sub.add_parser("dashboard", help="Open settings dashboard in browser")
    dash.add_argument("--port", type=int, default=7860, help="Dashboard port (default: 7860)")

    # check-samples command
    chk = sub.add_parser("check-samples", help="Evaluate sample image quality and generate descriptions")
    chk.add_argument("name", nargs="?", help="Specific sample group to check (optional, default: all)")
    chk.add_argument("--config", type=str, default=None, help="Path to afs-config.json")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(EXIT_BAD_INPUT)

    # Load config
    config_path = pathlib.Path(args.config) if hasattr(args, "config") and args.config else None
    cfg = load_config(config_path)

    if args.command == "status":
        _cmd_status(args.json, cfg)
    elif args.command == "check-samples":
        _cmd_check_samples(args, cfg)
    elif args.command == "process":
        _cmd_process(args, cfg)
    elif args.command == "flatten":
        _cmd_flatten(args)
    elif args.command == "dashboard":
        _cmd_dashboard(args)


def _cmd_check_samples(args, cfg):
    """Evaluate sample images and generate text descriptions."""
    from afs.samples import describe_sample, describe_all_samples, list_samples

    available = list_samples(cfg)
    if not available:
        print("  No samples found. Add images to the samples/ directory.")
        return

    LABELS = {"green": "GREAT", "blue": "OK", "orange": "LACKING", "red": "UNUSABLE"}

    if args.name:
        name = args.name.lower().strip()
        if name not in available:
            print(f"  Sample '{args.name}' not found. Available: {', '.join(available.keys())}")
            return
        print(f"\n  Checking: {name}/ ({available[name]} sample(s))...")
        result = describe_sample(name, config=cfg)
        if result.get("error"):
            print(f"  Error: {result['error']}")
        else:
            label = LABELS.get(result.get("rating", ""), "?")
            print(f"  [{label}] {result.get('description', 'no description')}")
            print(f"  Suggestion: {result.get('suggestion', '')}\n")
    else:
        print(f"\n  Checking {len(available)} sample group(s)...\n")
        results = describe_all_samples(config=cfg)
        for r in results:
            label = LABELS.get(r.get("rating", ""), "?")
            count = r.get("sample_count", 0)
            if r.get("error"):
                print(f"  {r['name']}/ ({count}) — ERROR: {r['error']}")
            else:
                print(f"  {r['name']}/ ({count} sample{'s' if count != 1 else ''}) [{label}]")
                print(f"    {r.get('description', 'no description')}")
                print(f"    Suggestion: {r.get('suggestion', '')}")
        print()


def _cmd_dashboard(args):
    """Launch the settings dashboard in browser."""
    import subprocess
    import webbrowser

    dashboard_dir = pathlib.Path(__file__).parent.parent / "dashboard"
    server_js = dashboard_dir / "server.js"

    if not server_js.exists():
        print("Error: dashboard/server.js not found", file=sys.stderr)
        sys.exit(EXIT_BAD_INPUT)

    if not shutil.which("node"):
        print("Error: Node.js not found. Install from https://nodejs.org", file=sys.stderr)
        sys.exit(EXIT_BAD_INPUT)

    # Install deps if needed
    if not (dashboard_dir / "node_modules").exists():
        print("  Installing dashboard dependencies...")
        subprocess.run(["npm", "install"], cwd=str(dashboard_dir), check=True)

    port = args.port
    print(f"\n  AFS Dashboard: http://localhost:{port}\n  Press Ctrl+C to stop.\n")

    proc = subprocess.Popen(
        ["node", str(server_js), "--port", str(port)],
        cwd=str(dashboard_dir),
    )

    try:
        webbrowser.open(f"http://localhost:{port}")
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print("\n  Dashboard stopped.")


def _cmd_status(json_mode: bool, cfg: dict):
    """Check Ollama connectivity, models, and dependencies."""
    models_cfg = cfg.get("models", {})
    ollama_url = models_cfg.get("ollama_url", "http://localhost:11434")
    vision_model = models_cfg.get("vision_model", "llava:latest")
    text_model = models_cfg.get("text_model", "qwen3:8b")
    sanitize = cfg.get("processing", {}).get("sanitize_images", True)
    convert_webp = cfg.get("processing", {}).get("convert_webp", True)

    try:
        resp = requests.get(f"{ollama_url}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        online = True
    except Exception:
        models = []
        online = False

    ffmpeg_available = shutil.which("ffmpeg") is not None

    result = {
        "event": "status",
        "version": VERSION,
        "ollama_online": online,
        "ollama_url": ollama_url,
        "vision_model": vision_model,
        "text_model": text_model,
        "sanitize_images": sanitize,
        "convert_webp": convert_webp,
        "ffmpeg_available": ffmpeg_available,
        "models_available": models,
    }

    if json_mode:
        print(json.dumps(result))
    else:
        status = "ONLINE" if online else "OFFLINE"
        print(f"Ollama: {status} ({ollama_url})")
        print(f"Vision model: {vision_model}")
        print(f"Text model: {text_model}")
        print(f"CDR (sanitize): {'ON' if sanitize else 'OFF'}")
        print(f"Convert WebP: {'ON' if convert_webp else 'OFF'}")
        print(f"ffmpeg: {'FOUND' if ffmpeg_available else 'NOT FOUND'}")
        if models:
            print(f"Available: {', '.join(models)}")
        else:
            print("No models found" if online else "Cannot connect to Ollama")

    if not online:
        sys.exit(EXIT_OLLAMA_DOWN)


def _cmd_process(args, cfg: dict):
    """Process files in a directory."""
    from afs.pipeline import process_batch, reface_batch

    input_dir = pathlib.Path(args.input).resolve()
    output_dir = pathlib.Path(args.output).resolve() if args.output else input_dir

    if not input_dir.exists():
        _error(f"Input directory not found: {input_dir}", args.json)
        sys.exit(EXIT_BAD_INPUT)

    json_mode = args.json

    def on_event(event):
        if json_mode:
            print(json.dumps(event), flush=True)
        else:
            _print_human(event)

    # --samples flag: select which samples to compare against (applies to both process and reface)
    if args.samples:
        selected = [s.strip() for s in args.samples.split(",") if s.strip()]
        cfg.setdefault("processing", {})["selected_samples"] = selected

    # --reface: lightweight re-identification only (uses text descriptions, not vision re-analysis)
    if args.reface:
        batch = reface_batch(
            input_dir=input_dir,
            output_dir=output_dir,
            dry_run=args.dry_run,
            config=cfg,
            on_event=on_event,
        )
    else:
        sanitize = cfg.get("processing", {}).get("sanitize_images", True)
        convert_webp = cfg.get("processing", {}).get("convert_webp", True)

        if args.no_sanitize:
            sanitize = False
        if args.no_convert_webp:
            convert_webp = False

        # Delete manifest if --force
        if args.force:
            manifest_path = output_dir / ".afs-manifest.json"
            if manifest_path.exists():
                manifest_path.unlink()

        batch = process_batch(
            input_dir=input_dir,
            output_dir=output_dir,
            dry_run=args.dry_run,
            sanitize_images=sanitize,
            convert_webp=convert_webp,
            on_event=on_event,
            config=cfg,
            force=args.force,
            max_files=args.max_files,
        )

    # Exit code based on outcome
    if batch.total > 0 and batch.errors == batch.total:
        sys.exit(EXIT_ALL_FAILED)
    # Step 2 total failure: all named files ended up in errors
    named_count = sum(1 for r in batch.results if r.method == "vision")
    if named_count > 0 and all(r.error for r in batch.results if r.method == "vision"):
        sys.exit(EXIT_ALL_FAILED)


def _cmd_flatten(args):
    """Flatten all topic folders — move files to root."""
    from afs.sorting import flatten_directory

    target = pathlib.Path(args.input).resolve()
    if not target.exists():
        print(f"Error: Directory not found: {target}", file=sys.stderr)
        sys.exit(EXIT_BAD_INPUT)

    dry_label = " (dry run)" if args.dry_run else ""
    print(f"\n  Flattening: {target}{dry_label}\n")

    def on_event(event):
        if event.get("event") == "flatten-progress":
            print(f"  {event['from']}/{event['file']} -> {event['to']}")

    result = flatten_directory(target, dry_run=args.dry_run, on_event=on_event)

    print(f"\n  Done: {result['files_moved']} files moved, "
          f"{result['folders_removed']} folders removed, "
          f"{result['collisions']} collisions resolved\n")


def _print_human(event: dict):
    """Pretty-print an event for human consumption."""
    ev = event["event"]
    if ev == "start":
        skipped = event.get("skipped", 0)
        skip_msg = f" (skipping {skipped} already sorted)" if skipped else ""
        print(f"\n  Processing {event['total']} files{skip_msg}")
        print(f"  Input:  {event['input']}")
        print(f"  Output: {event['output']}\n")
    elif ev == "warm":
        print(f"  Loading model: {event.get('model', '?')}...")
    elif ev == "progress":
        status = event["status"].upper()
        name = event["file"]
        tier = event.get("tier", "?")
        total = event.get("total", "?")
        ident = f" [{event['identified']}]" if event.get("identified") else ""
        photo_tag = " [photo]" if event.get("photo_detected") else ""
        if event.get("dest"):
            dest = pathlib.Path(event["dest"]).name
            topic = event.get("topic", "")
            print(f"  [{event['index']}/{total}] T{tier} {status}: {name} -> {topic}/{dest}{ident}")
        elif status == "NAMED":
            desc = event.get("phrase") or ", ".join(event.get("keywords", [])[:3])
            print(f"  [{event['index']}/{total}] T{tier} {status}: {name} [{desc}]{ident}{photo_tag}")
        elif event.get("error"):
            err_type = event.get("error_type", "")
            print(f"  [{event['index']}/{total}] T{tier} {status}: {name} -- [{err_type}] {event['error']}")
    elif ev == "done":
        skipped = event.get("skipped", 0)
        skip_msg = f", {skipped} skipped" if skipped else ""
        print(f"\n  Done: {event['moved']} moved, {event['errors']} errors, "
              f"{event['filtered']} filtered{skip_msg} ({event['ms']}ms)\n")
    elif ev == "step2-start":
        count = event.get("files", 0)
        chunks = event.get("chunks", 1)
        prior = event.get("prior_folders", 0)
        chunk_msg = f" in {chunks} chunks" if chunks > 1 else ""
        prior_msg = f" ({prior} existing folders)" if prior else ""
        print(f"\n  Step 2: Sorting {count} files into folders{chunk_msg}{prior_msg}...")
    elif ev == "step2-chunk":
        print(f"    Chunk {event['chunk']}/{event['of']}: {event['assigned']} assigned, {event['folders_so_far']} folders")
    elif ev == "step2-done":
        assignments = event.get("assignments", {})
        folders_created = event.get("folders_created", 0)
        print(f"  Step 2 done: {folders_created} folders assigned")
        for folder, count in sorted(assignments.items()):
            print(f"    {folder}: {count} files")
    elif ev == "step2a-start":
        count = event.get("folders", 0)
        print(f"\n  Step 2a: Consolidating {count} folders...")
    elif ev == "step2a-done":
        merges = event.get("merges", 0)
        eliminated = event.get("folders_eliminated", 0)
        resort = event.get("resort_files", 0)
        consolidated = event.get("consolidated_folders", [])
        if merges:
            print(f"  Step 2a done: {merges} merges, {eliminated} folders eliminated, {resort} files queued for re-sort")
            print(f"    Consolidated to {len(consolidated)} folders: {', '.join(consolidated[:15])}")
            if len(consolidated) > 15:
                print(f"    ... and {len(consolidated) - 15} more")
        else:
            print(f"  Step 2a: No consolidation needed ({len(consolidated)} folders)")
    elif ev == "resort-start":
        count = event.get("files", 0)
        print(f"\n  Re-sorting {count} files from junk folders...")
    elif ev == "step2-error":
        err_type = event.get("error_type", "unknown")
        print(f"  Step 2 failed [{err_type}]: {event.get('error', '')}")


def _error(msg: str, json_mode: bool):
    if json_mode:
        print(json.dumps({"event": "error", "error": msg}))
    else:
        print(f"Error: {msg}", file=sys.stderr)


if __name__ == "__main__":
    main()
