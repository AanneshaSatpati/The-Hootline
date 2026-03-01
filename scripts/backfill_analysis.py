#!/usr/bin/env python3
"""Backfill learning analysis for an episode published before the learning system deployed.

Re-runs audio transcription with the current (extended) prompt, then runs episode analysis.

Usage:
    python3 scripts/backfill_analysis.py --date 2026-02-28
    python3 scripts/backfill_analysis.py --date 2026-02-28 --show-id hootline
"""

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import shows
from src import database
from src.audio_transcriber import transcribe_episode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("backfill_analysis")


def backfill(date: str, show_id: str = "hootline"):
    show = shows.get(show_id)
    if not show:
        print(f"Unknown show_id: {show_id}")
        return

    db_path = show.db_path

    # 1. Get episode from DB
    conn = database._get_connection(db_path)
    try:
        episode = conn.execute("SELECT * FROM episodes WHERE date = ?", (date,)).fetchone()
        digest = conn.execute("SELECT * FROM digests WHERE date = ?", (date,)).fetchone()
    finally:
        conn.close()

    if not episode:
        print(f"No episode found for date {date}")
        return

    episode = dict(episode)
    print(f"Found episode for {date}: {episode.get('duration_formatted', '?')}")

    # 2. Find the MP3 file (local first, then download from GCS)
    mp3_path = None
    tmp_mp3 = None
    episodes_dir = show.episodes_dir / date
    mp3_files = list(episodes_dir.glob("*.mp3")) if episodes_dir.exists() else []
    if not mp3_files:
        # Try the flat episodes directory and hootline subdirectory
        mp3_files = list(show.episodes_dir.glob(f"*{date}*.mp3"))
    if not mp3_files:
        hootline_dir = show.episodes_dir / "hootline"
        if hootline_dir.exists():
            mp3_files = list(hootline_dir.glob(f"*{date}*.mp3"))
    if mp3_files:
        mp3_path = mp3_files[0]
        print(f"Using local MP3: {mp3_path}")
    else:
        # Download from GCS
        gcs_url = episode.get("gcs_url", "")
        if not gcs_url:
            print(f"No local MP3 and no GCS URL — cannot re-transcribe")
            return
        print(f"No local MP3 found. Downloading from GCS: {gcs_url}")
        import requests
        resp = requests.get(gcs_url, timeout=300)
        resp.raise_for_status()
        tmp_mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_mp3.write(resp.content)
        tmp_mp3.close()
        mp3_path = Path(tmp_mp3.name)
        print(f"Downloaded {len(resp.content)} bytes to {mp3_path}")

    # 3. Re-run transcription with extended prompt (current code)
    print("Re-running audio transcription with extended prompt...")
    segment_order = show.format.segment_order
    segment_durations = show.format.segment_durations

    try:
        audio_analysis = transcribe_episode(
            mp3_path, segment_order, segment_durations, db_path=db_path,
        )
    finally:
        # Clean up temp file if we downloaded from GCS
        if tmp_mp3:
            Path(tmp_mp3.name).unlink(missing_ok=True)

    if not audio_analysis:
        print("Transcription failed")
        return

    print(f"Transcription complete. Word counts: {audio_analysis.get('word_counts', {})}")

    # 4. Update audio_analysis_full and audio_segment_words in DB
    database.update_audio_analysis(
        date,
        audio_analysis.get("word_counts", {}),
        audio_analysis_full=audio_analysis,
        db_path=db_path,
    )
    print("Updated episode DB with new analysis")

    # 5. Clear existing findings and suggestions for this date (stale data from failed run)
    conn = database._get_connection(db_path)
    try:
        conn.execute("DELETE FROM findings WHERE episode_date = ?", (date,))
        conn.execute("DELETE FROM suggestions WHERE episode_date = ?", (date,))
        conn.commit()
    finally:
        conn.close()
    print("Cleared stale findings and suggestions")

    # 6. Get quality report from digest
    quality_report = {}
    if digest:
        try:
            quality_report = json.loads(dict(digest).get("quality_report") or "{}")
        except Exception:
            quality_report = {}

    # 7. Re-run episode analyzer
    print("Running episode analyzer...")
    from src.episode_analyzer import analyze_episode

    result = analyze_episode(
        episode_date=date,
        audio_analysis=audio_analysis,
        quality_report=quality_report,
        db_path=str(db_path),
    )

    print(f"\nBackfill complete:")
    print(f"  Findings: {result['findings_count']}")
    print(f"  Suggestions: {result['suggestions_count']}")
    print(f"  Egregious: {result['is_egregious']}")
    print(f"\nReload the Learning tab in your dashboard to see results.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill learning analysis for an episode")
    parser.add_argument("--date", required=True, help="Episode date in YYYY-MM-DD format")
    parser.add_argument("--show-id", default="hootline", help="Show ID (default: hootline)")
    args = parser.parse_args()
    backfill(args.date, args.show_id)
