# AFS Demo Video Script

**Duration:** ~90-120 seconds
**Format:** Screen recording (Playwright for dashboard, manual for terminal)
**Resolution:** 1920x1080

---

## Pre-Recording Setup

### Demo Folder 1: Memes (C:\demo-memes)
~25 files with gibberish names. Mix of:
- 15x JPG (memes, cartoons, landscapes)
- 3x GIF (animated memes)
- 2x PNG (memes)
- 1x WebP (to show CDR conversion)
- 1x WebM (video — shows frame extraction)
- 1x MP4 (video)
- 1x PDF (shows Tier 3 filtering)
- 1x TXT (shows Tier 3 filtering)

**All files renamed to random names:**
- `fjk29x.jpg`, `download(3).gif`, `IMG_4477.png`, `reaction4554.jpg`
- `photo(2).webp`, `vid_2024_01.webm`, `clip_final.mp4`
- `meeting_notes.pdf`, `readme.txt`
- `meme final FINAL (1).jpg`, `asdf.gif`, `unnamed.png`

### Demo Folder 2: Photos (C:\demo-photos)
~8 DSLR photos with original camera names:
- `IMG_2534.JPG`, `IMG_2535.JPG`, `IMG_2536.JPG`, etc.
- Include Tori face sample in faces/ directory

### Dashboard
- Running on localhost:7860
- All settings at defaults
- faces/ has tori/ subdirectory with 2 samples

### Terminal
- Claude Code open in a terminal
- Working directory: the agentic-file-sorter repo

---

## PART 1: Memes (Terminal + Claude Code) — ~60 seconds

### Scene 1: The Problem (5 seconds)
- Show `C:\demo-memes` in file explorer or `ls` output
- 25 files with meaningless names
- Pause to let viewer read a few names

**Voiceover/text overlay:** "A typical download folder. 25 files with meaningless names. No organization."

### Scene 2: Claude Code Invocation (10 seconds)
- Terminal with Claude Code open
- Type prompt:

```
Sort the files in C:\demo-memes using the agentic file sorter.
Use default settings and let it run.
```

- Claude Code reads CLAUDE.md, understands the tool, runs:

```bash
python afs.py process "C:\demo-memes"
```

**Text overlay:** "One prompt. Claude Code reads CLAUDE.md and knows what to do."

### Scene 3: Processing (20 seconds, sped up 4x)
- Show terminal output scrolling:
  - "Processing 25 files..."
  - Per-file progress: `[1/25] T1 NAMED: fjk29x.jpg → smug-pepe-sunglasses [pepe, smug, sunglasses]`
  - Step 2a consolidation
  - Step 2b folder assignment
  - "Done: 23 moved, 0 errors, 2 filtered"

**Text overlay:** "Local vision model names each file. Reasoning model organizes into folders. Zero cloud tokens."

### Scene 4: The Result (10 seconds)
- Show the sorted directory structure:
```
demo-memes/
  animals/
    cat-flower-crown-headband.jpg
    bird-blue-jay-tree-nature.jpg
  memes/
    smug-pepe-sunglasses.jpg
    minion-despicable-me-yellow.jpg
  fantasy/
    castle-village-mountains.jpg
    wizard-throne-elderly.jpg
  filtered/
    pdf/meeting_notes.pdf
    txt/readme.txt
```
- Pause to let viewer read

**Text overlay:** "Every file named by content. Sorted into topic folders. Non-images safely filtered."

### Scene 5: Manifest (5 seconds)
- Quick flash of manifest JSON in terminal:
```bash
python -c "import json; m=json.load(open('.afs-manifest.json')); print(f'Named: {m[\"stats\"][\"named\"]}, Sorted: {m[\"stats\"][\"sorted\"]}, Errors: {m[\"stats\"][\"errors\"]}')"
```

**Text overlay:** "Structured manifest for agent handoff. Resume-safe. Crash-safe."

---

## PART 2: Photos + Dashboard — ~50 seconds

### Scene 6: The Photo Problem (5 seconds)
- Show `C:\demo-photos` — 8 files all named `IMG_NNNN.JPG`

**Text overlay:** "Camera photos. Generic names. Who's in them?"

### Scene 7: Dashboard Tour (15 seconds)
- Open browser to localhost:7860
- Quick pan across Settings tab (2 seconds)
  - Hover over "Photo Threshold" tooltip (hold 2 seconds)
  - Hover over "Skip CDR for Photos" tooltip (hold 2 seconds)
- Click Faces tab (3 seconds)
  - Show existing Tori samples (green-hair, red-hair)
  - Brief pause on help text: "Upload clear, close-up face photos..."
- Click Run tab (2 seconds)

**Text overlay:** "Settings dashboard. Customize everything. Face recognition built in."

### Scene 8: Run from Dashboard (15 seconds, sped up 4x)
- In Run tab:
  - Click Browse → navigate to `C:\demo-photos` → Select This Folder
  - Click "Process" button
  - Show live output streaming:
    - `[1/8] named: IMG_2534.JPG [tori, woman, red hair] [tori] [photo]`
    - Progress bar filling up
    - `Done! 8 moved, 0 errors (45s)`

**Text overlay:** "Photos auto-detected. CDR skipped. Faces identified."

### Scene 9: The Photo Result (8 seconds)
- Show flat directory — no subfolders, just renamed files:
```
demo-photos/
  tori-woman-red-hair-2534.jpg
  tori-mirror-red-hair-2535.jpg
  cat-curtain-peekaboo-2537.jpg
  dark-room-projector-2614.jpg
  wooden-table-living-room-2531.jpg
```

**Text overlay:** "Photos stay flat. People named. Sequence numbers preserved. Searchable by name."

---

## End Screen (5 seconds)

```
AFS — Agentic File Sorter
github.com/albertdobmeyer/agentic-file-sorter

Part of the Agentic Power-Tools series
Star the repo if this is useful.
```

---

## Recording Notes

- **Speed:** Real-time for human interactions, 4x for processing wait times
- **Privacy:** Use C:\ paths only. No personal folders visible.
- **Transitions:** Simple cuts between scenes. No fancy transitions.
- **Audio:** Optional background music (lo-fi). Voiceover optional — text overlays work fine.
- **Thumbnail:** Before/after split — gibberish filenames left, semantic names right.

## Playwright Automation (Dashboard scenes)

Playwright can automate:
- Opening dashboard URL
- Clicking tabs
- Hovering over tooltips (with pause)
- Clicking Browse, navigating folders, selecting
- Clicking Process, waiting for output
- Taking screenshots at key moments

Terminal scenes should be recorded manually (or via asciinema).
