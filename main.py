"""FastAPI app — serves RSS feed, audio files, and dashboard."""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import LOCAL_TZ, ShowConfig, is_dev, is_prod, settings, shows
from src import database, feed_builder, gcs_storage
from src.models import CompiledDigest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Backward-compat alias — tests import PST from main
PST = LOCAL_TZ


def _pst_now() -> datetime:
    """Return the current datetime in local Seattle time (PST/PDT)."""
    return datetime.now(LOCAL_TZ)



# --- Per-show state ---

@dataclass
class ShowState:
    """Mutable per-show state for the preparation workflow."""

    show: ShowConfig
    generation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation_running: bool = False
    preparation_active: bool = False
    preparation_date: str | None = None
    preparation_cancelled: bool = False
    preparation_digest: CompiledDigest | None = None
    preparation_error: str | None = None


# Registry: populated during lifespan startup
_show_states: dict[str, ShowState] = {}
_next_scheduled_run: datetime | None = None


def _resolve_show(show_id: str = "") -> ShowState:
    """Resolve a show_id to its ShowState. Defaults to the first show."""
    if show_id and show_id in _show_states:
        return _show_states[show_id]
    # Default to the first configured show
    return next(iter(_show_states.values()))


def _calc_next_run() -> datetime:
    """Calculate the next scheduled run time based on generation_hour and generation_minute."""
    now = datetime.now(UTC)
    target = now.replace(hour=settings.generation_hour, minute=settings.generation_minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _episode_date_for_latest_run() -> str:
    """Get the episode date (PST) that the most recent scheduled run would produce."""
    now_utc = datetime.now(UTC)
    latest_run = now_utc.replace(
        hour=settings.generation_hour, minute=settings.generation_minute,
        second=0, microsecond=0,
    )
    if latest_run > now_utc:
        latest_run -= timedelta(days=1)
    return latest_run.astimezone(PST).strftime("%Y-%m-%d")


def _today_digest_exists(state: ShowState) -> bool:
    """Check if a digest for the most recent episode date already exists."""
    return database.get_digest(_episode_date_for_latest_run(), db_path=state.show.db_path) is not None


def _missed_todays_run(state: ShowState) -> bool:
    """Return True if the scheduled time already passed today and no digest exists yet."""
    now = datetime.now(UTC)
    target = now.replace(hour=settings.generation_hour, minute=settings.generation_minute, second=0, microsecond=0)
    return now > target and not _today_digest_exists(state)


async def _run_generation(state: ShowState) -> None:
    """Run digest preparation (steps 1-3), guarded by a lock."""
    from generate import generate_digest_only

    show = state.show

    if state.generation_lock.locked():
        logger.warning("[%s] Generation already in progress, skipping.", show.show_id)
        return

    async with state.generation_lock:
        state.generation_running = True

        if not state.preparation_active:
            state.preparation_active = True
            state.preparation_date = datetime.now(PST).strftime("%Y-%m-%d")
            state.preparation_digest = None
            state.preparation_error = None
            stale_prep = show.episodes_dir / f"noctua-{state.preparation_date}.prep.mp3"
            if stale_prep.exists():
                stale_prep.unlink()
                logger.info("[%s] Removed stale prep file: %s", show.show_id, stale_prep.name)

        try:
            def _run_sync():
                return asyncio.run(generate_digest_only(show=show, save_to_db=False))

            result = await asyncio.to_thread(_run_sync)

            if state.preparation_cancelled:
                state.preparation_digest = None
                state.preparation_error = None
                logger.info("[%s] Preparation cancelled — discarded in-memory digest", show.show_id)
            elif result is None:
                state.preparation_digest = None
                state.preparation_error = "No newsletters found — nothing to prepare."
                logger.info("[%s] Preparation returned no digest (no emails/articles)", show.show_id)
            else:
                state.preparation_digest = result
                state.preparation_error = None
                logger.info("[%s] Preparation digest ready (in-memory only)", show.show_id)
        except Exception as e:
            logger.error("[%s] Digest preparation failed: %s", show.show_id, e)
            state.preparation_error = f"Generation failed: {e}"
        finally:
            state.generation_running = False
            state.preparation_cancelled = False



async def _scheduler() -> None:
    """Background fallback scheduler (in case external cron misses)."""
    global _next_scheduled_run
    while True:
        _next_scheduled_run = _calc_next_run()
        wait_seconds = (_next_scheduled_run - datetime.now(UTC)).total_seconds()
        logger.info(
            "Scheduler: next generation at %s UTC (in %.0f minutes)",
            _next_scheduled_run.strftime("%Y-%m-%d %H:%M"),
            wait_seconds / 60,
        )
        await asyncio.sleep(max(wait_seconds, 0))
        logger.info("Scheduler: triggering daily generation for all shows.")
        for state in _show_states.values():
            asyncio.create_task(_run_generation(state))
        # Weekly trend analysis on Sundays
        if datetime.now(UTC).weekday() == 6:  # Sunday
            asyncio.create_task(_run_weekly_trends())


async def _run_weekly_trends() -> None:
    """Run weekly trend analysis for all shows."""
    from src.episode_analyzer import run_weekly_trend_analysis
    loop = asyncio.get_event_loop()
    for state in _show_states.values():
        try:
            result = await loop.run_in_executor(
                None, run_weekly_trend_analysis, str(state.show.db_path),
            )
            if result:
                # NOTE: In dev, this does NOT sync to GCS. Set NOCTUA_ENV=prod to persist.
                gcs_storage.upload_db(state.show.db_path, state.show.show_id)
                logger.info("Weekly trends for %s: %d suggestions", state.show.show_id, len(result))
        except Exception as e:
            logger.error("Weekly trend analysis failed for %s: %s", state.show.show_id, e)


async def _deferred_startup():
    """Download DBs from GCS and sync feeds in background after app starts serving."""
    def _init_shows():
        for state in _show_states.values():
            show = state.show
            try:
                gcs_storage.download_db(show.db_path, show.show_id)
                feed_builder.sync_catalog_from_db(show=show)
                logger.info("[%s] Startup init complete.", show.show_id)
            except Exception as e:
                logger.warning("[%s] Non-fatal startup error: %s", show.show_id, e)

    await asyncio.to_thread(_init_shows)

    # Check for missed runs after DB is loaded
    for state in _show_states.values():
        if _missed_todays_run(state):
            logger.info("[%s] Startup: missed today's scheduled run — triggering now.", state.show.show_id)
            asyncio.create_task(_run_generation(state))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the background scheduler and check for missed runs on startup."""
    # Startup banner — make environment crystal clear
    if is_prod():
        logger.info("Running in PROD mode — GCS writes enabled")
    else:
        logger.warning("Running in DEV mode — GCS writes disabled. "
                        "Set NOCTUA_ENV=prod to enable GCS persistence.")

    # Initialize per-show state
    for show_id, show in shows.items():
        _show_states[show_id] = ShowState(show=show)

    # Ensure output directories exist (fast, non-blocking)
    for state in _show_states.values():
        state.show.episodes_dir.mkdir(parents=True, exist_ok=True)
        state.show.exports_dir.mkdir(parents=True, exist_ok=True)

    # Start GCS download and feed sync in background (don't block health checks)
    init_task = asyncio.create_task(_deferred_startup())

    task = asyncio.create_task(_scheduler())
    logger.info("Background scheduler started (%02d:%02d UTC). Shows: %s",
                settings.generation_hour, settings.generation_minute,
                ", ".join(_show_states.keys()))
    yield
    task.cancel()
    init_task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="The Hootline", description="Daily podcast generator", lifespan=lifespan)

# --- Routers ---
from routers.learning import router as learning_router
from routers.episodes import router as episodes_router
from routers.digests import router as digests_router
from routers.pipeline import router as pipeline_router
from routers.dashboard import router as dashboard_router
app.include_router(learning_router)
app.include_router(episodes_router)
app.include_router(digests_router)
app.include_router(pipeline_router)
app.include_router(dashboard_router)

# Serve static assets (cover image, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")



async def _transcribe_episode_background(date: str, mp3_path: Path, show: ShowConfig) -> None:
    """Run audio transcription in background, save results to DB, then run learning analysis."""
    from src.audio_transcriber import transcribe_episode
    try:
        database.set_audio_analysis_status(date, "running", db_path=show.db_path)
        segment_order = show.format.segment_order
        segment_durations = show.format.segment_durations
        loop = asyncio.get_event_loop()
        analysis = await loop.run_in_executor(
            None, lambda: transcribe_episode(mp3_path, segment_order, segment_durations, db_path=show.db_path)
        )
        # Extract word_counts for backward compat, store full analysis
        word_counts = analysis.get("word_counts", analysis)
        database.update_audio_analysis(
            date, word_counts, audio_analysis_full=analysis, db_path=show.db_path,
        )
        # NOTE: In dev, this does NOT sync to GCS. Set NOCTUA_ENV=prod to persist.
        gcs_storage.upload_db(show.db_path, show.show_id)
        logger.info("Audio transcription complete for %s: %d topics, %d gaps, %d tone findings",
                     date, sum(1 for v in word_counts.values() if v > 0),
                     len(analysis.get("coverage_gaps", [])),
                     len(analysis.get("tone_findings", [])))

        # Run learning system analysis
        try:
            from src.episode_analyzer import analyze_episode
            quality_report = database.get_quality_report(date, db_path=show.db_path)
            learn_result = await loop.run_in_executor(
                None, analyze_episode, date, analysis, quality_report, str(show.db_path),
            )
            # NOTE: In dev, this does NOT sync to GCS. Set NOCTUA_ENV=prod to persist.
            gcs_storage.upload_db(show.db_path, show.show_id)
            logger.info("Learning analysis complete for %s: %d findings, %d suggestions",
                        date, learn_result["findings_count"], learn_result["suggestions_count"])
        except Exception as e:
            logger.error("Learning analysis failed for %s: %s", date, e)

    except Exception as e:
        logger.error("Audio transcription failed for %s: %s", date, e)
        database.set_audio_analysis_status(date, "failed", db_path=show.db_path)



