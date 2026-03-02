"""Learning system API routes — /api/learning/*"""

import json
import logging

from fastapi import APIRouter, Form, Query
from fastapi.responses import JSONResponse

from src import database, gcs_storage

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_resolve_show():
    """Late import to avoid circular dependency."""
    from main import _resolve_show
    return _resolve_show


def _infer_prompt_key(suggestion: dict) -> str | None:
    """Infer which prompt_key a suggestion targets based on its content."""
    detail = (suggestion.get("detail", "") + " " + suggestion.get("title", "")).lower()
    if "digest" in detail and "system" in detail:
        return "digest_system"
    if "preamble" in detail:
        return "digest_preamble"
    if "transcription" in detail or "audio" in detail:
        if "system" in detail:
            return "transcription_system"
        return "transcription_user"
    if "digest" in detail:
        return "digest_system"
    return None


@router.get("/api/learning/episodes")
async def api_learning_episodes(show_id: str = Query("")):
    """List episode dates that have learning findings."""
    state = _get_resolve_show()(show_id)
    dates = database.get_episode_dates_with_findings(db_path=state.show.db_path)
    return JSONResponse({"dates": dates})


@router.get("/api/learning/episode/{date}")
async def api_learning_episode(date: str, show_id: str = Query("")):
    """Get findings + suggestions + audio analysis for an episode."""
    state = _get_resolve_show()(show_id)
    db = state.show.db_path
    findings = database.get_findings(date, db_path=db)
    # Get all pending suggestions plus this episode's suggestions
    ep_suggestions = database.get_suggestions(episode_date=date, db_path=db)
    pending_other = database.get_suggestions(status="pending", db_path=db)
    # Merge: ep_suggestions + pending from other dates (deduplicated)
    seen_ids = {s["id"] for s in ep_suggestions}
    for s in pending_other:
        if s["id"] not in seen_ids:
            ep_suggestions.append(s)
            seen_ids.add(s["id"])
    audio_analysis = database.get_audio_analysis_full(date, db_path=db)
    quality_report = database.get_quality_report(date, db_path=db)
    # Check egregious
    is_egregious = False
    if findings:
        from src.episode_analyzer import _check_egregious
        is_egregious = _check_egregious(audio_analysis, quality_report, findings)
    return JSONResponse({
        "date": date,
        "findings": findings,
        "suggestions": ep_suggestions,
        "audio_analysis": audio_analysis,
        "quality_report": quality_report,
        "is_egregious": is_egregious,
    })


@router.post("/api/learning/approve-suggestion")
async def api_learning_approve(suggestion_id: int = Form(0), show_id: str = Form("")):
    """Approve a suggestion. For prompt_edit, writes to prompt_overrides."""
    state = _get_resolve_show()(show_id)
    db = state.show.db_path
    suggestions = database.get_suggestions(db_path=db)
    suggestion = next((s for s in suggestions if s["id"] == suggestion_id), None)
    if not suggestion:
        return JSONResponse({"error": "Suggestion not found."}, status_code=404)
    if suggestion["status"] != "pending":
        return JSONResponse({"error": "Suggestion is not pending."}, status_code=400)
    database.update_suggestion_status(suggestion_id, "approved", db_path=db)
    # For prompt_edit, save to prompt_overrides
    if suggestion["type"] == "prompt_edit" and suggestion.get("suggested_value"):
        prompt_key = _infer_prompt_key(suggestion)
        if prompt_key:
            database.save_prompt_override(
                prompt_key=prompt_key,
                original_value=suggestion.get("current_value", ""),
                override_value=suggestion["suggested_value"],
                suggestion_id=suggestion_id,
                db_path=db,
            )
    # NOTE: In dev, this does NOT sync to GCS. Set NOCTUA_ENV=prod to persist.
    gcs_storage.upload_db(db, state.show.show_id)
    return JSONResponse({"status": "ok"})


@router.post("/api/learning/dismiss-suggestion")
async def api_learning_dismiss(suggestion_id: int = Form(0), show_id: str = Form("")):
    """Dismiss a suggestion."""
    state = _get_resolve_show()(show_id)
    updated = database.update_suggestion_status(suggestion_id, "dismissed", db_path=state.show.db_path)
    if not updated:
        return JSONResponse({"error": "Suggestion not found."}, status_code=404)
    # NOTE: In dev, this does NOT sync to GCS. Set NOCTUA_ENV=prod to persist.
    gcs_storage.upload_db(state.show.db_path, state.show.show_id)
    return JSONResponse({"status": "ok"})


@router.post("/api/learning/snooze-suggestion")
async def api_learning_snooze(suggestion_id: int = Form(0), show_id: str = Form("")):
    """Snooze a suggestion."""
    state = _get_resolve_show()(show_id)
    updated = database.update_suggestion_status(suggestion_id, "snoozed", db_path=state.show.db_path)
    if not updated:
        return JSONResponse({"error": "Suggestion not found."}, status_code=404)
    # NOTE: In dev, this does NOT sync to GCS. Set NOCTUA_ENV=prod to persist.
    gcs_storage.upload_db(state.show.db_path, state.show.show_id)
    return JSONResponse({"status": "ok"})


@router.get("/api/learning/prompt-overrides")
async def api_learning_overrides(show_id: str = Query("")):
    """Get current active prompt overrides."""
    state = _get_resolve_show()(show_id)
    overrides = database.get_prompt_overrides_full(db_path=state.show.db_path)
    return JSONResponse({"overrides": overrides})
