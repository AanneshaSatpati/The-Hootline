"""Pipeline routes — cron trigger, runs, preparation workflow, health checks."""

import asyncio
import logging
import shutil
from datetime import datetime

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from config import settings
from src import database
from src.episode_manager import _ffmpeg_path

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_resolve_show():
    """Late import to avoid circular dependency."""
    from main import _resolve_show
    return _resolve_show


def _get_show_states():
    """Late import to avoid circular dependency."""
    from main import _show_states
    return _show_states


def _get_run_generation():
    """Late import to avoid circular dependency."""
    from main import _run_generation
    return _run_generation


# --- Health ---

@router.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    from main import _next_scheduled_run
    _show_states = _get_show_states()
    any_running = any(s.generation_running for s in _show_states.values())
    return {
        "status": "ok",
        "generation_running": any_running,
        "next_scheduled_run": _next_scheduled_run.isoformat() if _next_scheduled_run else None,
        "generation_schedule_utc": f"{settings.generation_hour:02d}:{settings.generation_minute:02d}",
        "shows": list(_show_states.keys()),
    }


@router.get("/health/detail")
async def health_detail() -> dict:
    """Detailed health check with file system and database stats."""
    from main import _next_scheduled_run
    _show_states = _get_show_states()
    show_details = {}
    for sid, state in _show_states.items():
        show = state.show
        ep_count = len(list(show.episodes_dir.glob("noctua-*.mp3"))) if show.episodes_dir.exists() else 0
        show_details[sid] = {
            "episodes": ep_count,
            "digests": len(database.list_digests(db_path=show.db_path)),
            "feed_exists": show.feed_path.exists(),
            "generation_running": state.generation_running,
        }

    return {
        "status": "ok",
        "shows": show_details,
        "next_scheduled_run": _next_scheduled_run.isoformat() if _next_scheduled_run else None,
        "generation_schedule_utc": f"{settings.generation_hour:02d}:{settings.generation_minute:02d}",
        "ffmpeg": _ffmpeg_path(),
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
    }


# --- Runs ---

@router.get("/api/runs")
async def api_runs(show_id: str = Query(default="")):
    """List pipeline runs."""
    state = _get_resolve_show()(show_id)
    return JSONResponse(database.list_runs(db_path=state.show.db_path))


@router.get("/api/runs/{run_id}")
async def api_run(run_id: str, show_id: str = Query(default="")):
    """Get a single pipeline run."""
    state = _get_resolve_show()(show_id)
    run = database.get_run(run_id, db_path=state.show.db_path)
    if not run:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    return JSONResponse(run)


# --- Cron & Preparation ---

@router.api_route("/api/cron/generate", methods=["GET", "POST"])
async def api_cron_generate(request: Request, secret: str = Query(""), show_id: str = Query(default="")):
    """External cron trigger for daily digest generation.

    When show_id is omitted, triggers all shows. When provided, triggers only that show.
    Accepts secret via query param or Authorization: Bearer header.
    """
    _show_states = _get_show_states()
    _run_generation = _get_run_generation()

    # Also accept secret via Authorization header (more secure than query string)
    if not secret:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            secret = auth_header[7:]

    if not settings.cron_secret:
        return JSONResponse(
            {"error": "CRON_SECRET not configured on server."},
            status_code=500,
        )
    if secret != settings.cron_secret:
        return JSONResponse({"error": "Invalid secret."}, status_code=403)

    if show_id:
        if show_id not in _show_states:
            return JSONResponse({"error": f"Unknown show: {show_id}"}, status_code=404)
        state = _show_states[show_id]
        if state.generation_lock.locked():
            return JSONResponse(
                {"status": "already_running", "message": f"Generation already in progress for {show_id}."},
                status_code=409,
            )
        logger.info("Cron trigger: starting generation for %s.", show_id)
        asyncio.create_task(_run_generation(state))
        return JSONResponse({"status": "started", "message": f"Generation started for {show_id} via cron."})

    # Trigger all shows
    started = []
    skipped = []
    for sid, state in _show_states.items():
        if state.generation_lock.locked():
            skipped.append(sid)
        else:
            asyncio.create_task(_run_generation(state))
            started.append(sid)

    logger.info("Cron trigger: started=%s, skipped=%s", started, skipped)
    return JSONResponse({
        "status": "started",
        "message": f"Generation started via cron.",
        "started": started,
        "skipped": skipped,
    })


@router.post("/api/start-preparation")
async def api_start_preparation(show_id: str = Query(default="")):
    """Start the preparation workflow: generate a new digest."""
    from main import PST
    _run_generation = _get_run_generation()
    state = _get_resolve_show()(show_id)

    today_str = datetime.now(PST).strftime("%Y-%m-%d")

    state.preparation_cancelled = False
    state.preparation_active = True
    state.preparation_date = today_str
    state.preparation_digest = None
    state.preparation_error = None

    if state.generation_lock.locked():
        return JSONResponse({
            "state": "generating",
            "date": today_str,
            "message": "Generation already in progress.",
        })

    asyncio.create_task(_run_generation(state))
    return JSONResponse({
        "state": "generating",
        "date": today_str,
        "message": "Digest preparation started.",
    })


@router.post("/api/cancel-preparation")
async def api_cancel_preparation(show_id: str = Query(default="")):
    """Cancel the preparation workflow."""
    state = _get_resolve_show()(show_id)

    if state.generation_running:
        state.preparation_cancelled = True
        logger.info("[%s] Preparation cancel requested.", state.show.show_id)

    if state.preparation_date:
        prep_mp3 = state.show.episodes_dir / f"noctua-{state.preparation_date}.prep.mp3"
        prep_mp3.unlink(missing_ok=True)

    state.preparation_active = False
    state.preparation_date = None
    state.preparation_digest = None
    state.preparation_error = None

    return JSONResponse({"status": "ok", "message": "Preparation cancelled."})


@router.get("/api/preparation-digest")
async def api_preparation_digest(show_id: str = Query(default="")):
    """Serve the in-memory preparation digest as a downloadable .md file."""
    state = _get_resolve_show()(show_id)
    if not state.preparation_digest:
        return Response(content="No preparation digest available.", status_code=404)
    return Response(
        content=state.preparation_digest.text,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="noctua-digest-{state.preparation_digest.date}.md"'},
    )
