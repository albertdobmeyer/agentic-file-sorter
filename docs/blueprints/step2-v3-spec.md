# Step 2 v3 — Clean Rewrite Spec

## Problem

Step 2 has been rewritten twice and accumulates dead code. The current version tries keyword frequency analysis + model planning + procedural assignment but the model returns unparseable output and files crash during moves. Time to start clean.

## Core Insight

Step 1 produces descriptive filenames. That's the product. Step 2 just needs to organize them into folders. The filenames ARE the information — no need for keyword analysis or complex prompts.

## New Step 2: Three Clean Passes

### Pass 1: PLAN FOLDERS (1 reasoning call)

**Input:** ~619 unique keywords from manifest (appearing 3+ times)
- NOT filenames (too many tokens)
- NOT keyword frequency analysis (model doesn't need frequencies)
- Just the unique keywords: "cat, bird, forest, castle, meme, cartoon, politics, space, alien..."
- ~1,200 tokens — fits easily in one 8192-context call

**Prompt (minimal):**
```
/no_think
These keywords describe 2,587 image files:
cat, bird, forest, castle, meme, cartoon, politics, space, alien, computer, humor, frog, ...

Create 15-25 topic folder names that would organize files with these keywords.
Folder names: plural, kebab-case, max 2 words. Prefer broad topics.

Respond with ONLY a JSON array: ["animals", "memes", "politics", "science", ...]
```

**Output:** `["animals", "memes", "politics", "science", "nature", ...]`
- JSON array — shortest possible output, no truncation risk
- ~50 tokens response
- One fresh inference, clean context

**Timeout:** 300 seconds (this is the one expensive call)

**Fallback:** If model fails, use `normalize_topic()` on the top 25 most frequent keywords as folder names.

### Pass 2: ASSIGN FILES (Python, zero model calls)

**Algorithm:**
```python
for each file in manifest:
    score each folder by word overlap with filename + keywords
    best match → assign
    no match → "misc"
```

**Word matching strategy:**
1. Exact: filename word == folder name → strong match
2. Contains: filename word contains folder name or vice versa → match
3. Canonical: `normalize_topic(word)` matches a folder → match
4. Topic: Step 1's `topic` field matches a folder → weak match

**No model calls.** ~100ms for 3,000 files.

### Pass 3: RESOLVE AMBIGUOUS (1 reasoning call for misc files)

**Only if** misc has >10% of files (meaning too many fell through string matching).

**Input:** Just the misc filenames + the folder list
```
/no_think
These files could not be automatically assigned to a folder:
  mysterious-forest-at-night.jpg
  colorful-tapestry-with-deities.jpg
  ...

Available folders: animals, memes, politics, science, nature, religion, ...

Assign each file to the best folder. Respond with JSON:
{"mysterious-forest-at-night.jpg": "nature", ...}
```

**This is the old chunked approach but ONLY for the ~10-20% ambiguous files**, not all 2,587. So maybe 1-3 calls instead of 87.

### Pass 4: VERIFY (1 reasoning call, optional)

**Input:** Folder summary `{animals: 120, memes: 450, misc: 30, ...}`
**Output:** Merge map or empty `{}`
**Purpose:** Catch duplicate folders ("comics" and "cartoons" both exist)

## Total Model Calls

| Pass | Calls | Tokens | Time |
|------|-------|--------|------|
| Plan folders | 1 | ~1,300 in, ~50 out | ~30s |
| Assign files | 0 | 0 | ~100ms |
| Resolve misc | 1-3 | ~500 each | ~30-90s |
| Verify | 1 | ~500 | ~10s |
| **Total** | **3-5** | **~3,000** | **~1-2 min** |

vs. old approach: 87 calls, ~25,000 tokens, 10+ minutes.

## Files to Change

**Rewrite from scratch:**
- `afs/batch_sort.py` — delete all existing code, replace with 4 clean functions

**Modify:**
- `afs/pipeline.py` — replace Step 2b block with new 4-pass sequence
- `afs/cli.py` — update event handlers

## Implementation

### `afs/batch_sort.py` (complete rewrite, ~150 lines)

```python
def plan_folders(keywords, config, on_event) -> list[str]
    """Pass 1: One model call. Returns folder name list."""

def assign_files(named_results, folders) -> dict[str, str]
    """Pass 2: Python string matching. Returns {filename: folder}."""

def resolve_ambiguous(misc_files, folders, config, on_event) -> dict[str, str]
    """Pass 3: Model call for misc files only. Returns {filename: folder}."""

def verify_folders(assignments, config, on_event) -> dict[str, str]
    """Pass 4: Model checks for redundant folders. Returns merge map."""
```

No legacy code. No backward compat shims. Clean.

## Verification

1. Test on C:\random-files (15 files) — should complete in <2 minutes
2. Test on W:\KNOWLEDGE (3,392 files) — Step 2 should complete in <3 minutes
3. Check: no files in misc (or <10%), no empty folders, reasonable folder names
4. `python -m pytest tests/` — all tests pass
