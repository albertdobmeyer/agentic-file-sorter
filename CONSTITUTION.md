# AFS — Constitution

**Agentic File Sorter: Secure Semantic Naming for Downloaded Media**

Version: 1.1 | Date: 2026-03-18 | Author: Albert K. | AKD Automation Solutions

Status: RATIFIED — All implementation decisions must trace back to this document.

## Preamble

The original SecureSemanticSorter grew to 173 files and 6 packages before collapsing under its own weight. AFS is the rebuild — stripped to its core purpose: take chaotic downloaded files (primarily memes and media), give them descriptive semantic names, and sort them into topic folders.

Every feature, every line of code, every dependency must justify itself against this document.

## The 5 Axioms

### Axiom 1: AGENTIC

AFS is a power tool for Claude Code within the Agentic 5-Drive System (A5DS) — a local orchestration suite where Claude Code drives Ollama models, scripts, and workflows. It is one of many custom tools built to save Claude Code tokens and time by offloading procedural work to local models and well-designed scripts.

- CLAUDE.md is the entry point and operational manual
- Primary interface is structured JSON on stdout (NDJSON events during processing, manifest after completion)
- Human-readable output is a thin wrapper over the JSON interface, never the other way around
- Designed to be invoked by Claude Code, results evaluated by reading the manifest
- Stateless and on-demand: run once, produce results, exit. No daemon, no watcher, no memory between runs
- The `.afs-manifest.json` is output, not state — but subsequent runs may read it to avoid redundant work. This is opportunistic, not required: if no manifest exists, all files are processed from scratch

### Axiom 2: LITE

The leanest version that delivers value. Every addition must clear the bar: "Does this directly improve naming or sorting?"

- Runtime dependencies: Pillow, requests. System tools: ffmpeg, Ollama
- No database. No cache. No ORM. No web UI. No interactive prompts
- The `.afs-manifest.json` is the only persistent output besides the sorted files themselves
- The manifest serves triple duty: model handoff between Step 1 and Step 2, structured feedback for agentic review, and resort-awareness signal for subsequent runs. No additional state files, databases, or caches.
- No features justified by "we might need this later" — YAGNI is law
- Adding a pip dependency requires a written justification that traces back to one of the 5 axioms

### Axiom 3: SEMANTIC

The product is the name. The entire pipeline — CDR re-rendering, frame extraction, vision analysis — exists solely to produce the best possible filename and determine the right folder.

**Filenames:**
- 2-5 words, kebab-case, derived from vision model keywords
- Describe content, not format: `smug-pepe-sunglasses.png` not `downloaded-image-3.png`
- Original filename is fed to the vision model as a hint, not used as the primary source
- Collisions resolved with numeric suffix: `name.ext`, `name-2.ext`, `name-3.ext`

**Folders:**
- Named after topics (what the content is about), not file types
- Plural, kebab-case, max 2 words: `reaction-memes/`, `cat-photos/`, `science-fiction/`
- `normalize_topic()` pre-processes keywords before the reasoning model prompt, mapping obvious synonyms (animal→animals, gaming→games) to reduce model burden and improve consistency
- The reasoning model sees all named files simultaneously during Step 2 and produces a coherent folder structure in one pass. There is no separate consolidation phase — folder creation, matching, and merging happen in a single batch-aware sorting step.
- `filtered/` is the catch-all destination for any file not successfully named and sorted — unsupported types (routed by extension into `filtered/{extension}/`), processing failures (`filtered/errors/`), or any other condition that prevents a file from completing the pipeline. The system does not diagnose or retry — it routes to `filtered/` and records the outcome in the manifest. The user decides what to do with filtered files. `filtered/` is never merged, renamed, or reorganized by any automated pass.

**Content analysis is transient.** Nothing about the file's content is stored or cataloged. The raw vision model response is used to extract keywords and generate a name, then discarded. The manifest retains the keywords and name for Step 2, not the analysis itself.

### Axiom 4: SECURE

Files entering AFS are untrusted — downloaded from the internet. The app handles them indirectly, never opening them in external applications, never executing their content.

**The method — Content Disarm and Reconstruction (CDR):**

The app uses three tiers based on what can be safely done with the file:

**Tier 1 — Re-renderable images** (JPEG, PNG, BMP, WebP, TIFF, static single-frame GIF):
PIL decodes the pixel data and writes a new file from scratch. The re-rendered file replaces the original. Non-pixel data (EXIF metadata, embedded payloads, steganographic content, polyglot tricks) is stripped as a side effect — PIL reconstructs only pixels. This is CDR, not "screenshotting." CDR is configurable: when `SANITIZE_IMAGES` is set to `false`, Tier 1 files are treated like Tier 2 (kept as-is). WebP files can be converted to JPG during re-rendering when `CONVERT_WEBP` is enabled.

**Tier 2 — Irreplaceable visual files** (animated GIF, MP4, WebM, MOV, AVI, MKV):
A representative frame is extracted as a temporary image for vision analysis. The original file is kept as-is — renamed and moved, bytes untouched. The temp frame is deleted after analysis. These files cannot be CDR'd without destroying their value (animation frames, video data).

**Tier 3 — Everything else** (PDF, Office docs, audio, archives, code, binary, unknown):
No analysis. No opening. No content inspection. Sorted directly into `filtered/{extension}/` by file extension. This is the safe default for anything the app doesn't understand visually.

**What "secure" means here:**
CDR is a property of the method, not a marketed feature. AFS is not anti-malware software. It doesn't scan for viruses, detect threats, or score risk. It simply avoids the attack surface of opening untrusted files by working with pixel data and opaque byte moves.

### Axiom 5: VISUAL-ONLY

The vision model is the primary brain for file analysis. If a file can't produce a meaningful image for analysis, it doesn't get analyzed — it goes to `filtered/`.

- Tier 1 images: analyzed after CDR re-render (or directly if `SANITIZE_IMAGES=false`)
- Tier 2 files: analyzed via extracted representative frame
- Tier 3 files: not analyzed — routed to filtered/ by extension
- The vision model receives one image per file and returns keywords + confidence. It does NOT determine topics or folder assignments — that is the reasoning model's job
- If confidence is low or the result is generic, a follow-up character identification query may be sent to the same vision model to improve naming quality
- After all files are named, a reasoning model receives the complete manifest — all filenames, keywords, and metadata at once — and assigns every file to a topic folder. It sees the full picture and produces a coherent folder structure in one pass
- These are two sequential steps with a clean manifest handoff, not interleaved model calls

## The Decision Tree

This is the method. It implements the axioms but can be refined independently.

```
STEP 1 — NAMING (vision model, per-file)

File arrives
│
├─ Already sorted? (manifest check — see Resort-Awareness below)
│   └─ Yes → SKIP — file is already named and placed
│
├─ Unsupported type (Tier 3)?
│   └─ Move to filtered/{extension}/ — no analysis, no naming
│
├─ Re-renderable image (Tier 1)?
│   ├─ SANITIZE_IMAGES=true → Re-render via PIL (CDR) → replace original
│   └─ SANITIZE_IMAGES=false → Keep original as-is
│   └─ → Vision model → keywords + confidence → semantic name
│
├─ Animated GIF (Tier 2)?
│   └─ Extract representative frame (temp) → Vision model
│   └─ → keywords + confidence → semantic name → keep original
│
└─ Video (Tier 2)?
    └─ Extract frame via ffmpeg (temp) → Vision model
    └─ → keywords + confidence → semantic name → keep original

Output: Manifest updated with each file's semantic name + keywords

STEP 2 — SORTING (reasoning model, entire batch)

Input: Complete manifest (all semantic names + keywords)
│
└─ Reasoning model receives all file names at once
    ├─ Creates topic folders as needed
    ├─ Assigns each named file to a folder
    ├─ Merges synonymous groupings (implicit consolidation)
    └─ On re-runs: also receives existing folder structure from prior manifest

Execute: Move/rename all files per the manifest assignments
Output: Final manifest (file→name→folder mappings, errors, run metadata)
```

## Resort-Awareness

AFS may be run on directories containing previously sorted files alongside
new additions. To avoid redundant processing:

A file is considered "already sorted" when ALL of the following are true:
1. A .afs-manifest.json exists in the output directory
2. The file's path appears in the manifest
3. The file has not been modified since the manifest's run timestamp (mtime check)

Files that fail any criterion proceed through the full pipeline.
The manifest is the sole authority — no filename pattern matching,
no heuristic guessing.

In Step 2, the reasoning model receives both new file names AND the
existing folder structure from the prior manifest. This gives it full
context to place new files alongside existing ones without reorganizing
what's already there.

## Output Structure

```
{output_dir}/
├── {topic}/                    # Semantically named topic folders
│   ├── smug-pepe-sunglasses.png
│   └── surprised-pikachu-face.jpg
├── filtered/                   # Files not successfully named and sorted
│   ├── pdf/
│   ├── exe/
│   └── {extension}/
└── .afs-manifest.json          # Structured run report (dotfile — not processed on re-runs)
```

## Configuration

```env
# .env
SANITIZE_IMAGES=true        # Re-render Tier 1 images via CDR (default: true)
CONVERT_WEBP=true           # Convert WebP → JPG during CDR (default: true, requires SANITIZE_IMAGES=true)
OLLAMA_URL=http://localhost:11434
VISION_MODEL=llava:latest   # Vision model for file analysis
TEXT_MODEL=qwen3:8b          # Reasoning model for folder operations only
```

Settings exist because they serve the axioms. No arbitrary caps on how many.

## Dependencies

**pip install:** Pillow, requests
**System:** ffmpeg (frame extraction), Ollama (local LLM serving)

That's it. No opencv. No pytesseract. No pydantic. No click. No rich.

## Error Handling

- Any failure during processing — vision model timeout, PIL decode error, ffmpeg extraction failure, or unhandled exception — routes the file to `filtered/errors/` with the error recorded in the manifest. No retry, no diagnosis.
- Ollama not running → exit with structured error, don't process anything

## What This App Is NOT

- **Not anti-malware.** CDR is a side effect of the method, not a feature.
- **Not a content moderation system.** No toxicity scoring, no NSFW detection.
- **Not a database application.** No SQLite, no schemas, no migrations.
- **Not a file deduplication tool.** No hash databases, no similarity matching.
- **Not a DAM.** No tagging, no search, no browsing, no catalog.
- **Not a format converter.** WebP→JPG is a side effect of CDR, not a feature.
- **Not a multi-model orchestrator.** Two models, two sequential steps. The vision model names files (Step 1). The reasoning model organizes them into folders (Step 2). No orchestration graph, no model routing, no chaining.

## Amendment Process

This constitution can be amended. Amendments must:
1. Be written, not verbal
2. State which axiom is being modified and why
3. Demonstrate the change doesn't violate other axioms
4. Be appended with a date and rationale

## Amendment Log

**All amendments ratified 2026-03-18 (v1.0 → v1.1)**

1. **Amendment 1: Redefine `filtered/`** (Axiom 3) — `filtered/` is the catch-all for any file not successfully named and sorted, not just unsupported types. Includes `filtered/errors/` for processing failures.

2. **Amendment 2: Two-step pipeline — name-then-sort** (Axioms 3, 5, Decision Tree, NOT section) — Vision model names files (Step 1), reasoning model sorts them into folders (Step 2). Clean manifest handoff between steps. No interleaved model calls.

3. **Amendment 3: Resort-awareness** (Axioms 1, 2, new section) — Subsequent runs read the prior manifest to skip already-sorted files (path + mtime check). The reasoning model receives existing folder structure to place new files alongside existing ones.

4. **Amendment 4: Retire consolidation** (consequence of Amendment 2) — No separate consolidation phase. The reasoning model handles folder creation, matching, and merging in a single batch-aware sorting step.

**Ratified 2026-03-21 (v1.1 → v1.2)**

5. **Amendment 5: Reinstate folder consolidation as Step 2a** (Axiom 3 SEMANTIC, supersedes Amendment 4) — When prior folders exist, a dedicated consolidation pass evaluates all folder names holistically and produces a merge map before file assignment. Amendment 4's assumption that implicit consolidation during file assignment was sufficient proved wrong — the reasoning model cannot consolidate folders it did not create. Step 2a is one cheap reasoning model call (~600 tokens) that sees all folder names at once and decides what to merge. Pipeline becomes: Step 1 (naming) → Step 2a (folder consolidation) → Step 2b (file assignment). Junk folders named after file extensions are flagged for re-sorting through Step 1.

**Ratified 2026-03-22 (v1.2 → v1.3)**

6. **Amendment 6: Photo-aware CDR** (Axiom 4 SECURE) — CDR is skipped for detected camera photos. Detection uses three signals: EXIF camera metadata (Make/Model tags), resolution threshold (default 4MP — memes are <2MP, phones start at 8MP), and camera filename patterns (IMG_, DSC_, PXL_, etc.). Photos are treated as Tier 2 for CDR purposes: analyzed via preview but original bytes and EXIF metadata preserved. Configurable via `skip_cdr_photos` (default: true). This prevents destructive re-rendering of high-value camera photos while maintaining CDR security for downloaded memes.

7. **Amendment 7: Face identification via sample images** (Axiom 5 VISUAL-ONLY) — Optional face matching using the vision model's multi-image capability. Users provide face sample images in a `faces/` directory (e.g., `faces/albert.jpg`). During Step 1, detected photos are sent to the vision model alongside all reference face images. The model identifies which named people appear and injects their names into the keywords for semantic naming. Zero new dependencies — leverages Ollama's existing multi-image API. Only runs on detected photos when face samples exist.

## Lineage

Supersedes all previous architecture documents for SecureSemanticSorter. Those remain as historical reference but do not govern implementation. AFS starts clean.
