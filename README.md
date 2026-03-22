# AFS — Agentic File Sorter

**A power tool for your CLI agent.** Point it at a folder of chaotic files, and your local vision model names them by content while a reasoning model organizes them into topic folders. Fully local, fully automatic, JSON-first.

> **Using Claude Code?** Point it at this repo. It reads [CLAUDE.md](CLAUDE.md) automatically and knows how to set up, run, and monitor AFS.

```
Downloads/                          →    Downloads/
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

## Why This Exists

AFS is built to be invoked by coding agents like [Claude Code](https://docs.anthropic.com/en/docs/claude-code). It offloads the tedious work of file organization to local Ollama models so your agent doesn't burn tokens doing it. The structured JSON manifest lets the agent inspect results, make decisions, and re-run if needed — without parsing human-readable output.

It works great as a standalone CLI tool too. But the design is agent-first: stateless, on-demand, JSON on stdout, manifest as the handoff artifact.

## How It Works

**Three steps, two models, clean handoff:**

**Step 1 — Naming** (vision model, per-file): Each image or video gets a content-derived semantic name. A photo of a cat becomes `orange-cat-sleeping.jpg`, not `IMG_8847.jpg`. Character recognition catches SpongeBob, Pepe, Wojak, and [100+ known characters](afs/analyze.py). Camera photos are auto-detected and CDR-skipped to preserve original quality.

**Step 2a — Folder Consolidation** (reasoning model, one call): After naming, the reasoning model evaluates all existing folder names holistically and merges redundant ones. `pol` becomes `politics`, `sci` becomes `science`. Junk folders named after file extensions (like `jpg/`) are flagged for re-sorting.

**Step 2b — Sorting** (reasoning model, chunked): All named files are presented to the reasoning model along with the consolidated folder list. It assigns every file to a topic folder in a coherent pass. Large batches are chunked automatically.

**Three processing tiers keep it safe:**

| Tier | File Types | What Happens |
|------|-----------|--------------|
| 1 | Images (JPG, PNG, WebP, ...) | Re-rendered via PIL (CDR) — strips metadata and embedded payloads. Camera photos auto-detected and skipped. Analyzed by vision model. |
| 2 | Video + animated GIF | Frame extracted for analysis. Original kept as-is, bytes untouched. |
| 3 | Everything else | No analysis, no opening. Sorted into `filtered/{extension}/` by type. |

## Quick Start

```bash
git clone https://github.com/A5DS-HQ/agentic-file-sorter.git
cd agentic-file-sorter

# Option A: Setup script (checks prereqs, installs deps, verifies Ollama)
bash setup.sh

# Option B: Manual
pip install -r requirements.txt
ollama pull llava:latest    # vision model
ollama pull qwen3:8b        # reasoning model

# Verify everything works
python afs.py status

# Preview what would happen (no files moved)
python afs.py process ~/Downloads --dry-run

# Sort for real
python afs.py process ~/Downloads
```

## Agent Integration

AFS is designed to be called by coding agents. If you are an agent, read [CLAUDE.md](CLAUDE.md) — it is your complete operational manual with event stream schema, manifest reference, monitoring guide, and troubleshooting.

The `--json` flag emits NDJSON events on stdout. The `.afs-manifest.json` in the output directory is the structured result artifact — readable mid-run for progress monitoring.

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

## Face Recognition (Optional)

Place face sample images in the `faces/` directory, named after the person:

```
faces/
  albert.jpg
  tori.png
```

When processing camera photos, AFS sends the photo + all reference faces to the vision model and identifies who appears. Results are injected into the filename:

`IMG_20250312_073.jpg` → `albert-tori-park-20250312-073.jpg`

No additional dependencies required — uses the same Ollama vision model with multi-image API.

## Configuration

Configuration priority: `afs-config.json` < `.env` < environment variables < CLI flags.

```json
{
  "models": {
    "vision_model": "llava:13b",
    "text_model": "qwen3:30b-a3b"
  },
  "processing": {
    "skip_cdr_photos": true,
    "photo_threshold_mp": 4.0
  },
  "sorting": {
    "custom_folders": {"biz": ["business", "stock", "finance"]},
    "folder_aliases": {"pol": "politics"}
  }
}
```

See [CLAUDE.md](CLAUDE.md) for the full configuration reference.

## CLI Reference

```bash
python afs.py status                              # Check Ollama + config
python afs.py process <dir>                       # Sort files (CDR on, photo detection on)
python afs.py process <dir> --dry-run             # Preview without moving
python afs.py process <dir> --no-sanitize         # Skip CDR re-rendering entirely
python afs.py process <dir> -o <out>              # Separate output directory
python afs.py process <dir> --force               # Ignore prior manifest, reprocess all
python afs.py process <dir> --max-files 20        # Spot-check first 20 files
python afs.py --json process <dir>                # JSON mode for agents
```

## Architecture

```
afs.py                   Entry point
afs/                     Source package
├── cli.py               CLI and event formatting
├── pipeline.py          Three-step orchestrator, manifest, resort-awareness
├── consolidate.py       Step 2a: folder consolidation, merge execution
├── batch_sort.py        Step 2b: reasoning model prompt, chunking
├── analyze.py           Step 1: vision model API, character identification
├── photo.py             Photo detection (EXIF, resolution, filename)
├── faces.py             Face recognition via sample images
├── preview.py           CDR re-rendering + frame extraction
├── naming.py            Keywords → kebab-case semantic filenames
├── sorting.py           Topic normalization, folder routing
└── types_.py            Tier classification, shared data types
faces/                   Face sample images (optional)
tests/                   Test suite + fixtures
```

## Prerequisites

| Requirement | Purpose | Install |
|------------|---------|---------|
| Python 3.10+ | Runtime | [python.org](https://python.org) |
| Ollama | Local LLM inference | [ollama.ai](https://ollama.ai) |
| `llava:latest` | Vision model (Step 1) | `ollama pull llava:latest` |
| `qwen3:8b` | Reasoning model (Step 2) | `ollama pull qwen3:8b` |
| Pillow + requests | Image processing, API | `pip install -r requirements.txt` |
| ffmpeg | Video frame extraction | Optional — only needed for video files |

Any Ollama vision model works for Step 1. Any text/reasoning model works for Step 2.

## Design Principles

See [CONSTITUTION.md](CONSTITUTION.md) for the full ratified document.

1. **AGENTIC** — JSON-first, stateless, designed to be invoked by coding agents
2. **LITE** — Two pip dependencies (Pillow + requests). No database, no cache, no framework
3. **SEMANTIC** — The product is the name. 2-5 word kebab-case filenames derived from content
4. **SECURE** — CDR strips metadata from memes. Camera photos auto-detected and preserved. Unknown types never opened
5. **VISUAL-ONLY** — If it can't produce a meaningful image for analysis, it goes to `filtered/`

## License

MIT
