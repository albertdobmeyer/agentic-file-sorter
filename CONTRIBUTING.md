# Contributing to AFS

Thanks for your interest. AFS is a lean project and we prefer small, focused contributions.

## Dev Setup

```bash
git clone https://github.com/akd-automation/agentic-file-sorter.git
cd agentic-file-sorter
pip install -r requirements.txt
python -m pytest tests/
```

You also need [Ollama](https://ollama.com/) running locally with a vision model and a reasoning model installed. See `afs-config.json` for defaults.

## Code Style

No linter is enforced. Follow the existing patterns:
- Type hints on function signatures
- Docstrings on public functions
- Imports use `from afs.X import Y`
- Config threaded as a `config` dict, not globals

## Testing

All changes must pass the existing 57 tests:

```bash
python -m pytest tests/
```

Add tests for new features. If your change touches the pipeline, test it against a real folder of files before opening a PR.

## Prompts

Changes to vision or reasoning prompts (in `analyze.py`, `batch_sort.py`, `consolidate.py`) have outsized impact. Test prompt changes on a real folder with diverse file types and verify manifest output before merging.

## Config

New settings must be added in three places:
1. `DEFAULTS` dict in `afs/config.py`
2. `_ENV_MAP` in `afs/config.py` (if the setting should be env-overridable)
3. `afs-config.json` with a sensible default value

## Constitution

Design decisions in AFS trace back to five axioms in `CONSTITUTION.md`. Read it before proposing architectural changes. If your feature conflicts with an axiom, explain the tradeoff in your PR description.

## Pull Requests

- One feature or fix per PR
- Small, focused diffs preferred
- Reference an issue number if one exists
- See the PR template checklist for requirements
