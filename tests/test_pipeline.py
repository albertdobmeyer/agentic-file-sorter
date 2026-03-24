"""Validation tests for the batch sort pipeline.

Tests the pipeline logic (collect_files, resort-awareness, Step 2 batch sort,
manifest schema, event emission) without requiring Ollama or real files.
"""

import datetime
import json
import pathlib
import tempfile
import time
from unittest.mock import patch, MagicMock

import pytest

from afs.types_ import FileResult, BatchResult, classify_tier
from afs.sorting import normalize_topic
from afs.naming import generate_name, deduplicate_path
from afs.pipeline import (
    collect_files,
    _load_prior_manifest,
    _is_already_sorted,
    _get_prior_folders,
    _write_manifest,
    _move_to_filtered,
    _move_to_errors,
    process_file,
    process_batch,
)
from afs.batch_sort import build_sort_prompt, step2_batch_sort


# --- Fixtures ---


@pytest.fixture
def tmp_dir(tmp_path):
    """Create a temp directory with test files."""
    (tmp_path / "cat.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    (tmp_path / "dog.png").write_bytes(b"\x89PNG" + b"\x00" * 100)
    (tmp_path / "notes.txt").write_text("hello")
    (tmp_path / "script.py").write_text("print('hi')")
    return tmp_path


@pytest.fixture
def tmp_dir_with_filtered(tmp_dir):
    """Temp directory that also has a filtered/ subdirectory from a prior run."""
    filtered = tmp_dir / "filtered" / "txt"
    filtered.mkdir(parents=True)
    (filtered / "old-notes.txt").write_text("old")
    (tmp_dir / "filtered" / "errors").mkdir(parents=True)
    (tmp_dir / "filtered" / "errors" / "bad.jpg").write_bytes(b"\x00")
    return tmp_dir


@pytest.fixture
def output_dir(tmp_path):
    """Separate output directory."""
    out = tmp_path / "output"
    out.mkdir()
    return out


# --- collect_files ---


class TestCollectFiles:
    def test_collects_root_files(self, tmp_dir):
        files = collect_files(tmp_dir)
        names = {f.name for f in files}
        assert "cat.jpg" in names
        assert "dog.png" in names
        assert "notes.txt" in names

    def test_skips_hidden_dirs(self, tmp_dir):
        hidden = tmp_dir / ".hidden"
        hidden.mkdir()
        (hidden / "secret.txt").write_text("secret")
        files = collect_files(tmp_dir)
        names = {f.name for f in files}
        assert "secret.txt" not in names

    def test_skips_hidden_files(self, tmp_dir):
        (tmp_dir / ".afs-manifest.json").write_text("{}")
        files = collect_files(tmp_dir)
        names = {f.name for f in files}
        assert ".afs-manifest.json" not in names

    def test_skips_filtered_directory(self, tmp_dir_with_filtered):
        files = collect_files(tmp_dir_with_filtered)
        names = {f.name for f in files}
        assert "old-notes.txt" not in names
        assert "bad.jpg" not in names
        # Root files still collected
        assert "cat.jpg" in names

    def test_collects_non_filtered_subdirs(self, tmp_dir):
        sub = tmp_dir / "subfolder"
        sub.mkdir()
        (sub / "nested.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        files = collect_files(tmp_dir)
        names = {f.name for f in files}
        assert "nested.jpg" in names


# --- classify_tier ---


class TestClassifyTier:
    def test_jpg_is_tier1(self, tmp_path):
        f = tmp_path / "test.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0")
        assert classify_tier(f) == 1

    def test_png_is_tier1(self, tmp_path):
        f = tmp_path / "test.png"
        f.write_bytes(b"\x89PNG")
        assert classify_tier(f) == 1

    def test_txt_is_tier3(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        assert classify_tier(f) == 3

    def test_mp4_is_tier2(self, tmp_path):
        f = tmp_path / "test.mp4"
        f.write_bytes(b"\x00" * 100)
        assert classify_tier(f) == 2


# --- normalize_topic ---


class TestNormalizeTopic:
    def test_singular_to_plural(self):
        assert normalize_topic("cat") == "animals"
        assert normalize_topic("vehicle") == "vehicles"

    def test_kebab_case(self):
        assert normalize_topic("video games") == "games"

    def test_max_two_words(self):
        result = normalize_topic("very long topic name")
        assert len(result.split("-")) <= 2

    def test_empty_returns_misc(self):
        assert normalize_topic("") == "misc"

    def test_already_valid(self):
        assert normalize_topic("animals") == "animals"


# --- Resort-awareness ---


class TestResortAwareness:
    def test_load_empty_dir(self, tmp_path):
        files, ts = _load_prior_manifest(tmp_path)
        assert files == {}
        assert ts == ""

    def test_load_valid_manifest(self, tmp_path):
        manifest = {
            "run": {"timestamp": "2026-03-18T12:00:00"},
            "files": [
                {"source": "cat.jpg", "folder": "animals", "status": "moved"},
                {"source": "meme.jpg", "folder": "memes", "status": "moved"},
            ],
        }
        (tmp_path / ".afs-manifest.json").write_text(json.dumps(manifest))
        files, ts = _load_prior_manifest(tmp_path)
        assert len(files) == 2
        assert "cat.jpg" in files
        assert ts == "2026-03-18T12:00:00"

    def test_load_corrupt_manifest(self, tmp_path):
        (tmp_path / ".afs-manifest.json").write_text("NOT JSON {{{")
        files, ts = _load_prior_manifest(tmp_path)
        assert files == {}
        assert ts == ""

    def test_is_already_sorted_matches(self, tmp_path):
        f = tmp_path / "cat.jpg"
        f.write_bytes(b"\xff\xd8")
        # Set file mtime to an older time
        old_time = time.time() - 3600
        import os
        os.utime(str(f), (old_time, old_time))

        prior_files = {"cat.jpg": {"folder": "animals", "status": "moved"}}
        prior_ts = datetime.datetime.now().isoformat(timespec="seconds")
        assert _is_already_sorted(f, prior_files, prior_ts) is True

    def test_is_already_sorted_newer_file(self, tmp_path):
        f = tmp_path / "cat.jpg"
        f.write_bytes(b"\xff\xd8")
        # File is brand new, prior timestamp is old
        prior_files = {"cat.jpg": {"folder": "animals", "status": "moved"}}
        prior_ts = "2020-01-01T00:00:00"
        assert _is_already_sorted(f, prior_files, prior_ts) is False

    def test_is_already_sorted_not_in_manifest(self, tmp_path):
        f = tmp_path / "new.jpg"
        f.write_bytes(b"\xff\xd8")
        prior_files = {"cat.jpg": {"folder": "animals", "status": "moved"}}
        prior_ts = datetime.datetime.now().isoformat(timespec="seconds")
        assert _is_already_sorted(f, prior_files, prior_ts) is False

    def test_is_already_sorted_error_status_not_skipped(self, tmp_path):
        f = tmp_path / "bad.jpg"
        f.write_bytes(b"\xff\xd8")
        old_time = time.time() - 3600
        import os
        os.utime(str(f), (old_time, old_time))
        prior_files = {"bad.jpg": {"folder": "", "status": "error"}}
        prior_ts = datetime.datetime.now().isoformat(timespec="seconds")
        assert _is_already_sorted(f, prior_files, prior_ts) is False

    def test_is_already_sorted_empty_prior(self, tmp_path):
        f = tmp_path / "cat.jpg"
        f.write_bytes(b"\xff\xd8")
        assert _is_already_sorted(f, {}, "") is False

    def test_is_already_sorted_handles_timezone_aware_ts(self, tmp_path):
        """Timezone-aware timestamp in manifest should not crash (TypeError catch)."""
        f = tmp_path / "cat.jpg"
        f.write_bytes(b"\xff\xd8")
        prior_files = {"cat.jpg": {"folder": "animals", "status": "moved"}}
        # Timezone-aware timestamp
        prior_ts = "2026-03-18T12:00:00+05:00"
        # Should not raise TypeError, should return False gracefully
        result = _is_already_sorted(f, prior_files, prior_ts)
        assert isinstance(result, bool)

    def test_get_prior_folders(self):
        prior_files = {
            "a.jpg": {"folder": "animals"},
            "b.jpg": {"folder": "memes"},
            "c.txt": {"folder": ""},
            "d.jpg": {"folder": "filtered"},
        }
        folders = _get_prior_folders(prior_files)
        assert "animals" in folders
        assert "memes" in folders
        assert "" not in folders
        assert "filtered" not in folders


# --- Step 2 prompt building ---


class TestBuildSortPrompt:
    def test_basic_prompt_structure(self):
        results = [
            FileResult(source="/tmp/cat.jpg", keywords=["cat", "orange", "sleeping"]),
            FileResult(source="/tmp/meme.jpg", keywords=["funny", "text"]),
        ]
        prompt = build_sort_prompt(results, [])
        assert "FILES TO SORT:" in prompt
        assert "cat.jpg" in prompt
        assert "meme.jpg" in prompt
        assert "RULES:" in prompt
        assert '"filtered"' in prompt  # protected folder warning

    def test_includes_existing_folders(self):
        results = [FileResult(source="/tmp/new.jpg", keywords=["cat"])]
        prompt = build_sort_prompt(results, ["animals", "memes"])
        assert "EXISTING FOLDERS" in prompt
        assert "animals" in prompt
        assert "memes" in prompt

    def test_no_existing_folders_section_when_empty(self):
        results = [FileResult(source="/tmp/new.jpg", keywords=["cat"])]
        prompt = build_sort_prompt(results, [])
        assert "EXISTING FOLDERS" not in prompt

    def test_keywords_are_normalized(self):
        """FR-004: keywords must be pre-processed via normalize_topic()."""
        results = [FileResult(source="/tmp/cat.jpg", keywords=["cat", "vehicle"])]
        prompt = build_sort_prompt(results, [])
        # "cat" normalizes to "animals", "vehicle" normalizes to "vehicles"
        assert "animals" in prompt
        assert "vehicles" in prompt

    def test_no_keywords_shows_placeholder(self):
        results = [FileResult(source="/tmp/mystery.jpg", keywords=[])]
        prompt = build_sort_prompt(results, [])
        assert "no description" in prompt


# --- Step 2 batch sort (mocked Ollama) ---


class TestStep2BatchSort:
    @patch("afs.batch_sort.requests.post")
    def test_successful_assignment(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "response": '{"cat.jpg": "animals", "meme.jpg": "memes"}'
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        results = [
            FileResult(source="/tmp/cat.jpg", keywords=["cat"]),
            FileResult(source="/tmp/meme.jpg", keywords=["funny"]),
        ]
        assignments = step2_batch_sort(results, [])
        assert assignments == {"cat.jpg": "animals", "meme.jpg": "memes"}

    @patch("afs.batch_sort.requests.post")
    def test_normalizes_folder_names(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "response": '{"cat.jpg": "Cat Photos"}'
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        results = [FileResult(source="/tmp/cat.jpg", keywords=["cat"])]
        assignments = step2_batch_sort(results, [])
        # "Cat Photos" should be normalized to kebab-case
        assert assignments["cat.jpg"] == "cat-photos"

    @patch("afs.batch_sort.requests.post")
    def test_returns_none_on_network_error(self, mock_post):
        mock_post.side_effect = Exception("connection refused")
        results = [FileResult(source="/tmp/cat.jpg", keywords=["cat"])]
        assert step2_batch_sort(results, []) is None

    @patch("afs.batch_sort.requests.post")
    def test_returns_none_on_invalid_json(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "I cannot do that"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        results = [FileResult(source="/tmp/cat.jpg", keywords=["cat"])]
        assert step2_batch_sort(results, []) is None

    @patch("afs.batch_sort.requests.post")
    def test_returns_none_on_empty_response(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "{}"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        results = [FileResult(source="/tmp/cat.jpg", keywords=["cat"])]
        assert step2_batch_sort(results, []) is None

    @patch("afs.batch_sort.requests.post")
    def test_handles_markdown_fenced_json(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "response": '```json\n{"cat.jpg": "animals"}\n```'
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        results = [FileResult(source="/tmp/cat.jpg", keywords=["cat"])]
        assignments = step2_batch_sort(results, [])
        assert assignments == {"cat.jpg": "animals"}

    @patch("afs.batch_sort.requests.post")
    def test_handles_think_tags(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "response": '<think>Let me think...</think>{"cat.jpg": "animals"}'
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        results = [FileResult(source="/tmp/cat.jpg", keywords=["cat"])]
        assignments = step2_batch_sort(results, [])
        assert assignments == {"cat.jpg": "animals"}


# --- Manifest ---


class TestWriteManifest:
    def test_writes_valid_json(self, tmp_path):
        batch = BatchResult(total=2, moved=1, errors=0, filtered=1)
        r1 = FileResult(
            source="cat.jpg", dest=str(tmp_path / "animals" / "orange-cat.jpg"),
            status="moved", topic="animals", keywords=["orange", "cat"],
            folder="animals", tier=1, confidence=0.9,
        )
        r2 = FileResult(
            source="notes.txt", dest=str(tmp_path / "filtered" / "txt" / "notes.txt"),
            status="moved", topic="filtered", method="filtered", tier=3,
        )
        batch.results = [r1, r2]
        batch.elapsed_ms = 5000

        _write_manifest(batch, tmp_path / "input", tmp_path, True)

        manifest_path = tmp_path / ".afs-manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())

        assert "run" in manifest
        assert "stats" in manifest
        assert "files" in manifest
        assert "folders" in manifest
        assert "errors" in manifest

    def test_stats_section_has_agentic_signals(self, tmp_path):
        batch = BatchResult(total=3, moved=2, errors=0, filtered=1)
        batch.results = [
            FileResult(source="a.jpg", dest=str(tmp_path / "animals" / "a.jpg"),
                       folder="animals", keywords=["cat"], status="moved",
                       tier=1, confidence=0.9),
            FileResult(source="b.jpg", dest=str(tmp_path / "memes" / "b.jpg"),
                       folder="memes", keywords=["funny"], status="moved",
                       tier=1, confidence=0.7),
            FileResult(source="c.txt", dest=str(tmp_path / "filtered" / "txt" / "c.txt"),
                       status="moved", method="filtered", tier=3, topic="filtered"),
        ]
        batch.elapsed_ms = 5000

        _write_manifest(batch, tmp_path / "input", tmp_path, True)
        manifest = json.loads((tmp_path / ".afs-manifest.json").read_text())

        stats = manifest["stats"]
        assert stats["total"] == 3
        assert stats["named"] == 2  # a.jpg + b.jpg
        assert stats["sorted"] == 2  # assigned to topic folders
        assert stats["filtered"] == 1  # c.txt
        assert stats["topic_folders"] == 2  # animals, memes
        assert stats["avg_confidence"] == 0.8  # (0.9 + 0.7) / 2

    def test_filtered_files_appear_in_folder_summary(self, tmp_path):
        batch = BatchResult(total=1, moved=1, filtered=1)
        batch.results = [
            FileResult(source="c.txt", dest=str(tmp_path / "filtered" / "txt" / "c.txt"),
                       status="moved", method="filtered", tier=3, topic="filtered"),
        ]
        batch.elapsed_ms = 100

        _write_manifest(batch, tmp_path / "input", tmp_path, False)
        manifest = json.loads((tmp_path / ".afs-manifest.json").read_text())

        assert "filtered/txt" in manifest["folders"]
        assert manifest["folders"]["filtered/txt"]["count"] == 1

    def test_per_file_entries(self, tmp_path):
        batch = BatchResult(total=1, moved=1)
        r = FileResult(
            source="cat.jpg", dest=str(tmp_path / "animals" / "orange-cat.jpg"),
            status="moved", topic="animals", keywords=["orange", "cat"],
            folder="animals", tier=1, confidence=0.9, identified="Garfield",
        )
        batch.results = [r]
        batch.elapsed_ms = 1000

        _write_manifest(batch, tmp_path / "input", tmp_path, True)
        manifest = json.loads((tmp_path / ".afs-manifest.json").read_text())

        files = manifest["files"]
        assert len(files) == 1
        entry = files[0]
        assert entry["source"] == "cat.jpg"
        assert entry["name"] == "orange-cat.jpg"
        assert entry["keywords"] == ["orange", "cat"]
        assert entry["folder"] == "animals"
        assert entry["status"] == "moved"
        assert entry["tier"] == 1
        assert entry["confidence"] == 0.9
        assert entry["identified"] == "Garfield"

    def test_folder_summary(self, tmp_path):
        batch = BatchResult(total=2, moved=2)
        batch.results = [
            FileResult(source="a.jpg", dest=str(tmp_path / "animals" / "a.jpg"),
                       folder="animals", keywords=["cat"], status="moved"),
            FileResult(source="b.jpg", dest=str(tmp_path / "animals" / "b.jpg"),
                       folder="animals", keywords=["dog"], status="moved"),
        ]
        batch.elapsed_ms = 500

        _write_manifest(batch, tmp_path / "input", tmp_path, False)
        manifest = json.loads((tmp_path / ".afs-manifest.json").read_text())

        assert "animals" in manifest["folders"]
        assert manifest["folders"]["animals"]["count"] == 2

    def test_preserves_prior_entries(self, tmp_path):
        prior_files = {
            "old.jpg": {
                "source": "old.jpg", "name": "old-cat.jpg", "folder": "animals",
                "status": "moved", "keywords": ["cat"], "tier": 1,
                "confidence": 0.8, "identified": None, "error": None,
            }
        }
        batch = BatchResult(total=1, moved=1)
        batch.results = [
            FileResult(source="new.jpg", dest=str(tmp_path / "memes" / "new.jpg"),
                       folder="memes", keywords=["funny"], status="moved"),
        ]
        batch.elapsed_ms = 500

        _write_manifest(batch, tmp_path / "input", tmp_path, False, prior_files)
        manifest = json.loads((tmp_path / ".afs-manifest.json").read_text())

        sources = [f["source"] for f in manifest["files"]]
        assert "new.jpg" in sources
        assert "old.jpg" in sources  # Prior entry preserved

    def test_errors_recorded(self, tmp_path):
        batch = BatchResult(total=1, errors=1)
        batch.results = [
            FileResult(source="bad.jpg", error="vision model timeout", status="error"),
        ]
        batch.elapsed_ms = 100

        _write_manifest(batch, tmp_path / "input", tmp_path, False)
        manifest = json.loads((tmp_path / ".afs-manifest.json").read_text())

        assert len(manifest["errors"]) == 1
        assert manifest["errors"][0]["file"] == "bad.jpg"
        assert "timeout" in manifest["errors"][0]["error"]


# --- Tier 3 and error routing ---


class TestFilteredRouting:
    def test_tier3_routes_to_filtered(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text("print('hi')")
        out = tmp_path / "output"
        out.mkdir()

        result = _move_to_filtered(f, out, dry_run=False, start=time.time())
        assert result.topic == "filtered"
        assert result.tier == 3
        assert result.method == "filtered"
        assert "filtered" in result.dest
        assert result.status == "moved"

    def test_tier3_dry_run(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text("print('hi')")
        out = tmp_path / "output"
        out.mkdir()

        result = _move_to_filtered(f, out, dry_run=True, start=time.time())
        assert result.status == "dry-run"
        assert f.exists()  # File not moved

    def test_errors_route_to_filtered_errors(self, tmp_path):
        f = tmp_path / "bad.jpg"
        f.write_bytes(b"\x00")
        out = tmp_path / "output"
        out.mkdir()

        result = _move_to_errors(f, out, dry_run=False, start=time.time(), error_msg="broken")
        assert result.topic == "filtered"
        assert result.error == "broken"
        assert "errors" in result.dest


# --- process_file (mocked vision) ---


class TestProcessFile:
    def test_tier3_file_no_vision_call(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("a,b,c")
        out = tmp_path / "output"
        out.mkdir()

        with patch("afs.pipeline.analyze_vision") as mock_vision:
            result = process_file(f, out)
            mock_vision.assert_not_called()

        assert result.tier == 3
        assert result.method == "filtered"

    @patch("afs.pipeline.generate_preview", return_value=None)
    @patch("afs.pipeline.apply_cdr", side_effect=lambda p, **kw: p)
    def test_preview_failure_routes_to_errors(self, mock_cdr, mock_preview, tmp_path):
        f = tmp_path / "cat.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        out = tmp_path / "output"
        out.mkdir()

        result = process_file(f, out)
        assert result.error == "preview generation failed"
        assert "errors" in result.dest


# --- process_batch integration (fully mocked) ---


class TestProcessBatch:
    @patch("afs.pipeline.verify_folder_assignments")
    @patch("afs.pipeline.assign_files_procedurally")
    @patch("afs.pipeline.plan_folders")
    @patch("afs.pipeline.process_file")
    def test_two_step_flow(self, mock_pf, mock_plan, mock_assign, mock_verify, tmp_path):
        """Verify Step 1 runs for all files, then Step 2 plan+assign runs."""
        (tmp_path / "a.jpg").write_bytes(b"\xff\xd8")
        (tmp_path / "b.jpg").write_bytes(b"\xff\xd8")
        out = tmp_path / "output"
        out.mkdir()

        mock_pf.side_effect = [
            FileResult(source=str(tmp_path / "a.jpg"), status="named",
                       keywords=["cat"], topic="animals", method="vision", tier=1),
            FileResult(source=str(tmp_path / "b.jpg"), status="named",
                       keywords=["dog"], topic="animals", method="vision", tier=1),
        ]
        mock_plan.return_value = {"animals": ["cat", "dog"]}
        mock_assign.return_value = {"a.jpg": "animals", "b.jpg": "animals"}
        mock_verify.return_value = {}

        events = []
        batch = process_batch(tmp_path, out, dry_run=True, on_event=events.append)

        assert mock_pf.call_count == 2
        assert mock_plan.call_count == 1
        assert mock_assign.call_count == 1

        event_types = [e["event"] for e in events]
        assert "start" in event_types
        assert "step2-start" in event_types
        assert "step2-done" in event_types
        assert "done" in event_types

    @patch("afs.pipeline.verify_folder_assignments")
    @patch("afs.pipeline.assign_files_procedurally")
    @patch("afs.pipeline.plan_folders")
    @patch("afs.pipeline.process_file")
    def test_step2_assigns_to_misc(self, mock_pf, mock_plan, mock_assign, mock_verify, tmp_path):
        """Files with no keyword match go to misc."""
        (tmp_path / "a.jpg").write_bytes(b"\xff\xd8")
        out = tmp_path / "output"
        out.mkdir()

        mock_pf.return_value = FileResult(
            source=str(tmp_path / "a.jpg"), status="named",
            keywords=["cat"], topic="animals", method="vision", tier=1,
        )
        mock_plan.return_value = {"misc": []}
        mock_assign.return_value = {"a.jpg": "misc"}
        mock_verify.return_value = {}

        batch = process_batch(tmp_path, out, dry_run=True, on_event=lambda e: None)

        assert batch.moved == 1
        assert batch.results[0].folder == "misc"

    @patch("afs.pipeline.verify_folder_assignments")
    @patch("afs.pipeline.assign_files_procedurally")
    @patch("afs.pipeline.plan_folders")
    @patch("afs.pipeline.process_file")
    def test_filtered_protection(self, mock_pf, mock_plan, mock_assign, mock_verify, tmp_path):
        """Files assigned to 'filtered' by plan must be redirected to misc."""
        (tmp_path / "a.jpg").write_bytes(b"\xff\xd8")
        out = tmp_path / "output"
        out.mkdir()

        mock_pf.return_value = FileResult(
            source=str(tmp_path / "a.jpg"), status="named",
            keywords=["cat"], topic="animals", method="vision", tier=1,
        )
        mock_plan.return_value = {"animals": ["cat"]}
        mock_assign.return_value = {"a.jpg": "filtered"}
        mock_verify.return_value = {}

        batch = process_batch(tmp_path, out, dry_run=True, on_event=lambda e: None)

        # "filtered" assignment gets redirected to "misc"
        assert batch.results[0].folder == "misc"

    @patch("afs.pipeline.verify_folder_assignments")
    @patch("afs.pipeline.assign_files_procedurally")
    @patch("afs.pipeline.plan_folders")
    @patch("afs.pipeline.process_file")
    def test_unassigned_file_goes_to_misc(self, mock_pf, mock_plan, mock_assign, mock_verify, tmp_path):
        """Files not in assignments default to misc."""
        (tmp_path / "a.jpg").write_bytes(b"\xff\xd8")
        out = tmp_path / "output"
        out.mkdir()

        mock_pf.return_value = FileResult(
            source=str(tmp_path / "a.jpg"), status="named",
            keywords=["cat"], topic="animals", method="vision", tier=1,
        )
        mock_plan.return_value = {"animals": ["dog"]}
        # File not matched — goes to misc by default
        mock_assign.return_value = {}
        mock_verify.return_value = {}

        batch = process_batch(tmp_path, out, dry_run=True, on_event=lambda e: None)

        # Unassigned file defaults to "misc"
        assert batch.moved == 1
        assert batch.results[0].folder == "misc"

    @patch("afs.pipeline.process_file")
    def test_all_tier3_skips_step2(self, mock_pf, tmp_path):
        """When all files are Tier 3, Step 2 should not run."""
        (tmp_path / "a.txt").write_text("hello")
        out = tmp_path / "output"
        out.mkdir()

        mock_pf.return_value = FileResult(
            source=str(tmp_path / "a.txt"), status="moved",
            method="filtered", tier=3, topic="filtered",
            dest=str(out / "filtered" / "txt" / "a.txt"),
        )

        events = []
        batch = process_batch(tmp_path, out, on_event=events.append)

        event_types = [e["event"] for e in events]
        assert "step2-start" not in event_types  # No Step 2
        assert batch.filtered == 1

    def test_resort_awareness_skips_sorted_files(self, tmp_path):
        """Files in prior manifest with older mtime should be skipped."""
        # Create a file with old mtime
        f = tmp_path / "old.jpg"
        f.write_bytes(b"\xff\xd8")
        import os
        old_time = time.time() - 3600
        os.utime(str(f), (old_time, old_time))

        # Create a new file
        (tmp_path / "new.txt").write_text("new")

        out = tmp_path / "output"
        out.mkdir()

        # Write a prior manifest that includes old.jpg
        manifest = {
            "run": {"timestamp": datetime.datetime.now().isoformat(timespec="seconds")},
            "files": [
                {"source": "old.jpg", "folder": "animals", "status": "moved",
                 "name": "old-cat.jpg", "keywords": ["cat"], "tier": 1,
                 "confidence": 0.8, "identified": None, "error": None},
            ],
        }
        (out / ".afs-manifest.json").write_text(json.dumps(manifest))

        with patch("afs.pipeline.process_file") as mock_pf:
            mock_pf.return_value = FileResult(
                source=str(tmp_path / "new.txt"), status="moved",
                method="filtered", tier=3, topic="filtered",
                dest=str(out / "filtered" / "txt" / "new.txt"),
            )
            events = []
            batch = process_batch(tmp_path, out, on_event=events.append)

            # Only new.txt should be processed, old.jpg skipped
            assert mock_pf.call_count == 1
            start_event = next(e for e in events if e["event"] == "start")
            assert start_event["skipped"] == 1


# --- Event handlers in afs/cli.py ---


class TestEventHandlers:
    def test_print_human_handles_all_events(self):
        """Verify _print_human doesn't crash on any event type."""
        from afs.cli import _print_human

        events = [
            {"event": "start", "total": 5, "skipped": 2, "input": "/in", "output": "/out"},
            {"event": "progress", "index": 1, "file": "cat.jpg", "status": "named",
             "dest": None, "topic": "animals", "keywords": ["cat", "orange"],
             "confidence": 0.9, "method": "vision", "tier": 1, "ms": 500},
            {"event": "progress", "index": 2, "file": "notes.txt", "status": "moved",
             "dest": "/out/filtered/txt/notes.txt", "topic": "filtered",
             "keywords": [], "confidence": 0, "method": "filtered", "tier": 3, "ms": 10},
            {"event": "step2-start", "files": 3, "prior_folders": 2},
            {"event": "step2-done", "assignments": {"animals": 2, "memes": 1},
             "folders_created": 2},
            {"event": "step2-error", "error": "model timeout"},
            {"event": "done", "total": 5, "moved": 3, "errors": 1,
             "filtered": 1, "skipped": 2, "ms": 12000},
        ]

        # Should not raise
        for event in events:
            _print_human(event)


# --- FileResult dataclass ---


class TestFileResult:
    def test_default_folder_empty(self):
        r = FileResult(source="test.jpg")
        assert r.folder == ""

    def test_folder_field_exists(self):
        r = FileResult(source="test.jpg", folder="animals")
        assert r.folder == "animals"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
