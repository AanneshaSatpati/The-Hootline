#!/usr/bin/env python3
"""Publish a pre-existing episode (digest + audio) for a specific date.

Usage:
    NOCTUA_ENV=prod python scripts/publish_episode.py \
        --date 2026-03-01 \
        --show-id hootline \
        --digest static/noctua-digest-2026-03-01.md \
        --audio static/OpenAI_Funding_Iran_Strikes_and_Seattle_Protests.m4a

This bypasses the normal preparation workflow and directly:
1. Saves the digest to the database
2. Converts audio to MP3 (if needed)
3. Processes the episode (extracts metadata, uploads MP3 to GCS)
4. Adds the episode to the RSS feed
5. Uploads the database to GCS
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Ensure we can import app modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("publish_episode")


def parse_digest_metadata(markdown_text: str) -> dict:
    """Extract metadata from a digest markdown file."""
    # Count segment headers (## SEGMENT N: ...)
    segments = re.findall(r'^## SEGMENT \d+: (.+?)(?:\s*\(~)', markdown_text, re.MULTILINE)
    segment_names = [s.strip() for s in segments]

    # Count words (excluding the production instructions header)
    content_start = markdown_text.find("## INTRO")
    content = markdown_text[content_start:] if content_start > 0 else markdown_text
    total_words = len(content.split())

    # Count articles/sources (rough: count "---" separators in content area)
    article_count = len(segment_names)

    # Build topics summary from segment names
    topics_summary = ", ".join(segment_names) if segment_names else "General News"

    # Build segment counts (approximate word count per segment)
    segment_counts = {}
    for name in segment_names:
        segment_counts[name] = 1  # At least one article per segment

    return {
        "total_words": total_words,
        "article_count": article_count,
        "topics_summary": topics_summary,
        "segment_counts": segment_counts,
        "segment_sources": {name: [] for name in segment_names},
    }


def main():
    parser = argparse.ArgumentParser(description="Publish a pre-existing episode")
    parser.add_argument("--date", required=True, help="Episode date (YYYY-MM-DD)")
    parser.add_argument("--show-id", default="hootline", help="Show ID")
    parser.add_argument("--digest", required=True, help="Path to digest markdown file")
    parser.add_argument("--audio", required=True, help="Path to audio file (MP3 or M4A)")
    parser.add_argument("--dry-run", action="store_true", help="Don't upload to GCS")
    args = parser.parse_args()

    # Validate date format
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", args.date):
        logger.error("Invalid date format: %s (expected YYYY-MM-DD)", args.date)
        sys.exit(1)

    # Validate environment
    env = os.environ.get("NOCTUA_ENV", "dev")
    if env != "prod" and not args.dry_run:
        logger.error("NOCTUA_ENV is '%s'. Set NOCTUA_ENV=prod to persist to GCS, or use --dry-run.", env)
        sys.exit(1)

    # Import app modules (after env is set)
    from config import shows
    from src import database, episode_manager, feed_builder, gcs_storage

    if args.show_id not in shows:
        logger.error("Unknown show: %s (available: %s)", args.show_id, list(shows.keys()))
        sys.exit(1)

    show = shows[args.show_id]
    date = args.date

    # --- Step 1: Read and save digest ---
    digest_path = Path(args.digest)
    if not digest_path.exists():
        logger.error("Digest file not found: %s", digest_path)
        sys.exit(1)

    markdown_text = digest_path.read_text()
    meta = parse_digest_metadata(markdown_text)

    logger.info("Digest: %d words, %d segments, topics: %s",
                meta["total_words"], meta["article_count"], meta["topics_summary"])

    # Build RSS summary from topics
    rss_summary = f"OpenAI funding, Iran strikes, Seattle protests, and more in today's briefing."

    database.save_digest(
        date=date,
        markdown_text=markdown_text,
        article_count=meta["article_count"],
        total_words=meta["total_words"],
        topics_summary=meta["topics_summary"],
        rss_summary=rss_summary,
        segment_counts=meta["segment_counts"],
        segment_sources=meta["segment_sources"],
        force=True,
        db_path=show.db_path,
    )
    logger.info("Digest saved to database for %s", date)

    # --- Step 2: Convert audio to MP3 ---
    audio_path = Path(args.audio)
    if not audio_path.exists():
        logger.error("Audio file not found: %s", audio_path)
        sys.exit(1)

    episodes_dir = show.episodes_dir
    episodes_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = episodes_dir / f"noctua-{date}.mp3"

    ext = audio_path.suffix.lower()
    if ext == ".mp3":
        # Copy directly
        shutil.copy2(str(audio_path), str(mp3_path))
        logger.info("Copied MP3 to %s", mp3_path)
    else:
        # Convert with ffmpeg
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        logger.info("Converting %s to MP3 via ffmpeg...", audio_path.name)
        result = subprocess.run(
            [ffmpeg, "-i", str(audio_path),
             "-codec:a", "libmp3lame", "-qscale:a", "2",
             "-y", str(mp3_path)],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            logger.error("ffmpeg conversion failed: %s", result.stderr[-500:])
            sys.exit(1)
        logger.info("Converted to MP3: %s (%.1f MB)", mp3_path.name, mp3_path.stat().st_size / 1e6)

    # --- Step 3: Process episode (metadata + GCS upload) ---
    logger.info("Processing episode...")
    metadata = episode_manager.process(
        mp3_path,
        topics_summary=meta["topics_summary"],
        rss_summary=rss_summary,
        show=show,
    )
    logger.info("Episode processed: %s, duration=%s, size=%.1f MB, gcs_url=%s",
                metadata.date, metadata.duration_formatted,
                metadata.file_size_bytes / 1e6, metadata.gcs_url or "(none)")

    # --- Step 4: Add to RSS feed ---
    feed_builder.add_episode(metadata, show=show)
    logger.info("Episode added to RSS feed")

    # --- Step 5: Upload DB to GCS ---
    if not args.dry_run:
        result = gcs_storage.upload_db(show.db_path, show.show_id)
        if result:
            logger.info("Database uploaded to GCS")
        else:
            logger.warning("Database upload returned False (check if GCS is configured)")

    logger.info("=== DONE ===")
    logger.info("Date: %s", date)
    logger.info("Duration: %s", metadata.duration_formatted)
    logger.info("GCS URL: %s", metadata.gcs_url or "N/A")
    logger.info("Topics: %s", meta["topics_summary"])


if __name__ == "__main__":
    main()
