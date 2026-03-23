# AFS Identification v2 — Four-Layer Subject Identification

**Status:** SPEC — not yet implemented
**Date:** 2026-03-23
**Scope:** Replace multi-image comparison with a four-layer identification system that's more accurate, more efficient, and has zero runtime dependencies.

## Problem

The current multi-image sample comparison (send target + reference images to llava in one API call) has a ~60% false positive rate for generic subjects. It works for distinctive faces but fails for generic objects, meme characters, and anything where visual similarity isn't enough. The context window gets clogged with 15+ base64 images, confusing the model.

## Solution

Four identification layers, each adding signal. Layers 1-3 run locally with zero dependencies. Layer 4 is optional (text-only web search, no image upload).

```
File arrives
  ↓
Layer 1: Built-in knowledge (prompt, always active)
  → Vision model already knows SpongeBob, Pepe, Elon Musk, etc.
  → Expanded character/celebrity list in prompt (~200 names)
  → Cost: zero extra (already part of Step 1 vision analysis)
  ↓
Layer 2: Perceptual hash DB (Pillow, always active)
  → Compute hash of target image
  → Compare against shipped known-subjects.json (~500 entries)
  → If Hamming distance < threshold → strong match signal
  → Cost: ~1ms per comparison, no model call
  ↓
Layer 3: Text-description matching (Ollama, when samples selected)
  → Pre-analyzed sample descriptions (cached in samples.json)
  → Include descriptions as TEXT in vision prompt (not images)
  → "Known subjects: tori (woman, oval face, green/red hair)"
  → Model matches what it sees against text descriptions
  → Cost: ~0 extra tokens vs current multi-image approach
  ↓
Layer 4: Web search confirmation (optional, toggle)
  → Vision model generates search query from what it sees
  → Text query → DuckDuckGo → text results (titles, tags)
  → Text results fed back as context: "search suggests: Pepe the Frog"
  → Cost: one HTTP request per uncertain identification
```

## Layer 1: Built-in Knowledge (Prompt Enhancement)

### What changes
- Expand the CHARACTER_PROMPT in `analyze.py` from ~60 to ~200 known names
- Add celebrity categories: politicians, actors, athletes, internet personalities
- Add meme categories: Pepe variants, Wojak variants, classic memes
- Merge character identification INTO the main vision prompt (not a separate pass)

### Current flow (2 model calls for uncertain images)
```
analyze_vision() → generic keywords → needs_identification()? → identify_character() → 2nd call
```

### New flow (1 model call, always)
```
analyze_vision() → prompt includes "Known characters/people: [200 names]"
                → model identifies in first pass if it recognizes anyone
                → no separate character identification pass needed
```

### Implementation
- Move character list from `identify_character()` prompt into main `analyze_vision()` prompt
- Add to the JSON schema: `"identified": "character/person name or null"`
- Remove the separate `needs_identification()` → `identify_character()` second pass
- Net effect: FEWER model calls (saves time), BETTER identification (model sees image + names simultaneously)

### File changes
- `afs/analyze.py` — merge character list into main prompt, remove `identify_character()` as separate call
- `afs/pipeline.py` — remove the character ID second-pass block

---

## Layer 2: Perceptual Hash Database

### What it is
A pre-computed database of perceptual hashes for ~500 known subjects (meme characters, celebrities, common objects). Shipped as `data/known-subjects.json`. Computed during development using CLIP + Pillow (CLIP is dev-only, not shipped).

### Perceptual hash algorithm (Pillow only, ~10 lines)
```python
def compute_phash(image_path, hash_size=16):
    """Compute perceptual hash using Pillow. Returns hex string."""
    from PIL import Image
    img = Image.open(image_path).convert("L").resize((hash_size, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if p > avg else "0" for p in pixels)
    return hex(int(bits, 2))

def hamming_distance(hash1, hash2):
    """Count differing bits between two hashes."""
    h1, h2 = int(hash1, 16), int(hash2, 16)
    return bin(h1 ^ h2).count("1")
```

### known-subjects.json structure
```json
{
  "version": 1,
  "subjects": {
    "pepe-the-frog": {
      "category": "meme",
      "aliases": ["pepe", "rare pepe", "feels bad man"],
      "description": "Green cartoon frog with humanoid body, large eyes, red lips",
      "hashes": ["0x3f8e1c7a...", "0x4a2b1d..."],  // multiple variants
      "hash_threshold": 15  // max Hamming distance for match
    },
    "wojak": {
      "category": "meme",
      "aliases": ["feels guy", "that feel"],
      "description": "Simple line-drawn male face, bald, small eyes, thin mouth",
      "hashes": ["0x..."],
      "hash_threshold": 12
    },
    "elon-musk": {
      "category": "celebrity",
      "aliases": ["elon"],
      "description": "Male, receding hairline, angular jaw, often in dark shirt",
      "hashes": ["0x..."],
      "hash_threshold": 20
    }
  }
}
```

### Dev-time tool: `dev-tools/build-hash-db.py`
- NOT shipped in the repo (gitignored)
- Requires CLIP + face_recognition (dev-only)
- Takes a curated image library as input
- For each image: compute perceptual hash, use CLIP to validate similarity, use vision model to generate description
- Outputs `data/known-subjects.json`

### Runtime flow
```python
def match_known_subjects(image_path, db):
    """Check image against known-subjects database. Pillow only."""
    target_hash = compute_phash(image_path)
    matches = []
    for name, entry in db["subjects"].items():
        for ref_hash in entry["hashes"]:
            dist = hamming_distance(target_hash, ref_hash)
            if dist <= entry["hash_threshold"]:
                matches.append((name, dist, entry))
                break
    return sorted(matches, key=lambda x: x[1])  # closest first
```

### File changes
- `afs/hashing.py` — NEW: `compute_phash()`, `hamming_distance()`, `match_known_subjects()`
- `data/known-subjects.json` — NEW: shipped database
- `dev-tools/build-hash-db.py` — NEW: dev-only script (gitignored)
- `afs/pipeline.py` — call `match_known_subjects()` before vision analysis, feed matches as context

---

## Layer 3: Text-Description Sample Matching

### What changes
Instead of sending sample IMAGES alongside the target (multi-image API), we:
1. Pre-describe each sample using the vision model (one-time, cached)
2. Store descriptions in `samples/samples.json`
3. During processing, include descriptions as TEXT in the vision prompt

### Pre-description (one-time per sample)
```python
def describe_sample(image_path, config):
    """Ask vision model to describe a sample image in detail."""
    # Single-image call, specialized prompt
    prompt = """Describe this image in detail for identification purposes.
    Focus on: distinctive facial features, hair, clothing, body type,
    colors, patterns, style, distinguishing marks.
    Be specific enough that someone could identify this subject in another photo.
    Respond with a single paragraph, 2-3 sentences."""
    # Returns: "Young woman with green hair, oval face, light skin, direct gaze, casual clothing"
```

### samples.json structure
```json
{
  "tori": {
    "samples": ["frontal.jpg", "profile.jpg"],
    "description": "Young woman with distinctive green/teal hair, oval face, light skin, direct gaze. Also seen with red hair. Casual style.",
    "quality": {"frontal.jpg": "green", "profile.jpg": "blue"},
    "last_checked": "2026-03-23T12:00:00"
  }
}
```

### Runtime: text in prompt, not images
```python
# OLD (multi-image, clogs context):
images = [target_b64, sample1_b64, sample2_b64, ...]
prompt = "Image 1 is target. Images 2-3 are references for tori..."

# NEW (text descriptions, ~50 tokens per person):
images = [target_b64]  # only the target image
prompt = """...
KNOWN SUBJECTS (identify if any appear in this image):
- tori: Young woman with green/teal or red hair, oval face, light skin, direct gaze
- albert: Man with short brown hair, glasses, angular face, often in dark clothing

If you recognize any of these subjects, include their name in the "identified" field.
..."""
```

### Benefits
- One image per API call (no context clogging)
- Text descriptions are precise and unambiguous
- Vision model is better at "does this match this description?" than visual comparison
- Scales to many subjects without performance degradation (~50 tokens per subject vs ~60KB per image)

### File changes
- `afs/faces.py` → rename to `afs/samples.py`
- Remove `identify_faces()` multi-image approach
- Add `describe_sample()`, `load_sample_descriptions()`
- Update `analyze_vision()` to accept sample descriptions as text context
- `samples/samples.json` — NEW: cached descriptions + quality ratings
- `afs/pipeline.py` — load descriptions once, pass to analyze_vision()
- Dashboard — "Check Samples" button triggers `describe_sample()` for each, stores in samples.json

---

## Layer 4: Web Search Confirmation (Optional)

### What it is
Text-only web search to confirm uncertain identifications. No images uploaded. The vision model generates a search query, we send text to DuckDuckGo, get text results back.

### Flow
```
Vision model sees image → "I think this might be a specific character but I'm not sure"
  ↓
Generate search query: "green frog cartoon meme humanoid"
  ↓
requests.get("https://api.duckduckgo.com/?q=green+frog+cartoon+meme&format=json")
  ↓
Parse results: {"AbstractText": "Pepe the Frog is an Internet meme...", "Heading": "Pepe the Frog"}
  ↓
Feed back: "Web search suggests this may be: Pepe the Frog (Internet meme character)"
  ↓
Vision model confirms or rejects based on visual match + web context
```

### Privacy model
- **Outgoing:** text search query only (e.g., "green cartoon frog meme")
- **Incoming:** text results only (titles, snippets, tags)
- **Never uploaded:** user images, file paths, filenames, personal data
- **Gated:** `web_search_assist: false` by default, requires explicit opt-in
- **Dashboard:** toggle with explanation: "Send text search queries to DuckDuckGo to help identify unknown subjects. No images are sent."

### DuckDuckGo Instant Answer API
- Free, no API key required
- `https://api.duckduckgo.com/?q={query}&format=json&no_html=1`
- Returns: `AbstractText`, `Heading`, `RelatedTopics[].Text`
- Rate limit: reasonable for our use (1 query per uncertain file, not every file)

### When it triggers
- Only when Layer 1-3 return low confidence or no match
- Only when `web_search_assist: true` in config
- Only for files where the vision model signals uncertainty

### File changes
- `afs/web_search.py` — NEW: `search_for_subject(query, config)`, `parse_search_results()`
- `afs/config.py` — add `web_search_assist: false` to defaults
- `afs/pipeline.py` — call web search as final fallback
- Dashboard — add toggle in Settings tab

---

## Implementation Roadmap

### Phase 1: Text-Description Matching (Layer 3) — highest impact, easiest
1. Add `describe_sample()` to `afs/samples.py`
2. Add `samples/samples.json` metadata file
3. Rewrite `analyze_vision()` to accept text descriptions (remove multi-image)
4. Add "Check Samples" to CLI and dashboard
5. Test on Tori photos — should match without sending her images in every call

### Phase 2: Built-in Knowledge Expansion (Layer 1) — prompt-only change
1. Expand character/celebrity list to ~200 names
2. Merge identification into main vision prompt
3. Remove separate `identify_character()` second-pass
4. Test on meme folder — should identify Pepe, SpongeBob, Wojak in first pass

### Phase 3: Perceptual Hash Database (Layer 2) — new module + dev tool
1. Create `afs/hashing.py` with Pillow-only perceptual hash
2. Create `dev-tools/build-hash-db.py` (uses CLIP, gitignored)
3. Curate image library, build initial `data/known-subjects.json` (~200 entries)
4. Wire hash matching into pipeline as pre-filter
5. Test: known memes should be identified without any model call

### Phase 4: Web Search Confirmation (Layer 4) — optional, gated
1. Create `afs/web_search.py` with DuckDuckGo integration
2. Add config toggle + dashboard toggle
3. Wire as fallback after Layers 1-3
4. Test: uncertain identifications confirmed by web context

### Phase 5: Integration + polish
1. All four layers working together in the pipeline
2. Update CLAUDE.md, README.md, CONSTITUTION.md
3. Dashboard: samples tab with quality check, search toggle
4. Run full test on W:\KNOWLEDGE with all layers active
5. Version bump to v2.0.0

---

## Success Criteria

| Metric | Current | Target |
|--------|---------|--------|
| Face identification (Tori) | 2-5/26 correct | 95%+ correct |
| False positives (generic samples) | 60%+ | <5% |
| Known meme characters (Pepe, SpongeBob) | Requires sample image | Identified automatically |
| Known celebrities | Not identified | Identified by name |
| Model calls per file | 1-3 (vision + character ID + face ID) | 1 (single enriched vision call) |
| Sample images in API call | 15 (context clogged) | 0 (text descriptions only) |
| Runtime dependencies | Pillow, requests | Pillow, requests (unchanged) |
| Dev-time dependencies | None | CLIP, face_recognition (not shipped) |

---

## File Summary

| File | Phase | Action |
|------|-------|--------|
| `afs/samples.py` | 1 | Rename from faces.py, add describe_sample(), text-based matching |
| `afs/analyze.py` | 1,2 | Accept text descriptions, merge character list into main prompt |
| `afs/pipeline.py` | 1,2,3,4 | Wire all four layers, remove multi-image calls |
| `afs/hashing.py` | 3 | NEW: perceptual hash computation + DB matching |
| `afs/web_search.py` | 4 | NEW: DuckDuckGo text search, result parsing |
| `data/known-subjects.json` | 3 | NEW: shipped hash + description database |
| `samples/samples.json` | 1 | NEW: cached sample descriptions + quality |
| `dev-tools/build-hash-db.py` | 3 | NEW: dev-only CLIP-based hash builder (gitignored) |
| `afs/config.py` | 1,4 | New keys: web_search_assist, etc. |
| `afs/cli.py` | 1 | check-samples subcommand |
| `dashboard/` | 1,4 | Samples tab updates, search toggle |
| `CLAUDE.md`, `README.md` | 5 | Document new identification system |
| `CONSTITUTION.md` | 5 | Amendment 8: four-layer identification |
