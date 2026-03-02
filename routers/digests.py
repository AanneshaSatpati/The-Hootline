"""Digest routes — digests, coverage, history, export, prompt config."""

import io
import json
import logging
import re
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import requests
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask

from src import database, gcs_storage

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


# --- Prompt config ---

@router.get("/api/prompt-config")
async def api_get_prompt_config(show_id: str = Query(default="")):
    """Get the current prompt configuration."""
    from src.digest_compiler import get_prompt_config
    state = _get_resolve_show()(show_id)
    config = get_prompt_config(state.show)
    return JSONResponse(config)


@router.post("/api/prompt-config")
async def api_save_prompt_config(request: Request, show_id: str = Query(default="")):
    """Save updated prompt configuration."""
    from src.digest_compiler import save_prompt_config
    state = _get_resolve_show()(show_id)
    body = await request.json()
    config = {
        "system_prompt": body.get("system_prompt", "").strip(),
        "podcast_preamble": body.get("podcast_preamble", "").strip(),
    }
    if not config["system_prompt"] or not config["podcast_preamble"]:
        return JSONResponse({"error": "Both prompts are required."}, status_code=400)
    save_prompt_config(config, state.show)
    return JSONResponse({"ok": True})


# --- Digest listing ---

@router.get("/api/digests")
async def api_digests(show_id: str = Query(default="")):
    """List all digests."""
    state = _get_resolve_show()(show_id)
    return JSONResponse(database.list_digests(db_path=state.show.db_path))


@router.get("/api/digests/{date}")
async def api_digest(date: str, show_id: str = Query(default="")):
    """Get a single digest by date."""
    state = _get_resolve_show()(show_id)
    digest = database.get_digest(date, db_path=state.show.db_path)
    if not digest:
        return JSONResponse({"error": "Digest not found"}, status_code=404)
    return JSONResponse(digest)


# --- Topic coverage ---

@router.get("/api/topic-coverage")
async def api_topic_coverage(
    mode: str = Query("cumulative"),
    published_only: bool = Query(False),
    show_id: str = Query(default=""),
):
    """Radar chart data: target vs actual topic coverage."""
    state = _get_resolve_show()(show_id)
    db_path = state.show.db_path
    fmt = state.show.format

    # Use per-show segment config
    show_segment_order = fmt.segment_order
    duration_map = fmt.segment_durations

    prep_digest_data = None
    if state.preparation_active and state.preparation_digest and not published_only:
        prep_digest_data = {
            "date": state.preparation_digest.date,
            "segment_counts": state.preparation_digest.segment_counts or {},
            "segment_sources": state.preparation_digest.segment_sources or {},
        }

    if mode == "latest" and prep_digest_data:
        digests = [prep_digest_data]
    else:
        limit = 1 if mode == "latest" else 30
        digests = database.get_topic_coverage(limit=limit, published_only=published_only, db_path=db_path)
        if mode == "cumulative" and prep_digest_data:
            digests = [prep_digest_data] + digests

    totals: dict[str, int] = {}
    all_sources: dict[str, set[str]] = {}
    for d in digests:
        for topic_name, count in d["segment_counts"].items():
            totals[topic_name] = totals.get(topic_name, 0) + count
        for topic_name, sources in d.get("segment_sources", {}).items():
            if topic_name not in all_sources:
                all_sources[topic_name] = set()
            all_sources[topic_name].update(sources)
    grand_total = sum(totals.values())
    has_data = grand_total > 0

    num_digests = max(len(digests), 1)
    topics = []
    for name in show_segment_order:
        mins = duration_map.get(name, 1)
        capacity = max(2, round(mins * 1.5))
        actual_articles = totals.get(name, 0)
        if has_data:
            avg_articles = actual_articles / num_digests
            actual_pct = min(avg_articles / capacity, 1.0) * 100
            raw_pct = (avg_articles / capacity) * 100
        else:
            actual_pct = 0
            raw_pct = 0
        label = f"{name} ({mins}m)"
        topics.append({
            "name": label,
            "target_pct": 100,
            "actual_pct": round(actual_pct, 1),
            "raw_pct": round(raw_pct, 1),
            "actual_articles": actual_articles,
            "allocated_min": mins,
            "capacity": capacity,
        })

    suggestions = []
    if has_data:
        for topic_name, t in zip(show_segment_order, topics):
            topic_sources = sorted(all_sources.get(topic_name, []))
            if t["actual_pct"] < 30:
                suggestions.append({
                    "topic": t["name"],
                    "action": "subscribe",
                    "reason": f"Only {t['actual_pct']:.0f}% filled — consider adding sources",
                })
            elif t["raw_pct"] > 200:
                src_list = ", ".join(topic_sources) if topic_sources else "unknown"
                suggestions.append({
                    "topic": t["name"],
                    "action": "unsubscribe",
                    "reason": f"{t['raw_pct']:.0f}% incoming vs capacity — content being discarded. Sources: {src_list}",
                })

    return JSONResponse({
        "topics": topics,
        "suggestions": suggestions,
        "digests_analyzed": len(digests),
        "total_articles": sum(totals.values()),
        "has_data": has_data,
        "mode": mode,
    })


@router.get("/api/topic-coverage-3d")
async def api_topic_coverage_3d(show_id: str = Query(default="")):
    """Per-episode topic coverage data for the 3D visualization."""
    state = _get_resolve_show()(show_id)
    db_path = state.show.db_path
    fmt = state.show.format
    duration_map = fmt.segment_durations
    segment_order = fmt.segment_order

    digests = database.get_topic_coverage(limit=100, published_only=True, db_path=db_path)
    digests.reverse()  # oldest first

    episodes = []
    for d in digests:
        coverage = []
        for name in segment_order:
            mins = duration_map.get(name, 1)
            capacity = max(2, round(mins * 1.5))
            count = d["segment_counts"].get(name, 0)
            ratio = round(min(count / capacity, 1.3), 3) if capacity else 0
            coverage.append(ratio)
        episodes.append({
            "date": d["date"],
            "coverage": coverage,
        })

    topics = []
    for name in segment_order:
        topics.append({"name": name, "alloc": duration_map.get(name, 1)})

    return JSONResponse({"topics": topics, "episodes": episodes})


def _parse_segment_words(markdown_text: str, segment_order: list[str]) -> dict[str, int]:
    """Parse digest markdown to count words per topic segment.

    Expected format: ## SEGMENT N: TopicName (~X minutes)
    """
    result = {name: 0 for name in segment_order}
    parts = re.split(r'## SEGMENT \d+:\s*(.+?)(?:\s*\(~\d+\s*minutes?\))?\s*\n', markdown_text)
    for i in range(1, len(parts) - 1, 2):
        topic_name = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        body = re.sub(r'\*\*Word budget:.*?\*\*', '', body)
        body = body.replace('---', '')
        word_count = len(body.split())
        for name in segment_order:
            if name.lower() == topic_name.lower():
                result[name] = word_count
                break
    return result


@router.get("/api/coverage-dashboard")
async def api_coverage_dashboard(show_id: str = Query(default="")):
    """Per-episode word-level coverage data for the coverage dashboard."""
    state = _get_resolve_show()(show_id)
    db_path = state.show.db_path
    fmt = state.show.format
    segment_order = fmt.segment_order
    duration_map = fmt.segment_durations
    words_per_minute = 150

    digests = database.get_digest_coverage_detail(
        limit=100, published_only=True, db_path=db_path
    )

    # Load audio analysis data keyed by date
    audio_eps = database.get_episodes_with_audio(limit=200, db_path=db_path)
    audio_by_date = {}
    for ep in audio_eps:
        audio_by_date[ep["date"]] = {
            "words": json.loads(ep.get("audio_segment_words") or "{}"),
            "status": ep.get("audio_analysis_status", "none"),
        }

    episodes_out = []
    for d in digests:
        segment_words = _parse_segment_words(d["markdown_text"], segment_order)

        # Digest coverage (from markdown text)
        digest_coverage = {}
        for topic_name in segment_order:
            target_mins = duration_map.get(topic_name, 1)
            target_words = target_mins * words_per_minute
            actual_words = segment_words.get(topic_name, 0)
            pct = round(actual_words / target_words, 4) if target_words > 0 else 0
            digest_coverage[topic_name] = {
                "pct": pct,
                "actual_words": actual_words,
                "target_words": target_words,
            }

        # Audio coverage (from transcription analysis)
        audio_info = audio_by_date.get(d["date"], {"words": {}, "status": "none"})
        audio_words = audio_info["words"]
        audio_coverage = {}
        for topic_name in segment_order:
            target_mins = duration_map.get(topic_name, 1)
            target_words = target_mins * words_per_minute
            actual_words = audio_words.get(topic_name, 0)
            pct = round(actual_words / target_words, 4) if target_words > 0 else 0
            audio_coverage[topic_name] = {
                "pct": pct,
                "actual_words": actual_words,
                "target_words": target_words,
            }

        episodes_out.append({
            "date": d["date"],
            "total_words": d["total_words"],
            "coverage": digest_coverage,
            "digest_coverage": digest_coverage,
            "audio_coverage": audio_coverage,
            "audio_status": audio_info["status"],
        })

    topics = []
    for name in segment_order:
        mins = duration_map.get(name, 1)
        topics.append({
            "name": name,
            "alloc_min": mins,
            "target_words": mins * words_per_minute,
        })

    return JSONResponse({
        "topics": topics,
        "episodes": episodes_out,
    })


# --- History ---

@router.get("/api/history")
async def api_history(show_id: str = Query(default="")):
    """Combined digest + episode history for the History tab."""
    state = _get_resolve_show()(show_id)
    show = state.show
    db_path = show.db_path
    episodes_dir = show.episodes_dir
    is_legacy = show.is_legacy

    digests = database.list_digests_with_char_count(limit=100, db_path=db_path)
    episodes_list = database.list_episodes(db_path=db_path)
    ep_by_date = {ep["date"]: ep for ep in episodes_list}
    digest_dates = set()

    rows = []
    for d in digests:
        digest_dates.add(d["date"])
        ep = ep_by_date.get(d["date"])
        gcs_url = ep.get("gcs_url", "") if ep else ""
        local_file = episodes_dir / f"noctua-{d['date']}.mp3"
        has_audio = bool(gcs_url) or local_file.exists()
        rows.append({
            "date": d["date"],
            "article_count": d["article_count"],
            "total_words": d["total_words"],
            "total_chars": d.get("total_chars", 0),
            "email_count": d.get("email_count", 0),
            "topics_summary": d["topics_summary"],
            "has_digest": True,
            "has_audio": has_audio,
            "duration_formatted": ep["duration_formatted"] if ep else None,
            "file_size_bytes": ep["file_size_bytes"] if ep else None,
            "rss_summary": ep.get("rss_summary", "") if ep else "",
            "gcs_url": gcs_url,
        })

    # Include episodes that have no matching digest (e.g. digest lost during redeploy)
    for ep in episodes_list:
        if ep["date"] not in digest_dates:
            gcs_url = ep.get("gcs_url", "")
            local_file = episodes_dir / f"noctua-{ep['date']}.mp3"
            has_audio = bool(gcs_url) or local_file.exists()
            rows.append({
                "date": ep["date"],
                "article_count": 0,
                "total_words": 0,
                "total_chars": 0,
                "email_count": 0,
                "topics_summary": ep.get("topics_summary", ""),
                "has_digest": False,
                "has_audio": has_audio,
                "duration_formatted": ep["duration_formatted"],
                "file_size_bytes": ep["file_size_bytes"],
                "rss_summary": ep.get("rss_summary", ""),
                "gcs_url": gcs_url,
            })

    rows.sort(key=lambda r: r["date"], reverse=True)
    return JSONResponse({"rows": rows, "total": len(rows)})


# --- Export ---

@router.get("/api/export-episodes")
def api_export_episodes(show_id: str = Query(default="")):
    """Bundle all episode MP3s and digests into a ZIP for download."""
    state = _get_resolve_show()(show_id)
    show = state.show
    episodes_dir = show.episodes_dir
    exports_dir = show.exports_dir

    episodes_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)

    zip_name = f"{show.show_id}-all-episodes.zip"
    zip_path = exports_dir / zip_name

    # Serve cached ZIP if it exists and is less than 1 hour old
    if zip_path.exists():
        age_seconds = time.time() - zip_path.stat().st_mtime
        if age_seconds < 3600:
            logger.info("Serving cached export ZIP (age: %ds)", int(age_seconds))
            return FileResponse(
                zip_path,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
            )

    # Download any missing MP3s from GCS before zipping
    all_episodes = database.list_episodes(db_path=show.db_path)
    if not all_episodes:
        return JSONResponse({"error": "No episodes found."}, status_code=404)

    for ep in all_episodes:
        mp3_name = f"noctua-{ep['date']}.mp3"
        local_mp3 = episodes_dir / mp3_name
        if not local_mp3.exists() and ep.get("gcs_url"):
            try:
                logger.info("Export: downloading %s from GCS...", mp3_name)
                resp = requests.get(ep["gcs_url"], timeout=300)
                resp.raise_for_status()
                local_mp3.write_bytes(resp.content)
            except Exception as e:
                logger.warning("Export: failed to download %s: %s", mp3_name, e)

    mp3_files = sorted(episodes_dir.glob("noctua-*.mp3"))
    if not mp3_files:
        return JSONResponse({"error": "No episodes found (download failed)."}, status_code=404)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for mp3 in mp3_files:
            zf.write(mp3, mp3.name)
        # Add all digests
        all_digests = database.list_digests(limit=9999, db_path=show.db_path)
        for d in all_digests:
            full = database.get_digest(d["date"], db_path=show.db_path)
            if full and full["markdown_text"]:
                zf.writestr(f"noctua-digest-{d['date']}.md", full["markdown_text"])

    return FileResponse(
        zip_path,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@router.get("/api/export-weeks")
async def api_export_weeks(show_id: str = Query(default="")):
    """List all pending (un-downloaded) weekly ZIPs."""
    state = _get_resolve_show()(show_id)
    exports_dir = state.show.exports_dir
    prefix = state.show.show_id

    if not exports_dir.exists():
        return JSONResponse([])
    zips = sorted(exports_dir.glob(f"{prefix}-W*.zip"))
    result = []
    for z in zips:
        label = z.stem.removeprefix(f"{prefix}-")
        stat = z.stat()
        created = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
        result.append({
            "filename": z.name,
            "size_bytes": stat.st_size,
            "week_label": label,
            "created_at": created,
        })
    return JSONResponse(result)


@router.get("/api/download-export/{filename}")
async def api_download_export(filename: str, show_id: str = Query(default="")):
    """Download a specific weekly ZIP and delete it afterward."""
    if ".." in filename or "/" in filename:
        return Response(content="Invalid filename.", status_code=400)

    state = _get_resolve_show()(show_id)
    zip_path = state.show.exports_dir / filename
    if not zip_path.exists():
        return JSONResponse({"error": "Export not found."}, status_code=404)

    logger.info("Serving export (will delete after): %s", filename)
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(lambda p: p.unlink(missing_ok=True), zip_path),
    )


# --- Digest views (HTML & Markdown downloads) ---

def _md_to_html(markdown_text: str) -> str:
    """Convert markdown text to simple HTML (no external dependencies)."""
    import html as html_mod
    lines = markdown_text.split("\n")
    out: list[str] = []
    in_paragraph = False

    for line in lines:
        stripped = line.strip()

        # Horizontal rule
        if stripped in ("---", "***", "___"):
            if in_paragraph:
                out.append("</p>")
                in_paragraph = False
            out.append("<hr>")
            continue

        # Headings
        if stripped.startswith("#"):
            if in_paragraph:
                out.append("</p>")
                in_paragraph = False
            level = 0
            for ch in stripped:
                if ch == "#":
                    level += 1
                else:
                    break
            level = min(level, 6)
            text = html_mod.escape(stripped[level:].strip())
            # Bold in headings
            text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
            out.append(f"<h{level}>{text}</h{level}>")
            continue

        # Empty line — close paragraph
        if not stripped:
            if in_paragraph:
                out.append("</p>")
                in_paragraph = False
            continue

        # Bold/italic inline
        escaped = html_mod.escape(stripped)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"\*(.+?)\*", r"<em>\1</em>", escaped)

        if not in_paragraph:
            out.append("<p>")
            in_paragraph = True
        else:
            out.append("<br>")
        out.append(escaped)

    if in_paragraph:
        out.append("</p>")

    return "\n".join(out)


def _render_digest_html(digest: dict, show_title: str) -> str:
    """Render a digest as a styled HTML page."""
    date = digest["date"]
    content_html = _md_to_html(digest["markdown_text"])
    topics = digest.get("topics_summary", "")
    article_count = digest.get("article_count", 0)
    total_words = digest.get("total_words", 0)

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{show_title} — {date}</title>
<style>
  :root {{
    --bg: #0f1117;
    --surface: #1a1d27;
    --border: #2e3140;
    --text: #e4e4e7;
    --text-dim: #8b8d98;
    --accent: #c4a052;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
    background: var(--bg); color: var(--text);
    min-height: 100vh; padding: 0;
  }}
  header {{
    border-bottom: 1px solid var(--border);
    padding: 16px 24px;
    display: flex; align-items: center; justify-content: space-between;
  }}
  header h1 {{ font-size: 18px; font-weight: 600; color: var(--accent); letter-spacing: 2px; }}
  header .meta {{ font-size: 11px; color: var(--text-dim); }}
  .container {{
    max-width: 820px; margin: 0 auto; padding: 32px 24px;
  }}
  .stats {{
    font-size: 12px; color: var(--text-dim); margin-bottom: 24px;
    padding-bottom: 16px; border-bottom: 1px solid var(--border);
  }}
  .content h1 {{ font-size: 22px; font-weight: 700; color: var(--accent); margin: 28px 0 12px; }}
  .content h2 {{ font-size: 18px; font-weight: 600; color: var(--accent); margin: 24px 0 10px; }}
  .content h3 {{ font-size: 15px; font-weight: 600; color: var(--text); margin: 20px 0 8px; }}
  .content h4, .content h5, .content h6 {{ font-size: 13px; font-weight: 600; color: var(--text-dim); margin: 16px 0 6px; }}
  .content p {{ font-size: 13px; line-height: 1.7; margin-bottom: 12px; color: var(--text); }}
  .content strong {{ color: var(--accent); }}
  .content em {{ color: var(--text-dim); font-style: italic; }}
  .content hr {{ border: none; border-top: 1px solid var(--border); margin: 20px 0; }}
  footer {{
    border-top: 1px solid var(--border);
    padding: 16px 24px; text-align: center;
    font-size: 11px; color: var(--text-dim);
  }}
</style>
</head>
<body>
  <header>
    <h1>{show_title}</h1>
    <span class="meta">{date}</span>
  </header>
  <div class="container">
    <div class="stats">{article_count} articles &middot; {total_words:,} words &middot; {topics}</div>
    <div class="content">
      {content_html}
    </div>
  </div>
  <footer>Generated by Noctua</footer>
</body>
</html>"""


@router.get("/digests/{date}.html")
async def digest_html_view(date: str, show_id: str = Query(default="")) -> Response:
    """Serve a digest as a styled HTML page (legacy route)."""
    if ".." in date or "/" in date:
        return Response(content="Invalid date.", status_code=400)
    state = _get_resolve_show()(show_id)
    digest = database.get_digest(date, db_path=state.show.db_path)
    if not digest:
        return Response(content="Digest not found.", status_code=404)
    html = _render_digest_html(digest, state.show.podcast_title)
    return HTMLResponse(content=html)


@router.get("/{show_id}/digests/{date}.html")
async def show_digest_html_view(show_id: str, date: str) -> Response:
    """Serve a show-specific digest as a styled HTML page."""
    if ".." in date or "/" in date:
        return Response(content="Invalid date.", status_code=400)
    _show_states = _get_show_states()
    if show_id not in _show_states:
        return Response(content="Show not found.", status_code=404)
    state = _show_states[show_id]
    digest = database.get_digest(date, db_path=state.show.db_path)
    if not digest:
        return Response(content="Digest not found.", status_code=404)
    html = _render_digest_html(digest, state.show.podcast_title)
    return HTMLResponse(content=html)


@router.get("/digests/{date}.md")
async def digest_download(date: str, show_id: str = Query(default="")) -> Response:
    """Serve a digest as a downloadable .md file (legacy route)."""
    if ".." in date or "/" in date:
        return Response(content="Invalid date.", status_code=400)
    state = _get_resolve_show()(show_id)
    digest = database.get_digest(date, db_path=state.show.db_path)
    if not digest:
        return Response(content="Digest not found.", status_code=404)
    return Response(
        content=digest["markdown_text"],
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="noctua-digest-{date}.md"'},
    )


@router.get("/{show_id}/digests/{date}.md")
async def show_digest_download(show_id: str, date: str) -> Response:
    """Serve a show-specific digest as a downloadable .md file."""
    if ".." in date or "/" in date:
        return Response(content="Invalid date.", status_code=400)
    _show_states = _get_show_states()
    if show_id not in _show_states:
        return Response(content="Show not found.", status_code=404)
    state = _show_states[show_id]
    digest = database.get_digest(date, db_path=state.show.db_path)
    if not digest:
        return Response(content="Digest not found.", status_code=404)
    return Response(
        content=digest["markdown_text"],
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="noctua-digest-{date}.md"'},
    )
