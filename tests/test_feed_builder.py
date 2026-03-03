"""Tests for feed_builder module."""

import json
from pathlib import Path
from unittest.mock import patch

from config import ShowConfig
from src.feed_builder import (
    _build_feed_generator,
    _load_episode_catalog,
    _save_episode_catalog,
    add_episode,
)
from src.models import EpisodeMetadata


def _make_show(tmp_path: Path) -> ShowConfig:
    """Create a ShowConfig pointing at tmp_path for tests."""
    return ShowConfig(
        show_id="test",
        podcast_title="The Hootline",
        podcast_description="Test show",
        gmail_credentials_json="",
        gmail_token_json="",
        gmail_label="",
        notebooklm_notebook_url="",
        google_account_email="",
        google_account_password="",
        output_dir=tmp_path,
    )


def _make_metadata(date: str = "2026-02-16", tmp_path: Path = Path("output")) -> EpisodeMetadata:
    return EpisodeMetadata(
        date=date,
        file_path=tmp_path / "episodes" / f"noctua-{date}.mp3",
        file_size_bytes=5_000_000,
        duration_seconds=1200,
        duration_formatted="00:20:00",
        topics_summary="Test topic A; Test topic B",
    )


def test_load_episode_catalog_empty(tmp_path):
    show = _make_show(tmp_path)
    result = _load_episode_catalog(show=show)
    assert result == []


def test_save_and_load_catalog(tmp_path):
    show = _make_show(tmp_path)
    episodes = [{"date": "2026-02-16", "file_size_bytes": 5000000}]
    _save_episode_catalog(episodes, show=show)
    loaded = _load_episode_catalog(show=show)
    assert loaded == episodes


def test_build_feed_generator():
    episodes = [
        {
            "date": "2026-02-16",
            "file_size_bytes": 5000000,
            "duration_seconds": 1200,
            "duration_formatted": "00:20:00",
            "topics_summary": "Topic A; Topic B",
            "published": "2026-02-16T18:00:00+00:00",
        }
    ]
    fg = _build_feed_generator(episodes)
    rss = fg.rss_str(pretty=True).decode()
    assert "The Hootline" in rss
    assert "audio/mpeg" in rss
    assert "February 16, 2026" in rss
    assert "itunes" in rss.lower()


def test_add_episode_and_build_feed(tmp_path):
    show = _make_show(tmp_path)
    with patch("src.feed_builder.database.save_episode"):
        metadata = _make_metadata(tmp_path=tmp_path)
        add_episode(metadata, show=show)

        json_path = show.episodes_json_path
        feed_path = show.feed_path

        assert json_path.exists()
        assert feed_path.exists()

        catalog = json.loads(json_path.read_text())
        assert len(catalog) == 1
        assert catalog[0]["date"] == "2026-02-16"

        feed_content = feed_path.read_text()
        assert "audio/mpeg" in feed_content


def test_add_episode_replaces_same_date(tmp_path):
    show = _make_show(tmp_path)
    with patch("src.feed_builder.database.save_episode"):
        add_episode(_make_metadata("2026-02-16", tmp_path=tmp_path), show=show)
        add_episode(_make_metadata("2026-02-16", tmp_path=tmp_path), show=show)

        catalog = json.loads(show.episodes_json_path.read_text())
        assert len(catalog) == 1
