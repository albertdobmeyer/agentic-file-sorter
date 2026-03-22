# AFS — Agentic File Sorter

**A power tool for your CLI agent.** Point it at a folder of chaotic files, and your local vision model names them by content while a reasoning model organizes them into topic folders. Fully local, fully automatic, JSON-first.

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

**Two steps, two models, clean handoff:**

**Step 1 — Naming** (vision model, per-file): Each image or video gets a content-derived semantic name. A photo of a cat becomes `orange-cat-sleeping.jpg`, not `IMG_8847.jpg`. Character recognition catches SpongeBob, Pepe, Wojak, and [100+ known characters](afs/analyze.py).

**Step 2 — Sorting** (reasoning model, entire batch): All named files are presented to a reasoning model at once. It sees the full picture and assigns every file to a topic folder — `animals/`, `memes/`, `science/` — in a single coherent pass. No file-by-file guessing.

**Three processing tiers keep it safe:**

| Tier | File Types | What Happens |
|------|-----------|--------------|
| 1 | Images (JPG, PNG, WebP, ...) | Re-rendered via PIL (CDR) — strips metadata and embedded payloads. Analyzed by vision model. |
| 2 | Video + animated GIF | Frame extracted for analysis. Original kept as-is, bytes untouched. |
| 3 | Everything else | No analysis, no opening. Sorted into `filtered/{extension}/` by type. |

## Quick Start

```bash
# Prerequisites
pip install Pillow requests
# Ollama running locally with models pulled:
ollama pull llava:latest    # vision model
ollama pull qwen3:8b        # reasoning model
# ffmpeg on PATH (for video frame extraction)

# Check everything is wired up
python afs.py status

# Preview what would happen (no files moved)
python afs.py process ~/Downloads --dry-run

# Sort for real
python afs.py process ~/Downloads
```

## Agent Integration

AFS is designed to be called by your coding agent. The `--json` flag emits NDJSON events on stdout for real-time progress, and the `.afs-manifest.json` in the output directory is the structured result your agent reads after the run.

```bash
# JSON mode — one event per line, machine-readable
python afs.py --json process ~/Downloads
```

The manifest tracks every file: its original name, semantic name, keywords, assigned folder, confidence score, processing tier, and any errors. Your agent can inspect it, decide if the results are good enough, adjust settings, and re-run. Already-sorted files are automatically skipped on re-runs (manifest + mtime check).

```json
{
  "run": { "step": "complete", "elapsed_ms": 42000 },
  "stats": { "total": 150, "named": 142, "sorted": 142, "errors": 0 },
  "files": [
    { "source": "IMG_8847.jpg", "name": "orange-cat-sleeping.jpg",
      "keywords": ["orange", "cat", "sleeping"], "folder": "animals",
      "confidence": 0.92, "tier": 1, "status": "moved" }
  ]
}
```

## Configuration

Edit `.env` in the project root:

```env
SANITIZE_IMAGES=true        # CDR on/off — re-render images to strip metadata (default: true)
CONVERT_WEBP=true           # Convert WebP → JPG during CDR (default: true)
OLLAMA_URL=http://localhost:11434
VISION_MODEL=llava:latest   # Any Ollama vision model
TEXT_MODEL=qwen3:8b          # Any Ollama text/reasoning model
```

CLI flags `--no-sanitize` and `--no-convert-webp` override `.env`.

## CLI Reference

```bash
python afs.py status                              # Check Ollama + config
python afs.py process <dir>                       # Sort files (CDR on)
python afs.py process <dir> --dry-run             # Preview without moving
python afs.py process <dir> --no-sanitize         # Skip CDR re-rendering
python afs.py process <dir> -o <out>              # Separate output directory
python afs.py --json process <dir>                # JSON mode for agents
```

## Architecture

```
afs.py              Entry point
afs/                     Source package
├── cli.py               CLI and event formatting
├── pipeline.py          Two-step orchestrator, manifest, resort-awareness
├── batch_sort.py        Step 2: reasoning model prompt, response parsing, chunking
├── analyze.py           Step 1: vision model API, character identification
├── preview.py           CDR re-rendering + frame extraction
├── naming.py            Keywords → kebab-case semantic filenames
├── sorting.py           Topic normalization, folder routing
└── types_.py            Tier classification, shared data types
tests/                   Test suite (57 tests) + fixtures
```

## Design Principles

See [CONSTITUTION.md](CONSTITUTION.md) for the full ratified document.

1. **AGENTIC** — JSON-first, stateless, designed to be invoked by coding agents
2. **LITE** — Two pip dependencies (Pillow + requests). No database, no cache, no framework (legacy name; axiom still applies)
3. **SEMANTIC** — The product is the name. 2-5 word kebab-case filenames derived from content
4. **SECURE** — CDR strips metadata from images. Videos analyzed via extracted frames. Unknown types never opened
5. **VISUAL-ONLY** — If it can't produce a meaningful image for analysis, it goes to `filtered/`

## Requirements

- Python 3.10+
- [Ollama](https://ollama.ai) running locally
- `llava:latest` and `qwen3:8b` (or any vision + reasoning model pair)
- `pip install Pillow requests`
- ffmpeg on PATH

## License

MIT
