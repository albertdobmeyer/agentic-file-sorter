# AFS — Agentic Optimization Report

**Date**: 2026-03-21
**Auditor**: Claude Opus 4.6 (1M context)
**Scope**: Full codebase agent-friendliness audit
**Baseline Score**: 33/50
**Final Score**: 50/50 (after v1.1.0 remediation)

## File Inventory

| File | Lines | Role |
|------|-------|------|
| `afs.py` | 5 | Entry point shim |
| `afs/cli.py` | 213 | CLI, .env loading, event formatting |
| `afs/pipeline.py` | 587 | Orchestrator, manifest, resort-awareness |
| `afs/batch_sort.py` | 226 | Step 2: prompt, model call, chunking |
| `afs/analyze.py` | 202 | Vision model, character ID, JSON parsing |
| `afs/preview.py` | 130 | CDR, preview generation |
| `afs/naming.py` | 84 | Semantic filename generation |
| `afs/sorting.py` | 115 | Topic normalization, file moving |
| `afs/types_.py` | 78 | Data classes, tier classification |
| `.env` | 8 | Configuration |
| **Total** | **~1,640** | **9 source files** |

---

## Scorecard

| # | Criterion | Score | Key Finding |
|---|-----------|-------|-------------|
| 1 | Invocation clarity | 4/5 | CLAUDE.md is excellent; `--no-convert-webp` missing from Quick Reference |
| 2 | JSON interface quality | 3/5 | Good structure, but no Step 2 progress, ambiguous error events, no heartbeat |
| 3 | Manifest completeness | 4/5 | Strong; missing `dry_run`, model names, `skipped` count, per-file `elapsed_ms` |
| 4 | Configuration surface | 2/5 | 7 env vars; chunk size, preview resolution, confidence threshold all hardcoded |
| 5 | Error handling for agents | 2/5 | All errors are string-only; `step2-error` is non-actionable; exit 0 on all failures |
| 6 | File granularity | 4/5 | Well-split; manifest writer in pipeline.py is the one candidate for extraction |
| 7 | Naming clarity | 5/5 | Excellent throughout; `topic` vs `folder` distinction is the only subtle point |
| 8 | Re-run efficiency | 4/5 | Resort-awareness works well; no `--force`, no Step 2-only path |
| 9 | Exit codes / stdout discipline | 3/5 | Stdout is clean in --json mode; exit codes are nearly useless |
| 10 | Missing agent affordances | 2/5 | No `--force`, `--config`, `--step1/2-only`, `--max-files`, structured error codes |
| | **Overall** | **33/50** | |

---

## Criterion 1: Invocation Clarity (4/5)

### Strengths
- CLAUDE.md quick-reference gets an agent productive in ~200 tokens
- Five concrete example commands cover the main use cases
- `status` command is a genuine pre-flight check with structured output
- Pipeline decision tree in CLAUDE.md exactly mirrors the code

### Gaps
- `--no-convert-webp` flag absent from CLAUDE.md Quick Reference (present in cli.py:61-64)
- CLAUDE.md says "JSON mode" but does not clarify the output is NDJSON (one JSON object per line)
- `.afs-manifest.json` location not explicitly stated in CLAUDE.md
- No documented exit codes anywhere

---

## Criterion 2: JSON Interface Quality (3/5)

### Event Types
- `start` — run metadata (total, skipped, input, output)
- `progress` — per-file result (tier, confidence, keywords, method, ms)
- `step2-start` — Step 2 begins (files, chunks, prior_folders)
- `step2-done` — folder assignments ({folder: count})
- `step2-error` — Step 2 failure
- `done` — final summary
- `error` — pre-flight failures
- `status` — from status command

### Strengths
- `progress` events carry enough data for real-time quality assessment
- `start` event carries `skipped` count
- `step2-start` carries `chunks` and `prior_folders` counts

### Gaps
- No `step2-chunk-progress` event during chunked sorting — minutes of silence for large batches
- No `model-warm` event when `_warm_model()` fires — silent gap before first progress
- `step2-done` shows `{folder: count}` not `{folder: [filenames]}` — agent must cross-reference
- `step2-error` carries only `"reasoning model returned no valid assignments"` — no error classification
- Per-file errors embedded in `progress` events, not emitted as `event: "error"` — inconsistent

---

## Criterion 3: Manifest Completeness (4/5)

### Present (strong)
- `run.step` tracks pipeline phase (naming/sorting/complete)
- `run.progress` gives file-level position during Step 1
- `stats.avg_confidence` — quality signal without reading individual files
- `stats.sorted` / `stats.filtered` / `stats.errors` — immediate pass/fail
- Per-file: `confidence`, `keywords`, `tier`, `cdr`, `error`, `identified`, `original`
- `folders` summary with per-folder file lists
- `errors` array — deduplicated failure list

### Missing
- `run.dry_run` boolean — agent can't tell if files were actually moved or previewed
- `run.vision_model` / `run.text_model` — which models produced these results?
- `run.version` — no AFS version string for schema compatibility
- `run.skipped` — count appears in events but not persisted to manifest
- Per-file `elapsed_ms` — tracked in FileResult dataclass but silently dropped during manifest write (pipeline.py:480-492)
- `run.chunk_size` — agent adjusting chunking can't see what was used
- `run.ollama_url` — ambiguous if using multiple endpoints
- No `stats.retry_count` — vision retries and chunk retries are invisible

---

## Criterion 4: Configuration Surface (2/5)

### Current (7 env vars)
`SANITIZE_IMAGES`, `CONVERT_WEBP`, `OLLAMA_URL`, `VISION_MODEL`, `TEXT_MODEL`, `VISION_TIMEOUT`, `TEXT_TIMEOUT`

### Hardcoded values an agent cannot change
1. `STEP2_CHUNK_SIZE = 30` (batch_sort.py:17)
2. `max_dim = 1280` preview resolution (preview.py:59, 79)
3. `max_parts = 5` filename keyword count (naming.py:37)
4. `CONFIDENCE_THRESHOLD = 0.5` character ID trigger (analyze.py:97)
5. `num_ctx` for all Ollama calls (analyze.py:68, batch_sort.py:191)
6. `keep_alive: "30m"` on all Ollama calls
7. `TOPIC_CANONICAL` synonym map (~80 entries, sorting.py:14-64)
8. No custom folder whitelist/blocklist
9. No `--config` flag for alternate config files
10. No vision model retry count control

---

## Criterion 5: Error Handling for Agents (2/5)

### Issues
1. `batch_sort._single_call()` catches all exceptions → returns `None` — no classification (batch_sort.py:199)
2. `analyze_vision()` returns `str(e)` — raw exception string, not categorized (analyze.py:77-78)
3. `identify_character()` swallows exceptions silently → returns `None` (analyze.py:131)
4. Per-file errors are free-form strings: `"preview generation failed"`, raw exception messages
5. No pre-flight Ollama check before batch processing
6. ffmpeg absence swallowed as `"preview generation failed"` not `"ffmpeg not installed"` (preview.py:106)
7. Exit code 0 on all outcomes except missing input directory (cli.py:131)
8. Agent cannot distinguish systemic failure (Ollama down) from isolated file failures

---

## Criterion 6: File Granularity (4/5)

### Assessment
- File split is well-considered — single responsibility per file
- `afs/cli.py` mixes .env loading with CLI logic (minor)
- `afs/pipeline.py` (587 lines) contains the 115-line manifest writer — extraction candidate
- `parse_json` in analyze.py is used by batch_sort.py — cross-module utility
- All other files appropriately sized (78-226 lines)

---

## Criterion 7: Naming Clarity (5/5)

Excellent throughout. Self-documenting names at every level.
- `apply_cdr()`, `generate_preview()`, `classify_tier()`
- `step2_batch_sort()`, `build_sort_prompt()`, `build_json_key_map()`
- `_is_already_sorted()`, `_load_prior_manifest()`, `_get_prior_folders()`
- `TOPIC_CANONICAL`, `STEP2_CHUNK_SIZE`, `CONFIDENCE_THRESHOLD`

One subtle point: `FileResult.topic` (set in Step 1 from vision) vs `FileResult.folder` (set in Step 2 after normalization) — both hold topic folder names but at different pipeline stages. Not confusing in code, but the manifest writes both fields without clarifying the distinction.

---

## Criterion 8: Re-run Efficiency (4/5)

### Strengths
- `_is_already_sorted()` uses manifest presence + mtime check
- Prior folders passed to Step 2 for context
- Manifest checkpointed after every file (crash recovery)
- `skipped` count in events

### Gaps
- No `--force` flag — agent must manually delete manifest to force full re-run
- No `--step2-only` — interrupted Step 2 forces expensive Step 1 re-run
- No timezone handling in mtime comparison (pipeline.py:439)
- Error files always reprocessed (correct behavior but undocumented)

---

## Criterion 9: Exit Codes / Stdout Discipline (3/5)

### Stdout
- `--json` mode: one valid JSON object per line, human text suppressed — correct
- `traceback.print_exc()` goes to stderr regardless of mode — acceptable
- `_error()` writes to stderr in human mode, stdout in json mode — correct

### Exit Codes
- `sys.exit(1)` ONLY for missing input directory
- Ollama offline in `status` → exit 0
- All files errored → exit 0
- Step 2 failed → exit 0
- Zero files found → exit 0
- Agent cannot use exit code for health checks

---

## Criterion 10: Missing Agent Affordances (2/5)

1. `--force` — bypass resort-awareness, reprocess all files
2. `--config <path>` — alternate config file
3. `--step2-only` — run Step 2 from existing manifest
4. `--max-files N` — spot-check before committing to full run
5. Structured error categories (not free-form strings)
6. `validate` subcommand — check all dependencies
7. `--manifest-only` — print current manifest without processing
8. JSON schema for manifest (or `$schema` key)
9. `step2-chunk-progress` heartbeat event
10. `step2-error` suggested_action field

---

## AFS vs MCP Comparison

### Why AFS is better than an MCP for this workload

**Token economics**: An MCP tool call means the CLI agent processes every result inline — every file analysis, every folder assignment burns agent tokens. AFS offloads all of that to local Ollama models. For 3,000 files, that's ~3,000 vision calls + ~100 reasoning calls happening locally, zero agent tokens. The agent pays only for the invocation command and reading the manifest.

**Procedural efficiency**: An MCP would expose fine-grained tools (`analyze_file`, `assign_folder`, `move_file`) requiring per-call orchestration by the agent. AFS runs the entire pipeline — CDR, preview, vision, naming, chunked sorting, moves — in a single invocation. Fire once, read the result.

**Manifest as durable handoff**: MCP returns results in conversation context, which compresses and eventually disappears. The `.afs-manifest.json` is persistent on disk — another agent, session, or day can pick it up.

### Where an MCP would be better

**Real-time interaction**: If the agent wants to approve each filename before moving, or adjust mid-run, AFS can't do that. An MCP could expose `preview_name(file)` → agent approves → `move_file(file, folder)`. But that burns tokens proportional to file count, defeating the purpose for bulk operations.

### The sweet spot

AFS stays a standalone tool for bulk processing. A thin MCP wrapper could later expose `afs.status`, `afs.process`, and `afs.manifest` tools — giving MCP discoverability without sacrificing token economics.

---

## Remediation — Implemented in v1.1.0

All gaps remediated in a single pass. Changes:

| Change | Impact | Files |
|--------|--------|-------|
| `config.py` + `afs-config.json` | Criterion 4: 2→5 | New files |
| `--force`, `--config`, `--max-files` flags | Criteria 8,10: 4→5, 2→5 | cli.py, pipeline.py |
| Manifest enrichment (version, dry_run, models, skipped, elapsed_ms, error_type) | Criterion 3: 4→5 | pipeline.py |
| Exit codes 0/1/2/3 | Criterion 9: 3→5 | cli.py |
| Structured error types (8 categories) | Criterion 5: 2→5 | analyze.py, batch_sort.py, pipeline.py, types_.py |
| `step2-chunk` + `warm` events | Criterion 2: 3→5 | batch_sort.py, pipeline.py |
| ffmpeg check in `status` | Criterion 10 | cli.py |
| CLAUDE.md complete rewrite | Criterion 1: 4→5 | CLAUDE.md |

## Second Audit Results (v1.1.0)

| # | Criterion | Before | After | Delta |
|---|-----------|--------|-------|-------|
| 1 | Invocation clarity | 4 | 5 | +1 |
| 2 | JSON interface quality | 3 | 5 | +2 |
| 3 | Manifest completeness | 4 | 5 | +1 |
| 4 | Configuration surface | 2 | 5 | +3 |
| 5 | Error handling | 2 | 5 | +3 |
| 6 | File granularity | 4 | 5 | +1 |
| 7 | Naming clarity | 5 | 5 | — |
| 8 | Re-run efficiency | 4 | 5 | +1 |
| 9 | Exit codes / stdout | 3 | 5 | +2 |
| 10 | Missing agent affordances | 2 | 5 | +3 |
| | **Total** | **33** | **50** | **+17** |
