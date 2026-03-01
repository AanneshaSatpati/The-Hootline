"""SQLite database for storing daily digests and pipeline run logs."""

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("output/noctua.db")


def _get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Get a database connection, creating the DB and tables if needed."""
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            markdown_text TEXT NOT NULL,
            article_count INTEGER NOT NULL DEFAULT 0,
            total_words INTEGER NOT NULL DEFAULT 0,
            topics_summary TEXT NOT NULL DEFAULT '',
            rss_summary TEXT NOT NULL DEFAULT '',
            segment_counts TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            current_step TEXT,
            error_message TEXT,
            steps_log TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            file_size_bytes INTEGER NOT NULL,
            duration_seconds INTEGER NOT NULL,
            duration_formatted TEXT NOT NULL,
            topics_summary TEXT NOT NULL DEFAULT '',
            rss_summary TEXT NOT NULL DEFAULT '',
            gcs_url TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_date TEXT NOT NULL,
            job TEXT NOT NULL,
            severity TEXT NOT NULL,
            topic TEXT,
            finding TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_date TEXT NOT NULL,
            type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            title TEXT NOT NULL,
            detail TEXT NOT NULL,
            current_value TEXT,
            suggested_value TEXT,
            finding_ids TEXT NOT NULL DEFAULT '[]',
            reviewed_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prompt_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_key TEXT UNIQUE NOT NULL,
            original_value TEXT NOT NULL,
            override_value TEXT NOT NULL,
            approved_from_suggestion_id INTEGER,
            applied_at TEXT NOT NULL,
            FOREIGN KEY (approved_from_suggestion_id) REFERENCES suggestions(id)
        );

        CREATE INDEX IF NOT EXISTS idx_digests_date ON digests(date);
        CREATE INDEX IF NOT EXISTS idx_episodes_date ON episodes(date);
        CREATE INDEX IF NOT EXISTS idx_runs_started ON pipeline_runs(started_at);
        CREATE INDEX IF NOT EXISTS idx_findings_date ON findings(episode_date);
        CREATE INDEX IF NOT EXISTS idx_suggestions_date ON suggestions(episode_date);
        CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);
    """)
    # Migrate: add rss_summary column if missing (existing DBs)
    try:
        conn.execute("ALTER TABLE digests ADD COLUMN rss_summary TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migrate: add segment_counts column to digests if missing
    try:
        conn.execute("ALTER TABLE digests ADD COLUMN segment_counts TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migrate: add segment_sources column to digests if missing
    try:
        conn.execute("ALTER TABLE digests ADD COLUMN segment_sources TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migrate: add gcs_url column to episodes if missing
    try:
        conn.execute("ALTER TABLE episodes ADD COLUMN gcs_url TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migrate: add email_count column to digests if missing
    try:
        conn.execute("ALTER TABLE digests ADD COLUMN email_count INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # Migrate: add audio_segment_words column to episodes if missing
    try:
        conn.execute("ALTER TABLE episodes ADD COLUMN audio_segment_words TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
    # Migrate: add audio_analysis_status column to episodes if missing
    try:
        conn.execute("ALTER TABLE episodes ADD COLUMN audio_analysis_status TEXT NOT NULL DEFAULT 'none'")
    except sqlite3.OperationalError:
        pass
    # Migrate: add quality_report column to digests if missing
    try:
        conn.execute("ALTER TABLE digests ADD COLUMN quality_report TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
    # Migrate: add audio_analysis_full column to episodes if missing
    try:
        conn.execute("ALTER TABLE episodes ADD COLUMN audio_analysis_full TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
    conn.commit()


# --- Digest CRUD ---

def has_episode(date: str, db_path: Path | None = None) -> bool:
    """Check if an episode exists for the given date."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM episodes WHERE date = ?", (date,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def save_digest(date: str, markdown_text: str, article_count: int,
                total_words: int, topics_summary: str, rss_summary: str = "",
                email_count: int = 0,
                segment_counts: dict[str, int] | None = None,
                segment_sources: dict[str, list[str]] | None = None,
                force: bool = False,
                db_path: Path | None = None) -> None:
    """Save or update a daily digest.

    Refuses to overwrite if an episode already exists for this date (locked),
    unless force=True (used when user explicitly publishes a new episode).
    """
    if not force and has_episode(date, db_path=db_path):
        logger.warning("Digest for %s is locked (episode exists) — skipping overwrite.", date)
        return

    conn = _get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO digests (date, markdown_text, article_count, total_words,
               topics_summary, rss_summary, email_count,
               segment_counts, segment_sources, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
               markdown_text=excluded.markdown_text,
               article_count=excluded.article_count,
               total_words=excluded.total_words,
               topics_summary=excluded.topics_summary,
               rss_summary=excluded.rss_summary,
               email_count=excluded.email_count,
               segment_counts=excluded.segment_counts,
               segment_sources=excluded.segment_sources,
               created_at=excluded.created_at""",
            (date, markdown_text, article_count, total_words, topics_summary,
             rss_summary, email_count,
             json.dumps(segment_counts or {}),
             json.dumps(segment_sources or {}),
             datetime.now(UTC).isoformat()),
        )
        conn.commit()
        logger.info("Saved digest for %s to database", date)
    finally:
        conn.close()


def get_digest(date: str, db_path: Path | None = None) -> dict | None:
    """Get a single digest by date."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM digests WHERE date = ?", (date,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_digests(limit: int = 50, db_path: Path | None = None) -> list[dict]:
    """List recent digests (most recent first)."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id, date, article_count, total_words, email_count, topics_summary, created_at "
            "FROM digests ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_digests_with_char_count(limit: int = 100, db_path: Path | None = None) -> list[dict]:
    """List recent digests with markdown_text char count (avoids N+1 queries)."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id, date, article_count, total_words, email_count, topics_summary, "
            "LENGTH(markdown_text) as total_chars, created_at "
            "FROM digests ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_digest(date: str, db_path: Path | None = None) -> bool:
    """Delete a single digest by date.

    Args:
        date: Date string (YYYY-MM-DD).

    Returns:
        True if a row was deleted, False otherwise.
    """
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute("DELETE FROM digests WHERE date = ?", (date,))
        conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Deleted digest for %s", date)
        return deleted
    finally:
        conn.close()


def delete_digests_between(start: str, end: str, db_path: Path | None = None) -> int:
    """Delete digests in a date range (inclusive).

    Args:
        start: Start date (YYYY-MM-DD).
        end: End date (YYYY-MM-DD).

    Returns:
        Number of rows deleted.
    """
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM digests WHERE date BETWEEN ? AND ?", (start, end)
        )
        conn.commit()
        deleted = cursor.rowcount
        logger.info("Deleted %d digests between %s and %s", deleted, start, end)
        return deleted
    finally:
        conn.close()


def get_topic_coverage(limit: int = 30, published_only: bool = False,
                       db_path: Path | None = None) -> list[dict]:
    """Get segment_counts and segment_sources from recent digests for topic coverage analysis.

    Args:
        limit: Max number of digests to return.
        published_only: If True, only return digests that have a published episode.
    """
    conn = _get_connection(db_path)
    try:
        if published_only:
            rows = conn.execute(
                "SELECT d.date, d.segment_counts, d.segment_sources "
                "FROM digests d INNER JOIN episodes e ON d.date = e.date "
                "ORDER BY d.date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT date, segment_counts, segment_sources FROM digests ORDER BY date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["segment_counts"] = json.loads(d["segment_counts"])
            d["segment_sources"] = json.loads(d.get("segment_sources") or "{}")
            result.append(d)
        return result
    finally:
        conn.close()


def get_digest_coverage_detail(limit: int = 100, published_only: bool = False,
                                db_path: Path | None = None) -> list[dict]:
    """Get digest data including markdown_text for word-level coverage parsing.

    Returns results in ascending date order (oldest first).
    """
    conn = _get_connection(db_path)
    try:
        if published_only:
            rows = conn.execute(
                "SELECT d.date, d.markdown_text, d.total_words, d.segment_counts "
                "FROM digests d INNER JOIN episodes e ON d.date = e.date "
                "ORDER BY d.date ASC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT date, markdown_text, total_words, segment_counts "
                "FROM digests ORDER BY date ASC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["segment_counts"] = json.loads(d["segment_counts"])
            result.append(d)
        return result
    finally:
        conn.close()


# --- Episode Archive ---

def delete_episode(date: str, db_path: Path | None = None) -> bool:
    """Delete an episode record by date.

    Args:
        date: Date string (YYYY-MM-DD).

    Returns:
        True if a row was deleted, False otherwise.
    """
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute("DELETE FROM episodes WHERE date = ?", (date,))
        conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Deleted episode record for %s", date)
        return deleted
    finally:
        conn.close()


def save_episode(date: str, file_size_bytes: int, duration_seconds: int,
                 duration_formatted: str, topics_summary: str,
                 rss_summary: str = "", gcs_url: str = "",
                 db_path: Path | None = None) -> None:
    """Permanently archive an episode. This record is never deleted."""
    conn = _get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO episodes (date, file_size_bytes, duration_seconds,
               duration_formatted, topics_summary, rss_summary, gcs_url, published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
               file_size_bytes=excluded.file_size_bytes,
               duration_seconds=excluded.duration_seconds,
               duration_formatted=excluded.duration_formatted,
               topics_summary=excluded.topics_summary,
               rss_summary=excluded.rss_summary,
               gcs_url=excluded.gcs_url,
               published_at=excluded.published_at""",
            (date, file_size_bytes, duration_seconds, duration_formatted,
             topics_summary, rss_summary, gcs_url, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        logger.info("Archived episode for %s", date)
    finally:
        conn.close()


def list_episodes(limit: int = 0, db_path: Path | None = None) -> list[dict]:
    """List all archived episodes (most recent first). No limit by default."""
    conn = _get_connection(db_path)
    try:
        if limit > 0:
            rows = conn.execute(
                "SELECT * FROM episodes ORDER BY date DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM episodes ORDER BY date DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Audio Analysis ---

def update_audio_analysis(date: str, audio_segment_words: dict[str, int],
                          status: str = "complete",
                          audio_analysis_full: dict | None = None,
                          db_path: Path | None = None) -> None:
    """Update the audio transcription results for an episode."""
    conn = _get_connection(db_path)
    try:
        if audio_analysis_full is not None:
            conn.execute(
                "UPDATE episodes SET audio_segment_words = ?, audio_analysis_status = ?, "
                "audio_analysis_full = ? WHERE date = ?",
                (json.dumps(audio_segment_words), status,
                 json.dumps(audio_analysis_full), date),
            )
        else:
            conn.execute(
                "UPDATE episodes SET audio_segment_words = ?, audio_analysis_status = ? "
                "WHERE date = ?",
                (json.dumps(audio_segment_words), status, date),
            )
        conn.commit()
        logger.info("Updated audio analysis for %s (status=%s)", date, status)
    finally:
        conn.close()


def set_audio_analysis_status(date: str, status: str,
                              db_path: Path | None = None) -> None:
    """Update only the audio analysis status for an episode."""
    conn = _get_connection(db_path)
    try:
        conn.execute(
            "UPDATE episodes SET audio_analysis_status = ? WHERE date = ?",
            (status, date),
        )
        conn.commit()
    finally:
        conn.close()


def get_episodes_with_audio(limit: int = 100,
                            db_path: Path | None = None) -> list[dict]:
    """Get episodes with audio analysis data (oldest first)."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT date, audio_segment_words, audio_analysis_status "
            "FROM episodes ORDER BY date ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Pipeline Run Logging ---

def start_run(run_id: str, db_path: Path | None = None) -> None:
    """Record the start of a pipeline run."""
    conn = _get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO pipeline_runs (run_id, started_at, status, steps_log) "
            "VALUES (?, ?, 'running', '[]')",
            (run_id, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def log_step(run_id: str, step: str, status: str, message: str = "",
             db_path: Path | None = None) -> None:
    """Log a pipeline step to the current run."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT steps_log FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            return

        steps = json.loads(row["steps_log"])
        steps.append({
            "step": step,
            "status": status,
            "message": message,
            "timestamp": datetime.now(UTC).isoformat(),
        })

        conn.execute(
            "UPDATE pipeline_runs SET steps_log = ?, current_step = ? WHERE run_id = ?",
            (json.dumps(steps), step, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def finish_run(run_id: str, status: str, error_message: str = "",
               db_path: Path | None = None) -> None:
    """Mark a pipeline run as finished."""
    conn = _get_connection(db_path)
    try:
        conn.execute(
            "UPDATE pipeline_runs SET status = ?, finished_at = ?, error_message = ? "
            "WHERE run_id = ?",
            (status, datetime.now(UTC).isoformat(), error_message, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_runs(limit: int = 20, db_path: Path | None = None) -> list[dict]:
    """List recent pipeline runs (most recent first)."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["steps_log"] = json.loads(d["steps_log"])
            result.append(d)
        return result
    finally:
        conn.close()


def get_run(run_id: str, db_path: Path | None = None) -> dict | None:
    """Get a single pipeline run by run_id."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["steps_log"] = json.loads(d["steps_log"])
        return d
    finally:
        conn.close()


# --- Learning System ---

def save_quality_report(date: str, quality_report: dict,
                        db_path: Path | None = None) -> None:
    """Save quality report JSON to the digests table."""
    conn = _get_connection(db_path)
    try:
        conn.execute(
            "UPDATE digests SET quality_report = ? WHERE date = ?",
            (json.dumps(quality_report), date),
        )
        conn.commit()
        logger.info("Saved quality report for %s", date)
    finally:
        conn.close()


def get_quality_report(date: str, db_path: Path | None = None) -> dict:
    """Get quality report for a digest date."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT quality_report FROM digests WHERE date = ?", (date,)
        ).fetchone()
        if row and row["quality_report"]:
            return json.loads(row["quality_report"])
        return {}
    finally:
        conn.close()


def get_audio_analysis_full(date: str, db_path: Path | None = None) -> dict:
    """Get full audio analysis for an episode date."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT audio_analysis_full FROM episodes WHERE date = ?", (date,)
        ).fetchone()
        if row and row["audio_analysis_full"]:
            return json.loads(row["audio_analysis_full"])
        return {}
    finally:
        conn.close()


def save_findings(episode_date: str, findings: list[dict],
                  db_path: Path | None = None) -> list[int]:
    """Save findings to DB, return list of inserted IDs."""
    now = datetime.now(UTC).isoformat()
    ids = []
    conn = _get_connection(db_path)
    try:
        for f in findings:
            cursor = conn.execute(
                """INSERT INTO findings (episode_date, job, severity, topic, finding, data, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (episode_date, f["job"], f["severity"], f.get("topic"),
                 f["finding"], json.dumps(f.get("data", {})), now),
            )
            ids.append(cursor.lastrowid)
        conn.commit()
        logger.info("Saved %d findings for %s", len(ids), episode_date)
        return ids
    finally:
        conn.close()


def save_suggestions(episode_date: str, suggestions: list[dict],
                     db_path: Path | None = None) -> None:
    """Save suggestions to DB."""
    now = datetime.now(UTC).isoformat()
    conn = _get_connection(db_path)
    try:
        for s in suggestions:
            conn.execute(
                """INSERT INTO suggestions
                   (episode_date, type, status, title, detail, current_value,
                    suggested_value, finding_ids, created_at)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
                (episode_date, s["type"], s["title"], s["detail"],
                 s.get("current_value"), s.get("suggested_value"),
                 json.dumps(s.get("finding_ids", [])), now),
            )
        conn.commit()
        logger.info("Saved %d suggestions for %s", len(suggestions), episode_date)
    finally:
        conn.close()


def get_findings(episode_date: str, db_path: Path | None = None) -> list[dict]:
    """Get all findings for an episode date."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM findings WHERE episode_date = ? ORDER BY id",
            (episode_date,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"])
            result.append(d)
        return result
    finally:
        conn.close()


def get_suggestions(episode_date: str | None = None, status: str | None = None,
                    db_path: Path | None = None) -> list[dict]:
    """Get suggestions, optionally filtered by episode date and/or status."""
    conn = _get_connection(db_path)
    try:
        conditions = []
        params = []
        if episode_date:
            conditions.append("episode_date = ?")
            params.append(episode_date)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM suggestions {where} ORDER BY id DESC",
            params,
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["finding_ids"] = json.loads(d["finding_ids"])
            result.append(d)
        return result
    finally:
        conn.close()


def update_suggestion_status(suggestion_id: int, status: str,
                             db_path: Path | None = None) -> bool:
    """Update a suggestion's status. Returns True if updated."""
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute(
            "UPDATE suggestions SET status = ?, reviewed_at = ? WHERE id = ?",
            (status, datetime.now(UTC).isoformat(), suggestion_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def save_prompt_override(prompt_key: str, original_value: str, override_value: str,
                         suggestion_id: int | None = None,
                         db_path: Path | None = None) -> None:
    """Save or update a prompt override."""
    conn = _get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO prompt_overrides
               (prompt_key, original_value, override_value, approved_from_suggestion_id, applied_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(prompt_key) DO UPDATE SET
               override_value=excluded.override_value,
               approved_from_suggestion_id=excluded.approved_from_suggestion_id,
               applied_at=excluded.applied_at""",
            (prompt_key, original_value, override_value, suggestion_id,
             datetime.now(UTC).isoformat()),
        )
        conn.commit()
        logger.info("Saved prompt override for key '%s'", prompt_key)
    finally:
        conn.close()


def get_prompt_overrides(db_path: Path | None = None) -> dict[str, str]:
    """Load active prompt overrides from DB. Returns {prompt_key: override_value}."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT prompt_key, override_value FROM prompt_overrides"
        ).fetchall()
        return {r["prompt_key"]: r["override_value"] for r in rows}
    finally:
        conn.close()


def get_prompt_overrides_full(db_path: Path | None = None) -> list[dict]:
    """Get full prompt override records."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM prompt_overrides ORDER BY applied_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_episode_dates_with_findings(db_path: Path | None = None) -> list[str]:
    """Get distinct episode dates that have findings, most recent first."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT episode_date FROM findings ORDER BY episode_date DESC"
        ).fetchall()
        return [r["episode_date"] for r in rows]
    finally:
        conn.close()


def get_recent_coverage_gap_trends(days: int = 7,
                                    db_path: Path | None = None) -> list[dict]:
    """Get coverage gap trends over recent days for weekly analysis."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT topic, COUNT(*) as count
               FROM findings
               WHERE job = 'coverage_gap'
               AND created_at >= datetime('now', ?)
               AND topic IS NOT NULL
               GROUP BY topic
               ORDER BY count DESC""",
            (f"-{days} days",),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
