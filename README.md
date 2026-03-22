# AFS -- Agentic File Sorter

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/albertdobmeyer/agentic-file-sorter)](https://github.com/albertdobmeyer/agentic-file-sorter/releases)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://python.org)

**A local vision pipeline that turns chaotic download folders into semantically named, topic-organized files. Built to be invoked by CLI agents. Zero cloud tokens burned.**

> **Using Claude Code?** Point it at this repo. It reads [CLAUDE.md](CLAUDE.md) automatically and knows how to set up, run, and monitor AFS -- no manual required.

---

## Before / After

```
Downloads/                          ->    Downloads/
  IMG_8847.jpg                             animals/
  photo(2).png                               golden-retriever-park.jpg
  meme final FINAL (1).webp                  cat-sleeping-couch.png
  fjk29x.jpg                              memes/
  reaction4554.png                           pepe-smug-sunglasses.png
  video_2024_01.mp4                          spongebob-tired-monday.webp
  document.pdf                             filtered/
                                             pdf/
                                               document.pdf
                                           .afs-manifest.json
```

Every file gets a 2-5 word kebab-case name derived from what the vision model actually sees in it. A reasoning model then organizes the named files into coherent topic folders in a single holistic pass.

---

## Why This Exists -- MCP vs Agentic Tools

The conventional way to give a CLI agent file-sorting powers is an MCP server: expose `analyze_file`, `assign_folder`, `move_file` as fine-grained tools, and let the agent orchestrate each call. That works -- but the economics are brutal.

**The MCP approach (per-call orchestration):**

- Agent calls `analyze_file` for every file. 3,000 files = 3,000 round-trips through the agent's context.
- Every call burns agent tokens -- the analysis prompt, the response parsing, the folder decision, the move command.
- Results live in conversation context. When the session ends (or compresses), they vanish.
- The agent must stay active for the entire run. No hands-off automation.

**The AFS approach (single invocation, local pipeline):**

- Agent runs `python afs.py process ~/Downloads --json`. One command.
- 3,000 vision model calls + 100 reasoning model calls happen on local Ollama models. Zero agent tokens.
- The `.afs-manifest.json` is a persistent artifact on disk -- another agent, another session, another day can pick it up and inspect results. It does not disappear when the conversation ends.
- AFS prevents OS sleep during long runs. True hands-off: fire and forget.
- When it finishes, the agent reads the manifest, inspects the results, adjusts config if needed, and re-runs. Total agent cost: a handful of tool calls instead of thousands.

**The sweet spot:** AFS stays standalone for bulk processing. The agent invokes it, monitors the manifest, and makes high-level decisions. Local models do the heavy lifting. The CLI agent stays lean.

This is token economics. The insight is simple: procedural work (analyze 3,000 images, consolidate 50 folders, assign files to topics) belongs on local models. Strategic work (decide which model to use, evaluate quality, re-run on failures) belongs on the agent.

---

## Part of the Agentic Power-Tools Series

AFS is one of a series of lean local tools designed for CLI agent orchestration. The philosophy: the agent is the manager, not the worker. Each tool offloads a specific category of procedural work to local models or well-designed scripts, returning structured results the agent can act on.

**Tools in the series:**

| Tool | Purpose | Repo |
|------|---------|------|
| **AFS** | File naming and organization via local vision + reasoning models | This repo |
| **Agentic Project Tracker** | Project and task management | [agentic-project-tracker](https://github.com/albertdobmeyer/agentic-project-tracker) |

Same design principles across the series: single-invocation pipelines, JSON-first output, manifest-based handoff, zero cloud token overhead for procedural work.

---

## How It Works

**Three steps, two models, clean manifest handoff:**

### Step 1 -- Naming (vision model, per-file)

Each image or video is analyzed by a local vision model to produce a content-derived semantic name. `IMG_8847.jpg` becomes `orange-cat-sleeping.jpg`. Character recognition catches SpongeBob, Pepe, Wojak, and [100+ known characters](afs/analyze.py). Camera photos are auto-detected by EXIF data, resolution, and filename patterns -- CDR is skipped to preserve original quality and metadata.

### Step 2a -- Folder Consolidation (reasoning model, one call)

After naming, the reasoning model evaluates all existing folder names holistically and produces a merge map. `pol` becomes `politics`, `sci` becomes `science`. Junk folders named after file extensions (like `jpg/`) are flagged, and their files are re-sorted through Step 1. This is one cheap reasoning call (~600 tokens) that prevents folder sprawl across multiple runs.

### Step 2b -- File Assignment (reasoning model, chunked)

All named files are presented to the reasoning model alongside the consolidated folder list. It assigns every file to a topic folder in a coherent pass. Large batches are chunked automatically (default: 30 files per chunk). Custom folders and folder aliases are resolved during assignment.

### Processing Tiers

Security comes from never opening untrusted files directly. Three tiers determine what happens to each file:

| Tier | File Types | What Happens |
|------|-----------|--------------|
| **Tier 1** | Images (JPG, PNG, WebP, BMP, TIFF, static GIF) | Re-rendered via PIL (CDR) -- strips metadata and embedded payloads. Camera photos auto-detected and skipped. Analyzed by vision model. |
| **Tier 2** | Video (MP4, WebM, MOV, AVI, MKV) + animated GIF | Representative frame extracted for analysis. Original kept byte-for-byte identical. |
| **Tier 3** | Everything else (PDF, Office, audio, archives, code) | No analysis, no opening. Sorted into `filtered/{extension}/` by type. |

CDR (Content Disarm and Reconstruction) is a side effect of the method: PIL decodes pixel data and writes a new file from scratch, discarding non-pixel data as a byproduct. AFS is not anti-malware software -- it simply avoids the attack surface of opening untrusted files.

---

## Quick Start

```bash
git clone https://github.com/albertdobmeyer/agentic-file-sorter.git
cd agentic-file-sorter

# Option A: Setup script (checks prereqs, installs deps, verifies Ollama)
bash setup.sh

# Option B: Manual
pip install -r requirements.txt
ollama pull llava:latest    # vision model
ollama pull qwen3:8b        # reasoning model

# Option C: Install as CLI tool (adds 'afs' command to PATH)
pip install .

# Verify everything works
python afs.py status        # or just: afs status

# Preview what would happen (no files moved)
python afs.py process ~/Downloads --dry-run

# Sort for real
python afs.py process ~/Downloads
```

The setup script checks Python version, installs pip dependencies, verifies Ollama connectivity, and confirms models are pulled. It reports issues clearly and never modifies system configuration.

---

## Face Recognition

AFS can identify known people in camera photos using the same vision model -- no additional dependencies required.

### Setup

Create a `faces/` directory in the project root with one sample image per person:

```
faces/
  albert/
    albert-1.jpg
    albert-2.jpg
  tori/
    tori-1.png
```

Each subdirectory name becomes the person's identifier. Multiple samples per person improve accuracy.

### How It Works

When processing camera photos (auto-detected via EXIF, resolution, and filename patterns), AFS sends the photo alongside all reference face images to the vision model using Ollama's multi-image API. The model identifies which named people appear and injects their names into the filename:

```
IMG_20250312_073.jpg  ->  albert-tori-park-20250312-073.jpg
```

Face identification only runs on detected photos when face samples exist. It uses the same vision model and Ollama endpoint -- zero new dependencies, zero new configuration.

**Privacy note:** Face samples stay local -- they are never uploaded anywhere. The `faces/` directory is gitignored by default. Do not commit personal face images to a public repository.

### Re-running Face ID

If you add new face samples after an initial run, use `--reface` to re-identify faces without re-analyzing every file:

```bash
python afs.py process ~/Photos --reface
```

This reuses the existing manifest keywords and only re-runs the face identification step -- much cheaper than a full reprocess.

---

## Customization

Configuration follows a layered priority system. Higher layers override lower ones:

```
CLI flags  >  environment variables  >  .env  >  afs-config.json
```

### afs-config.json (primary)

The main configuration file. Agent-friendly JSON with all settings and sensible defaults. Only change what you need:

```json
{
  "models": {
    "vision_model": "llava:13b",
    "text_model": "qwen3:30b-a3b"
  },
  "processing": {
    "skip_cdr_photos": true,
    "photo_threshold_mp": 4.0,
    "chunk_size": 30
  },
  "sorting": {
    "max_topics": 25,
    "photo_sorting": "flat",
    "custom_folders": {"biz": ["business", "stock", "finance"]},
    "folder_aliases": {"pol": "politics", "sci": "science"}
  }
}
```

### Key Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `models.vision_model` | `llava:latest` | Any Ollama vision model. Larger models = better naming. |
| `models.text_model` | `qwen3:8b` | Any Ollama text/reasoning model for folder operations. |
| `processing.photo_threshold_mp` | `4.0` | Megapixel threshold for camera photo detection. Memes are typically <2MP, phones start at 8MP. |
| `processing.skip_cdr_photos` | `true` | Skip CDR re-rendering for detected camera photos (preserves EXIF and original quality). |
| `sorting.max_topics` | `25` | Maximum number of topic folders the reasoning model can create. |
| `sorting.photo_sorting` | `flat` | How photos are organized: `flat` keeps them in one folder, alternatives group by date or event. |
| `sorting.custom_folders` | `{}` | Define folder names with keyword triggers. Files matching any keyword are routed to that folder. |
| `sorting.folder_aliases` | `{}` | Map shorthand folder names to canonical names (applied during consolidation). |

### .env (simple overrides)

For quick overrides without editing JSON:

```env
VISION_MODEL=llava:13b
TEXT_MODEL=qwen3:30b-a3b
SANITIZE_IMAGES=false
OLLAMA_URL=http://192.168.1.100:11434
```

### Flatten (undo sorting)

To undo folder sorting and move all files back to the root directory:

```bash
python afs.py flatten ~/Downloads             # move all files to root
python afs.py flatten ~/Downloads --dry-run   # preview first
```

This is useful for re-sorting with different settings or a different model.

---

## Agent Integration

AFS is designed for programmatic invocation. The `--json` flag emits NDJSON events on stdout, and the `.afs-manifest.json` is the persistent handoff artifact.

### NDJSON Event Stream

```bash
python afs.py process ~/Downloads --json
```

One JSON object per line. Key events:

| Event | When | Key Fields |
|-------|------|------------|
| `start` | Run begins | `total`, `skipped`, `input`, `output` |
| `progress` | After each file (Step 1) | `index`, `total`, `file`, `status`, `confidence` |
| `step2a-done` | Consolidation complete | `merges`, `folders_eliminated`, `resort_files` |
| `step2-done` | Sorting complete | `assignments`, `folders_created` |
| `done` | Run complete | `total`, `moved`, `errors`, `filtered`, `ms` |

### Manifest

The `.afs-manifest.json` is written after every file (crash-safe checkpointing). It contains the full run metadata, per-file results, and folder structure:

```json
{
  "run": { "step": "complete", "elapsed_ms": 42000 },
  "stats": { "total": 150, "named": 142, "sorted": 142, "errors": 0, "photos_detected": 12 },
  "files": [
    { "source": "IMG_8847.jpg", "name": "orange-cat-sleeping.jpg",
      "keywords": ["orange", "cat", "sleeping"], "folder": "animals",
      "confidence": 0.92, "tier": 1, "status": "moved", "photo_detected": true }
  ]
}
```

An agent can read the manifest mid-run to check progress (`run.step`, `run.progress`), after completion to evaluate results (`stats.errors`, `stats.avg_confidence`), or days later to understand what was done.

See [CLAUDE.md](CLAUDE.md) for the complete event schema, manifest reference, monitoring guide, exit codes, error types, and processing strategies.

---

## CLI Reference

```bash
# Status and diagnostics
python afs.py status                              # Check Ollama, models, ffmpeg, config
python afs.py status --json                       # JSON output for agent gating
python afs.py --version                           # Print version

# Processing
python afs.py process <dir>                       # Sort files (CDR on, photo detection on)
python afs.py process <dir> --dry-run             # Preview without moving files
python afs.py process <dir> -o <out>              # Separate output directory
python afs.py process <dir> --force               # Ignore prior manifest, reprocess all
python afs.py process <dir> --max-files 20        # Spot-check first 20 files
python afs.py process <dir> --no-sanitize         # Skip CDR re-rendering entirely
python afs.py process <dir> --no-convert-webp     # Keep WebP as-is during CDR
python afs.py process <dir> --reface              # Re-run face ID only (cheap)
python afs.py process <dir> --config /path/to.json # Alternate config file
python afs.py process <dir> --json                # NDJSON mode for agents

# Flatten (undo sorting)
python afs.py flatten <dir>                       # Move all files from subfolders to root
python afs.py flatten <dir> --dry-run             # Preview flatten operation
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Bad input (missing directory, invalid arguments) |
| 2 | Ollama unreachable |
| 3 | All files failed or Step 2 total failure |

---

## Architecture

```
agentic-file-sorter/
├── afs.py                   Entry point (thin shim -> afs.cli.main)
├── afs/                     Source package
│   ├── cli.py               CLI parsing, event formatting, exit codes
│   ├── config.py            Central config loader (json > env > defaults)
│   ├── pipeline.py          Three-step orchestrator, manifest I/O, resort-awareness
│   ├── consolidate.py       Step 2a: folder consolidation, merge map, physical merges
│   ├── batch_sort.py        Step 2b: reasoning model prompt building, chunked assignment
│   ├── analyze.py           Step 1: Ollama vision API, character identification, error classification
│   ├── photo.py             Camera photo detection (EXIF, resolution, filename patterns)
│   ├── faces.py             Face recognition via sample images (multi-image Ollama API)
│   ├── preview.py           CDR re-rendering (Pillow) + video frame extraction (ffmpeg)
│   ├── naming.py            Keywords -> kebab-case semantic filenames
│   ├── sorting.py           Topic normalization, folder routing, file moves, flatten
│   └── types_.py            FileResult/BatchResult dataclasses, tier classification, extension sets
├── faces/                   Face sample images (optional)
├── tests/                   Test suite + fixtures
├── afs-config.json          Configuration (only non-default values needed)
├── .env                     Fallback configuration (simple key=value overrides)
├── setup.sh                 One-command setup (checks prereqs, installs deps)
├── requirements.txt         Pillow + requests
├── CONSTITUTION.md          Design axioms and decision rationale
├── CLAUDE.md                Agent operational manual (event schema, manifest ref, strategies)
└── README.md                This file
```

---

## Prerequisites

| Requirement | Purpose | Install |
|------------|---------|---------|
| Python 3.10+ | Runtime | [python.org](https://python.org) |
| Ollama | Local LLM inference | [ollama.com](https://ollama.com) |
| Vision model | File analysis (Step 1) | `ollama pull llava:latest` |
| Reasoning model | Folder operations (Step 2) | `ollama pull qwen3:8b` |
| Pillow + requests | Image processing, HTTP | `pip install -r requirements.txt` |
| ffmpeg | Video frame extraction | Optional -- only needed for video files |

Any Ollama vision model works for Step 1 (llava, llava:13b, bakllava, etc.). Any text/reasoning model works for Step 2 (qwen3, mistral, llama3, etc.). Pick models that fit your GPU.

---

## Design Principles

AFS is governed by five axioms defined in [CONSTITUTION.md](CONSTITUTION.md):

1. **AGENTIC** -- JSON-first, stateless, designed to be invoked by coding agents. The manifest is the handoff artifact. Human-readable output is a thin wrapper over the JSON interface, never the other way around.

2. **LITE** -- Two pip dependencies (Pillow, requests). No database, no cache, no ORM, no web UI, no framework. Every addition must clear the bar: "Does this directly improve naming or sorting?"

3. **SEMANTIC** -- The product is the name. 2-5 word kebab-case filenames derived from what the vision model sees. Folders named after topics, not file types. Plural, kebab-case, max 2 words.

4. **SECURE** -- Files are untrusted. Tier 1 images are re-rendered via CDR. Tier 2 files are analyzed via extracted frames. Tier 3 files are never opened. Camera photos are auto-detected and CDR-skipped to preserve quality.

5. **VISUAL-ONLY** -- If a file cannot produce a meaningful image for analysis, it goes to `filtered/`. The vision model is the only brain for content analysis. The reasoning model only sees filenames and keywords, never file contents.

---

## License

MIT
