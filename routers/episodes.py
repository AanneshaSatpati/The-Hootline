"""Episode routes — publish, upload, transcribe, serve audio, feed."""

import asyncio
import json
import logging
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Form, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from config import settings
from src import database, episode_manager, feed_builder, gcs_storage
from src.episode_manager import _ffmpeg_path

logger = logging.getLogger(__name__)

ACCEPTED_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".webm"}

router = APIRouter()


def _get_resolve_show():
    from main import _resolve_show
    return _resolve_show


def _get_show_states():
    from main import _show_states
    return _show_states


@router.get("/api/latest-episode")
async def api_latest_episode(show_id: str = Query(default="")):
    """Get the latest episode and the latest digest."""
    _resolve_show = _get_resolve_show()
    state = _resolve_show(show_id)
    show = state.show
    episodes_json = show.episodes_json_path
    episodes_dir = show.episodes_dir
    db_path = show.db_path

    # Determine URL prefix for episodes
    is_legacy = show.is_legacy
    ep_url_prefix = "/episodes" if is_legacy else f"/{show.show_id}/episodes"

    episode_data = None
    if episodes_json.exists():
        episodes = json.loads(episodes_json.read_text())
        if episodes:
            for candidate in sorted(episodes, key=lambda e: e["date"], reverse=True):
                gcs_url = candidate.get("gcs_url", "")
                local_file = episodes_dir / f"noctua-{candidate['date']}.mp3"
                if gcs_url or local_file.exists():
                    audio_url = gcs_url or f"{ep_url_prefix}/noctua-{candidate['date']}.mp3"
                    episode_data = {**candidate, "audio_url": audio_url}
                    break

    digest_meta = None
    all_digests = database.list_digests(limit=1, db_path=db_path)
    if all_digests:
        latest_digest = database.get_digest(all_digests[0]["date"], db_path=db_path)
        if latest_digest:
            has_ep = database.has_episode(latest_digest["date"], db_path=db_path)
            seg_counts = json.loads(latest_digest.get("segment_counts") or "{}")

            # Digest view URL (HTML page)
            if is_legacy:
                dl_url = f"/digests/{latest_digest['date']}.html"
            else:
                dl_url = f"/{show.show_id}/digests/{latest_digest['date']}.html"

            digest_meta = {
                "date": latest_digest["date"],
                "article_count": latest_digest["article_count"],
                "total_words": latest_digest["total_words"],
                "total_chars": len(latest_digest["markdown_text"]),
                "email_count": latest_digest.get("email_count", 0),
                "topics_summary": latest_digest["topics_summary"],
                "segment_counts": seg_counts,
                "download_url": dl_url,
                "locked": has_ep,
            }

    prep = None
    if state.preparation_active and state.preparation_date:
        prep_mp3 = episodes_dir / f"noctua-{state.preparation_date}.prep.mp3"
        has_mp3 = prep_mp3.exists()
        has_digest = state.preparation_digest is not None

        if state.generation_running:
            prep_state = "generating"
        elif state.preparation_error:
            prep_state = "failed"
        elif has_digest and has_mp3:
            prep_state = "audio_uploaded"
        elif has_digest:
            prep_state = "digest_ready"
        else:
            prep_state = "generating"

        existing_episode = database.has_episode(state.preparation_date, db_path=db_path)

        prep = {
            "active": True,
            "generating": state.generation_running,
            "state": prep_state,
            "date": state.preparation_date,
            "existing_episode": existing_episode,
            "error": state.preparation_error,
            "digest": {
                "date": state.preparation_digest.date,
                "article_count": state.preparation_digest.article_count,
                "total_words": state.preparation_digest.total_words,
                "total_chars": len(state.preparation_digest.text),
                "email_count": state.preparation_digest.email_count,
                "topics_summary": state.preparation_digest.topics_summary,
                "segment_counts": state.preparation_digest.segment_counts or {},
                "download_url": f"/api/preparation-digest?show_id={show.show_id}",
            } if has_digest else None,
            "audio": {
                "date": state.preparation_date,
                "audio_url": f"{ep_url_prefix}/noctua-{state.preparation_date}.prep.mp3",
                "file_size_bytes": prep_mp3.stat().st_size if has_mp3 else 0,
            } if has_mp3 else None,
        }

    return JSONResponse({
        "episode": episode_data,
        "digest": digest_meta,
        "preparation": prep,
    })


@router.get("/api/episodes")
async def api_episodes(show_id: str = Query(default="")):
    """Get the full archive of all episodes ever published."""
    state = _get_resolve_show()(show_id)
    episodes = database.list_episodes(db_path=state.show.db_path)
    return JSONResponse({"episodes": episodes, "total": len(episodes)})


@router.post("/api/publish-episode")
async def api_publish_episode(date: str = Form(""), show_id: str = Form("")):
    """Publish a prepared episode to RSS and archive."""
    _resolve_show = _get_resolve_show()
    state = _resolve_show(show_id)
    show = state.show

    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse({"error": "Invalid date format."}, status_code=400)

    if not state.preparation_digest or state.preparation_digest.date != date:
        return JSONResponse({"error": "No preparation digest available for this date."}, status_code=404)

    prep_mp3 = show.episodes_dir / f"noctua-{date}.prep.mp3"
    if not prep_mp3.exists():
        return JSONResponse({"error": f"No uploaded audio found for {date}."}, status_code=404)

    mp3_path = show.episodes_dir / f"noctua-{date}.mp3"
    prep_mp3.rename(mp3_path)

    digest = state.preparation_digest
    database.save_digest(
        date=digest.date,
        markdown_text=digest.text,
        article_count=digest.article_count,
        total_words=digest.total_words,
        topics_summary=digest.topics_summary,
        rss_summary=digest.rss_summary,
        segment_counts=digest.segment_counts,
        segment_sources=digest.segment_sources,
        force=True,
        db_path=show.db_path,
    )
    if digest.quality_report:
        database.save_quality_report(digest.date, digest.quality_report, db_path=show.db_path)

    try:
        metadata = episode_manager.process(mp3_path, digest.topics_summary, digest.rss_summary, show=show)
    except Exception as e:
        return JSONResponse(
            {"error": f"Episode processing failed: {e}"},
            status_code=422,
        )

    try:
        feed_builder.add_episode(metadata, show=show)
    except Exception as e:
        return JSONResponse(
            {"error": f"Feed update failed: {e}"},
            status_code=500,
        )

    state.preparation_active = False
    state.preparation_digest = None

    # NOTE: In dev, this does NOT sync to GCS. Set NOCTUA_ENV=prod to persist.
    gcs_storage.upload_db(show.db_path, show.show_id)

    # Trigger audio transcription in background (non-blocking)
    from main import _transcribe_episode_background
    asyncio.create_task(_transcribe_episode_background(date, mp3_path, show))

    # Determine feed URL
    is_legacy = show.is_legacy
    feed_url = f"{settings.base_url}/feed.xml" if is_legacy else f"{settings.base_url}/{show.show_id}/feed.xml"

    return JSONResponse({
        "status": "ok",
        "message": f"Episode for {date} published to RSS.",
        "feed_url": feed_url,
        "episode": {
            "date": metadata.date,
            "duration_formatted": metadata.duration_formatted,
            "file_size_bytes": metadata.file_size_bytes,
            "topics_summary": metadata.topics_summary,
            "gcs_url": metadata.gcs_url,
        },
    })


@router.post("/api/transcribe-episode")
async def api_transcribe_episode(date: str = Form(""), show_id: str = Form("")):
    """Manually trigger audio transcription for an episode."""
    _resolve_show = _get_resolve_show()
    state = _resolve_show(show_id)
    show = state.show

    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse({"error": "Invalid date format."}, status_code=400)

    if not database.has_episode(date, db_path=show.db_path):
        return JSONResponse({"error": f"No episode found for {date}."}, status_code=404)

    mp3_path = show.episodes_dir / f"noctua-{date}.mp3"
    if not mp3_path.exists():
        return JSONResponse(
            {"error": f"MP3 file not found locally for {date}."},
            status_code=404,
        )

    status = database.get_episodes_with_audio(limit=200, db_path=show.db_path)
    current = next((e for e in status if e["date"] == date), None)
    if current and current.get("audio_analysis_status") == "running":
        return JSONResponse({"error": "Transcription already in progress."}, status_code=409)

    database.set_audio_analysis_status(date, "pending", db_path=show.db_path)
    from main import _transcribe_episode_background
    asyncio.create_task(_transcribe_episode_background(date, mp3_path, show))

    return JSONResponse({"status": "ok", "message": f"Transcription started for {date}."})


@router.get("/api/transcription-status")
async def api_transcription_status(date: str = Query(""), show_id: str = Query("")):
    """Check audio transcription status for an episode."""
    state = _get_resolve_show()(show_id)
    eps = database.get_episodes_with_audio(limit=200, db_path=state.show.db_path)
    ep = next((e for e in eps if e["date"] == date), None)
    status = ep.get("audio_analysis_status", "none") if ep else "none"
    audio_words = json.loads(ep.get("audio_segment_words") or "{}") if ep else {}
    return JSONResponse({"date": date, "status": status, "audio_segment_words": audio_words})


@router.post("/api/upload-episode")
async def api_upload_episode(file: UploadFile, date: str = Form(""), show_id: str = Form("")):
    """Upload audio for a given digest date (preview only, no publishing)."""
    try:
        return await _handle_upload(file, date, show_id)
    except Exception as e:
        logger.error("Unhandled upload error: %s", e, exc_info=True)
        return JSONResponse({"error": f"Upload failed: {e}"}, status_code=500)


async def _handle_upload(file: UploadFile, date: str, show_id: str):
    _resolve_show = _get_resolve_show()
    state = _resolve_show(show_id)
    show = state.show
    episodes_dir = show.episodes_dir
    is_legacy = show.is_legacy
    ep_url_prefix = "/episodes" if is_legacy else f"/{show.show_id}/episodes"

    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse(
            {"error": "Invalid date format. Use YYYY-MM-DD."},
            status_code=400,
        )

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(
            {"error": "Invalid date."},
            status_code=400,
        )

    has_digest = (state.preparation_digest and state.preparation_digest.date == date) or \
                 database.get_digest(date, db_path=show.db_path) is not None
    if not has_digest:
        return JSONResponse(
            {"error": f"No digest found for {date}. Prepare a digest first."},
            status_code=404,
        )

    if not file.filename:
        return JSONResponse({"error": "No file provided."}, status_code=400)
    ext = Path(file.filename).suffix.lower()
    if ext not in ACCEPTED_AUDIO_EXTENSIONS:
        return JSONResponse(
            {"error": f"Unsupported format '{ext}'. Accepted: {', '.join(sorted(ACCEPTED_AUDIO_EXTENSIONS))}"},
            status_code=400,
        )

    episodes_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = episodes_dir / f"noctua-{date}.prep.mp3"
    upload_path = episodes_dir / f"noctua-{date}.prep{ext}"
    try:
        total_bytes = 0
        with open(upload_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):  # 1MB chunks
                f.write(chunk)
                total_bytes += len(chunk)
        if total_bytes == 0:
            upload_path.unlink(missing_ok=True)
            return JSONResponse(
                {"error": "Uploaded file is empty."},
                status_code=400,
            )
        logger.info("Saved upload %s (%d bytes)", upload_path.name, total_bytes)
    except Exception as e:
        logger.error("Failed to save uploaded file: %s", e)
        upload_path.unlink(missing_ok=True)
        return JSONResponse(
            {"error": f"Failed to save file: {e}"},
            status_code=500,
        )

    if ext != ".mp3":
        try:
            logger.info("Converting %s (%d bytes) to MP3...", upload_path.name, upload_path.stat().st_size)
            result = subprocess.run(
                [_ffmpeg_path(), "-i", str(upload_path), "-codec:a", "libmp3lame", "-qscale:a", "2", "-y", str(mp3_path)],
                capture_output=True, text=True, timeout=300,
            )
            upload_path.unlink(missing_ok=True)
            if result.returncode != 0:
                logger.error("ffmpeg failed (exit %d): %s", result.returncode, result.stderr[-500:])
                mp3_path.unlink(missing_ok=True)
                return JSONResponse(
                    {"error": f"Audio conversion failed: {result.stderr[-300:].strip()}"},
                    status_code=422,
                )
            logger.info("Converted %s to MP3 (%d bytes)", ext, mp3_path.stat().st_size)
        except subprocess.TimeoutExpired:
            upload_path.unlink(missing_ok=True)
            mp3_path.unlink(missing_ok=True)
            return JSONResponse(
                {"error": "Audio conversion timed out (file may be too large)."},
                status_code=422,
            )
        except (FileNotFoundError, OSError) as e:
            ffpath = _ffmpeg_path()
            logger.error("ffmpeg error: %s. Resolved path: %s, which: %s", e, ffpath, shutil.which("ffmpeg"))
            upload_path.unlink(missing_ok=True)
            return JSONResponse(
                {"error": f"ffmpeg unavailable (path={ffpath}): {e}. Cannot convert audio."},
                status_code=500,
            )

    try:
        from src.episode_manager import _ensure_mp3, _format_duration
        mp3_path = _ensure_mp3(mp3_path)
        from mutagen.mp3 import MP3
        audio = MP3(str(mp3_path))
        duration_seconds = int(audio.info.length)
        duration_formatted = _format_duration(duration_seconds)
        file_size_bytes = mp3_path.stat().st_size
    except Exception as e:
        logger.error("Audio validation failed for %s: %s", mp3_path.name, e)
        mp3_path.unlink(missing_ok=True)
        return JSONResponse(
            {"error": f"Audio validation failed: {e}"},
            status_code=422,
        )

    return JSONResponse({
        "status": "ok",
        "message": f"Audio for {date} uploaded. Preview ready — publish when ready.",
        "episode": {
            "date": date,
            "duration_formatted": duration_formatted,
            "duration_seconds": duration_seconds,
            "file_size_bytes": file_size_bytes,
            "audio_url": f"{ep_url_prefix}/noctua-{date}.prep.mp3",
        },
    })


@router.post("/api/bump-revision")
async def api_bump_revision(date: str = Form(""), show_id: str = Form("")):
    """Bump the revision for an episode."""
    state = _get_resolve_show()(show_id)
    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse({"error": "Invalid date format."}, status_code=400)
    new_rev = feed_builder.bump_revision(date, show=state.show)
    return JSONResponse({"status": "ok", "date": date, "revision": new_rev})


# --- Feed & Episode file serving ---

@router.get("/feed.xml")
async def feed() -> Response:
    """Serve the RSS podcast feed (default show, backward compat)."""
    state = _get_resolve_show()("")
    feed_path = state.show.feed_path
    if not feed_path.exists():
        return Response(content="Feed not yet generated.", status_code=404)
    return FileResponse(feed_path, media_type="application/rss+xml")


@router.get("/{show_id}/feed.xml")
async def show_feed(show_id: str) -> Response:
    """Serve a show-specific RSS podcast feed."""
    _show_states = _get_show_states()
    if show_id not in _show_states:
        return Response(content="Show not found.", status_code=404)
    state = _show_states[show_id]
    feed_path = state.show.feed_path
    if not feed_path.exists():
        return Response(content="Feed not yet generated.", status_code=404)
    return FileResponse(feed_path, media_type="application/rss+xml")


@router.get("/episodes/{filename}")
async def episode(filename: str, request: Request) -> Response:
    """Serve an episode MP3 file (default show, backward compat)."""
    state = _get_resolve_show()("")
    return _serve_episode(state.show.episodes_dir, filename, request)


@router.get("/{show_id}/episodes/{filename}")
async def show_episode(show_id: str, filename: str, request: Request) -> Response:
    """Serve a show-specific episode MP3 file."""
    _show_states = _get_show_states()
    if show_id not in _show_states:
        return Response(content="Show not found.", status_code=404)
    state = _show_states[show_id]
    return _serve_episode(state.show.episodes_dir, filename, request)


def _serve_episode(episodes_dir: Path, filename: str, request: Request) -> Response:
    """Serve an episode MP3 file with range request support."""
    file_path = episodes_dir / filename

    if ".." in filename or "/" in filename:
        return Response(content="Invalid filename.", status_code=400)

    if not file_path.exists():
        return Response(content="Episode not found.", status_code=404)

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        range_str = range_header.replace("bytes=", "")
        parts = range_str.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else file_size - 1

        if start >= file_size:
            return Response(
                content="Range not satisfiable",
                status_code=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        end = min(end, file_size - 1)
        content_length = end - start + 1

        with open(file_path, "rb") as f:
            f.seek(start)
            data = f.read(content_length)

        return Response(
            content=data,
            status_code=206,
            media_type="audio/mpeg",
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
            },
        )

    return FileResponse(
        file_path,
        media_type="audio/mpeg",
        headers={"Accept-Ranges": "bytes"},
    )
