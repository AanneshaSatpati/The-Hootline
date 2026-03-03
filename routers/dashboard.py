"""Dashboard routes — serves the SPA dashboard and show discovery APIs."""

import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

BUILD_NUMBER = 1
BUILD_DATE = datetime.now(UTC).strftime("%b %d, %Y")
BUILD_VERSION = f"b{BUILD_NUMBER} · {BUILD_DATE}"

_TEMPLATE_PATH = Path("templates/dashboard.html")

router = APIRouter()


def _get_resolve_show():
    """Late import to avoid circular dependency."""
    from main import _resolve_show
    return _resolve_show


def _get_show_states():
    """Late import to avoid circular dependency."""
    from main import _show_states
    return _show_states


def _build_dashboard_html(show_id: str = "") -> str:
    """Build the dashboard HTML with the given show_id baked in."""
    _show_states = _get_show_states()

    if not _show_states:
        return "<html><body><h1>Starting up...</h1><p>Server is initializing. Please refresh in a few seconds.</p><script>setTimeout(()=>location.reload(),3000)</script></body></html>"

    _resolve_show = _get_resolve_show()

    if show_id and show_id in _show_states:
        show = _show_states[show_id].show
    else:
        show = _resolve_show("").show
        show_id = show.show_id
    title = show.podcast_title
    tagline = show.podcast_description

    html = _TEMPLATE_PATH.read_text()
    return (html
            .replace("__SHOW_ID__", show_id)
            .replace("__SHOW_TITLE__", title)
            .replace("__SHOW_TAGLINE__", tagline)
            .replace("__BUILD_VERSION__", BUILD_VERSION))


# --- Show discovery ---

@router.get("/api/shows")
async def api_shows():
    """List all configured shows."""
    _show_states = _get_show_states()
    return JSONResponse([
        {"id": s.show.show_id, "title": s.show.podcast_title}
        for s in _show_states.values()
    ])


@router.get("/api/show-format")
async def api_show_format(show_id: str = Query(default="")):
    """Return the segment format for a show (used by dashboard JS)."""
    state = _get_resolve_show()(show_id)
    fmt = state.show.format
    return JSONResponse({
        "segments": [{"name": name, "minutes": mins} for name, mins in fmt.segments],
        "intro_minutes": fmt.intro_minutes,
        "outro_minutes": fmt.outro_minutes,
        "total_minutes": fmt.total_minutes,
    })


# --- Dashboard ---

@router.get("/")
async def dashboard():
    """Root redirects to default show dashboard."""
    return HTMLResponse(
        content=_build_dashboard_html(),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/{show_id}/")
async def show_dashboard(show_id: str):
    """Show-specific dashboard."""
    _show_states = _get_show_states()
    if show_id not in _show_states:
        return Response(content="Show not found.", status_code=404)
    return HTMLResponse(
        content=_build_dashboard_html(show_id),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )
