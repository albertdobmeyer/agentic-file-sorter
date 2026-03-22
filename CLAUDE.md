# AFS — Claude Context

## Mission

Take chaotic downloaded files (primarily memes and media), give them descriptive semantic names, and sort them into topic folders. Do this securely by never opening untrusted files directly — use CDR for images, frame extraction for video, and extension-based routing for everything else.

**The formula**: classify tier → CDR/extract → vision model names each file (Step 1) → reasoning model sorts all files into folders (Step 2)

## Constitution

See `CONSTITUTION.md` — 5 axioms (AGENTIC, LITE, SEMANTIC, SECURE, VISUAL-ONLY). Read it before making any design decision.

## Quick Reference

```bash
python afs.py status                              # Pre-flight: Ollama, models, ffmpeg
python afs.py status --json                       # JSON status for agent gating
python afs.py process <dir> --dry-run             # Preview without moving
python afs.py process <dir>                       # Sort files (CDR on)
python afs.py process <dir> --force               # Ignore prior manifest, reprocess all
python afs.py process <dir> --max-files 20        # Spot-check first 20 files
python afs.py process <dir> --no-sanitize         # Skip CDR re-rendering
python afs.py process <dir> --no-convert-webp     # Keep WebP as-is during CDR
python afs.py process <dir> --config /path/to.json # Alternate config file
python afs.py process <dir> -o <out> --json       # NDJSON mode for agents
python afs.py --version                           # Print version
```

## NDJSON Event Stream (`--json` mode)

One JSON object per line on stdout. Event types:

| Event | When | Key Fields |
|-------|------|------------|
| `start` | Run begins | `total`, `skipped`, `input`, `output` |
| `warm` | Model loading into GPU | `model` |
| `progress` | After each file (Step 1) | `index`, `total`, `file`, `status`, `tier`, `confidence`, `keywords`, `error`, `error_type` |
| `step2a-start` | Folder consolidation begins | `folders` |
| `step2a-done` | Consolidation complete | `merges`, `folders_eliminated`, `resort_files`, `consolidated_folders` |
| `resort-start` | Re-sorting junk folder files | `files` |
| `step2-start` | Step 2b file assignment begins | `files`, `chunks`, `prior_folders` |
| `step2-chunk` | After each chunk (large batches) | `chunk`, `of`, `assigned`, `folders_so_far` |
| `step2-done` | Step 2b complete | `assignments`, `folders_created` |
| `step2-error` | Step 2b failed | `error`, `error_type` |
| `done` | Run complete | `total`, `moved`, `errors`, `filtered`, `skipped`, `ms` |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Bad input (missing directory) |
| 2 | Ollama unreachable |
| 3 | All files failed or Step 2 total failure |

## Manifest (`.afs-manifest.json`)

Written to the output directory after every file (crash-safe checkpointing). Agent handoff artifact.

**`run` block**: `version`, `timestamp`, `input`, `output`, `elapsed_ms`, `dry_run`, `sanitize_images`, `vision_model`, `text_model`, `chunk_size`, `step`, `skipped`, `progress`

**`stats` block**: `total`, `named`, `sorted`, `filtered`, `errors`, `skipped`, `cdr_applied`, `topic_folders`, `avg_confidence`

**Per-file entries**: `source`, `name`, `keywords`, `folder`, `status`, `tier`, `confidence`, `identified`, `error`, `error_type`, `elapsed_ms`, `cdr`, `original`

**`consolidation` block** (present when Step 2a ran): `merge_map`, `folders_before`, `folders_after`, `files_moved`, `files_resorted`

## Error Types

Structured `error_type` field in manifest entries and NDJSON events:

| Type | Meaning |
|------|---------|
| `ollama_unreachable` | Connection refused / DNS failure |
| `model_timeout` | Read timeout during inference |
| `model_error` | HTTP 4xx/5xx from Ollama |
| `parse_failure` | Model returned unparseable response |
| `file_read_error` | Cannot read the file from disk |
| `preview_failed` | Preview generation failed |
| `unhandled` | Unexpected exception |
| `unassigned` | Step 2 did not assign a folder |
| `reserved_name` | Step 2 assigned reserved name "filtered" |

## Configuration

**Priority**: `afs-config.json` < `.env` < environment variables < CLI flags

### `afs-config.json` (primary — project root)

```json
{
  "models": {
    "ollama_url": "http://localhost:11434",
    "vision_model": "llava:latest",
    "text_model": "qwen3:8b",
    "vision_timeout": 180,
    "text_timeout": 120,
    "keep_alive": "30m"
  },
  "processing": {
    "sanitize_images": true,
    "convert_webp": true,
    "chunk_size": 30,
    "confidence_threshold": 0.5
  },
  "sorting": {
    "max_topics": 25,
    "max_topic_words": 2,
    "cleanup_empty_folders": true,
    "group_by_topic": [".jpg", ".jpeg", ".png", ".gif", "..."],
    "group_by_type": [".webm", ".mp4", ".psd", ".xlsx", "..."],
    "custom_folders": {"biz": ["business", "stock", "finance"]},
    "folder_aliases": {"pol": "politics", "sci": "science"}
  }
}
```

### `.env` (fallback — backward compat)

```env
SANITIZE_IMAGES=true
CONVERT_WEBP=true
OLLAMA_URL=http://localhost:11434
VISION_MODEL=llava:latest
TEXT_MODEL=qwen3:8b
VISION_TIMEOUT=180
TEXT_TIMEOUT=120
```

## Project Structure

```
agentic-file-sorter/
├── afs.py              Entry point (thin shim → afs.cli.main)
├── afs/                     Source package
│   ├── __init__.py
│   ├── cli.py               CLI, event formatting, exit codes
│   ├── config.py             Central config loader (json > env > defaults)
│   ├── pipeline.py           Orchestrator: Step 1, Step 2a, Step 2b, manifest, resort-awareness
│   ├── consolidate.py        Step 2a: folder consolidation, merge execution
│   ├── batch_sort.py         Step 2b: prompt building, reasoning model call, chunking
│   ├── analyze.py            Ollama vision API, character identification, error classification
│   ├── preview.py            CDR re-rendering + preview generation
│   ├── naming.py             Keywords → kebab-case semantic filename
│   ├── sorting.py            Topic normalization, folder routing, file moving
│   └── types_.py             FileResult/BatchResult, tier classification, extension sets
├── tests/
│   ├── test_pipeline.py      Test suite (57 tests)
│   └── fixtures/             Test images
├── afs-config.json          User preferences (only non-default values)
├── .env                     Fallback configuration
├── requirements.txt
├── CONSTITUTION.md
├── CLAUDE.md                This file
└── README.md
```

## Pipeline

```
STEP 1 — NAMING (per-file, vision model)
file → classify_tier()
  ├─ Already sorted? (manifest + mtime, unless --force) → SKIP
  ├─ Tier 3 → filtered/{ext}/ (no analysis)
  ├─ Tier 1 → apply_cdr() → generate_preview() → analyze_vision() → semantic name
  └─ Tier 2 → generate_preview() → analyze_vision() → semantic name (original untouched)
  └─ manifest checkpoint after EVERY file

STEP 2a — FOLDER CONSOLIDATION (one call, reasoning model) [consolidate.py]
all folder names → reasoning model → merge map
  ├─ Merges redundant/overlapping folders (pol→politics, sci→science)
  ├─ Flags junk extension folders (jpg/, png/) as RESORT
  ├─ Executes folder merges (physically moves files)
  └─ RESORT files queued for Step 1 processing

STEP 2b — FILE ASSIGNMENT (chunked, reasoning model) [batch_sort.py]
named files + consolidated folders → reasoning model → topic folder assignments
  ├─ Chunks large batches (chunk_size per call, step2-chunk events)
  ├─ Custom folders matched by keyword triggers
  ├─ Folder aliases resolved
  └─ Works against consolidated folder list (not original 100+ folders)
→ final manifest (step: "complete")
```

## Conventions

- All source code in `afs/` package — imports use `from afs.X import Y`
- Config loaded once in `cli.py`, threaded through as `config` dict parameter
- Vision model names files (Step 1). Reasoning model sorts (Step 2). Two steps, clean manifest handoff
- Errors classified with `error_type` — agents read the type, humans read the message
- Any failure routes to `filtered/errors/` with error recorded in manifest
- `filtered/` is reserved — never merged, renamed, or reorganized
