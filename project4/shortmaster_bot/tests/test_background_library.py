from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from app.generators.video import VideoAssembler
from app.services.background_library import (
    BackgroundLibraryManager,
    BackgroundLibraryUnavailableError,
)
from app.services.database import ShortsMasterDatabase
from tests.conftest import make_test_config

try:
    from moviepy import ColorClip
except ImportError:  # MoviePy 1.x
    from moviepy.editor import ColorClip


class NoShuffle:
    def shuffle(self, values) -> None:
        return None


def library_config(tmp_path: Path) -> dict:
    config = make_test_config(tmp_path)
    config["storage"] = {
        "base_dir": "",
        "videos_dir": "videos",
        "reports_dir": "reports",
        "logs_dir": "logs",
    }
    config["story_mode"] = {
        "enabled": True,
        "background_video": {
            "mode": "custom_background_library",
            "library_dir": "background_library",
            "manifest_path": "background_library_manifest.json",
            "loop": True,
            "subtitles": "always",
        },
    }
    config["generation"]["video"].update({"width": 180, "height": 320, "fps": 10})
    return config


def write_library(tmp_path: Path, entries: list[dict]) -> None:
    library = tmp_path / "background_library"
    library.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        (library / entry["filename"]).write_bytes(b"test-background")
    (tmp_path / "background_library_manifest.json").write_text(
        json.dumps({"version": 1, "backgrounds": entries}, indent=2),
        encoding="utf-8",
    )


def manifest_entry(filename: str, approved: bool = True) -> dict:
    return {
        "filename": filename,
        "source_url": f"https://example.com/{filename}",
        "license_type": "royalty-free user license",
        "added_by_user": "test-user",
        "approved_for_use": approved,
        "commercial_rights_verified": True,
        "category": "deep_cleaning",
    }


def test_empty_library_pauses_publishing_and_alerts(tmp_path: Path) -> None:
    config = library_config(tmp_path)
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    manager = BackgroundLibraryManager(config, db)
    alerts: list[tuple[str, str]] = []
    manager.alerts.send = lambda event, message: alerts.append((event, message)) or True

    with pytest.raises(BackgroundLibraryUnavailableError):
        manager.select()

    state = db.get_scheduler_state("youtube_upload_scheduler")
    assert state["paused"] is True
    assert "No approved MP4 backgrounds with verified commercial rights" in state["reason"]
    assert alerts and alerts[0][0] == "background library unavailable"


def test_random_selection_uses_only_approved_backgrounds(tmp_path: Path) -> None:
    entries = [
        manifest_entry("clean-a.mp4"),
        manifest_entry("clean-b.mp4"),
        manifest_entry("blocked.mp4", approved=False),
    ]
    write_library(tmp_path, entries)
    manager = BackgroundLibraryManager(library_config(tmp_path), rng=random.Random(11))
    manager._probe_video = lambda _path: None

    selected = {manager.select().filename for _ in range(20)}

    assert selected == {"clean-a.mp4", "clean-b.mp4"}
    assert "blocked.mp4" not in selected


def test_video_output_path_gets_unique_run_suffix_when_file_exists(tmp_path: Path, monkeypatch) -> None:
    assembler = VideoAssembler(library_config(tmp_path))
    base = assembler.output_dir / "000005-a-mulher-do-shopping.mp4"
    base.write_bytes(b"existing render")
    monkeypatch.setenv("GITHUB_RUN_ID", "27104314735")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")

    output_path = assembler._output_path(5, "A mulher do shopping")

    assert output_path != base
    assert output_path.name == "000005-a-mulher-do-shopping-27104314735-1.mp4"


def test_short_background_is_looped_to_narration_duration(tmp_path: Path) -> None:
    assembler = VideoAssembler(library_config(tmp_path))
    source = ColorClip(size=(90, 160), color=(10, 20, 30))
    source = assembler._with_duration(source, 0.5)
    opened = []
    try:
        fitted, looped, trimmed, opened = assembler._fit_background_clip(source, 1.6)
        assert looped is True
        assert trimmed is False
        assert float(fitted.duration) == pytest.approx(1.6)
    finally:
        for clip in reversed(opened):
            clip.close()
        source.close()


def test_long_background_is_trimmed_to_narration_duration(tmp_path: Path) -> None:
    assembler = VideoAssembler(library_config(tmp_path))
    source = ColorClip(size=(90, 160), color=(30, 20, 10))
    source = assembler._with_duration(source, 3.0)
    opened = []
    try:
        fitted, looped, trimmed, opened = assembler._fit_background_clip(source, 1.25)
        assert looped is False
        assert trimmed is True
        assert float(fitted.duration) == pytest.approx(1.25)
    finally:
        for clip in reversed(opened):
            clip.close()
        source.close()


def test_corrupted_background_is_skipped_for_next_approved_file(tmp_path: Path) -> None:
    entries = [manifest_entry("a-corrupted.mp4"), manifest_entry("b-valid.mp4")]
    write_library(tmp_path, entries)
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    manager = BackgroundLibraryManager(library_config(tmp_path), db, rng=NoShuffle())

    def fake_probe(path: Path) -> None:
        if path.name == "a-corrupted.mp4":
            raise ValueError("corrupted file")

    manager._probe_video = fake_probe
    selected = manager.select()
    summary = db.background_library_event_summary()

    assert selected.filename == "b-valid.mp4"
    assert summary[0]["filename"] == "a-corrupted.mp4"
    assert summary[0]["error_count"] == 1


def test_unapproved_background_is_blocked(tmp_path: Path) -> None:
    write_library(tmp_path, [manifest_entry("not-approved.mp4", approved=False)])
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    manager = BackgroundLibraryManager(library_config(tmp_path), db)
    manager.alerts.send = lambda *_args: True
    manager._probe_video = lambda _path: (_ for _ in ()).throw(AssertionError("must not probe unapproved file"))

    with pytest.raises(BackgroundLibraryUnavailableError):
        manager.select()

    assert db.get_scheduler_state("youtube_upload_scheduler")["paused"] is True


def test_background_without_verified_commercial_rights_is_blocked(tmp_path: Path) -> None:
    entry = manifest_entry("rights-not-verified.mp4")
    entry["commercial_rights_verified"] = False
    write_library(tmp_path, [entry])
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    manager = BackgroundLibraryManager(library_config(tmp_path), db)
    manager.alerts.send = lambda *_args: True
    manager._probe_video = lambda _path: (_ for _ in ()).throw(
        AssertionError("must not probe a commercially uncleared file")
    )

    with pytest.raises(BackgroundLibraryUnavailableError):
        manager.select()

    report = manager.scan()
    assert report["approved_assets"] == []
    assert any("commercial rights are not verified" in warning for warning in report["warnings"])


def test_verified_background_resumes_background_related_pause(tmp_path: Path) -> None:
    write_library(tmp_path, [manifest_entry("verified.mp4")])
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    db.set_scheduler_state(
        "youtube_upload_scheduler",
        {"paused": True, "reason": "No approved MP4 backgrounds with verified commercial rights"},
    )
    manager = BackgroundLibraryManager(library_config(tmp_path), db)

    resumed = manager.resume_publishing_if_library_available()

    assert resumed is True
    assert db.get_scheduler_state("youtube_upload_scheduler")["paused"] is False


def test_background_report_tracks_usage_errors_and_license_warning(tmp_path: Path) -> None:
    entry = manifest_entry("used.mp4")
    entry["license_type"] = ""
    write_library(tmp_path, [entry])
    db = ShortsMasterDatabase(tmp_path / "bot.db")
    manager = BackgroundLibraryManager(library_config(tmp_path), db)
    db.record_background_library_event("used.mp4", "failure", "test error")
    paths = manager.write_report()
    report = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))

    assert report["total_backgrounds"] == 1
    assert report["approved_backgrounds"] == 0
    assert report["failed_backgrounds"] == 1
    assert any("missing license metadata" in warning for warning in report["warnings"])
    assert Path(paths["html"]).exists()
