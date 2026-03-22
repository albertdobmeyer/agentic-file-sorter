# Changelog

All notable changes to AFS are documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.3.0] - 2026-03-22

### Added
- Three-step pipeline: Step 1 (naming), Step 2a (folder consolidation), Step 2b (file assignment)
- Photo detection: EXIF, resolution, camera filename patterns — CDR auto-skipped
- Face recognition via sample images (`faces/` directory, multi-image Ollama API)
- Folder consolidation (Step 2a): holistic merge of redundant folders
- Flatten command: `python afs.py flatten <dir>`
- `--reface` flag: re-run face identification without full reprocessing
- OS sleep prevention during long runs (Windows)
- Full `afs-config.json` with all settings and defaults
- `setup.sh` convenience installer
- GitHub Pages landing site
- Naming quality: synonym deduplication, meta-word filtering, photo-specific prompts

### Changed
- Pipeline: two-step to three-step (Step 2a consolidation inserted)
- Version bump: 1.1.0 to 1.3.0

## [1.2.0] - 2026-03-21

### Added
- Step 2a folder consolidation
- Sleep prevention
- NDJSON events for consolidation

## [1.1.0] - 2026-03-18

### Added
- Initial release: two-step pipeline, CDR, resort-awareness, NDJSON events
