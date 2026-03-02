# Contributing

## Acceptance Criteria

Every change must satisfy **all** of these before merging:

1. **All 25 eval tasks pass** — run `python evals/run_evals.py --base-url http://localhost:8000` against a live app instance. Zero failures required.
2. **Preparation workflow tests pass** — run `pytest tests/test_preparation_workflow.py -v`. All 20 tests must pass.
3. **No new regressions** — if you add a feature, add eval tasks to `evals/tasks/core.yaml` covering it.
4. **GCS behavior unchanged** — dev mode must never write to GCS. Verify any code that calls `gcs_storage.upload_db()` or `gcs_storage.upload_episode()` is gated correctly.

## Environment Model

Understanding the dev/prod split is essential:

| | Dev (default) | Prod (`NOCTUA_ENV=prod`) |
|---|---|---|
| GCS DB upload | **No-op** (logged warning) | Active |
| GCS DB download | Active (reads from prod) | Active |
| GCS episode upload | Active | Active |
| Local disk | Ephemeral — lost on restart | Ephemeral — lost on restart |
| Source of truth | GCS (for DB), local (for in-progress work) | GCS |

**Key rule**: Never assume local files persist between restarts. Always treat GCS as the canonical store.

## Project Structure

```
main.py                  # App factory, state, scheduler
generate.py              # Pipeline orchestrator (fetch → parse → compile)
config.py                # Settings, ShowConfig, ShowFormat

routers/
  pipeline.py            # Health, cron, preparation workflow
  episodes.py            # Upload, publish, transcribe, feed, audio serving
  digests.py             # Digest CRUD, coverage, history, export, views
  dashboard.py           # SPA dashboard, show discovery
  learning.py            # Learning system (findings, suggestions, overrides)

src/
  models.py              # Data models (EmailMessage, Article, etc.)
  database.py            # SQLite interface (all tables)
  email_fetcher.py       # Gmail API
  content_parser.py      # HTML parsing, dedup, classification
  topic_classifier.py    # Gemini topic classification
  digest_compiler.py     # Gemini digest compilation
  llm_client.py          # Gemini API wrapper
  feed_builder.py        # RSS feed generation
  episode_manager.py     # MP3 validation, ffmpeg, GCS upload
  gcs_storage.py         # GCS client (episodes + DB sync)
  audio_transcriber.py   # Gemini audio analysis
  episode_analyzer.py    # Post-episode learning analysis
  exceptions.py          # Custom exception hierarchy
  show_bible_context.py  # Show bible rules for tone analysis

templates/
  dashboard.html         # Single-page dashboard (~3800 lines)

evals/
  tasks/core.yaml        # 25 eval task definitions
  run_evals.py           # Eval runner (exercises live endpoints)
  report.json            # Latest eval results

docs/
  system-design.md       # Architecture and component overview
  data-architecture.md   # Models, schema, data flow
  feature-inventory.md   # All features with status and key files
  known-issues.md        # Known issues and resolved issues
```

## Adding a New Router

1. Create `routers/your_router.py` with `router = APIRouter()`
2. Use late imports for anything from `main.py`:
   ```python
   def _get_resolve_show():
       from main import _resolve_show
       return _resolve_show
   ```
3. All endpoints must accept `show_id` parameter (defaults to first show)
4. Register in `main.py`: `from routers.your_router import router as your_router; app.include_router(your_router)`

## Adding a New Database Column

1. Add the column to the `CREATE TABLE` statement in `_create_tables()` (for new installs)
2. Add a migration block (for existing DBs):
   ```python
   try:
       conn.execute("ALTER TABLE table_name ADD COLUMN col_name TYPE NOT NULL DEFAULT 'value'")
   except sqlite3.OperationalError:
       pass
   ```
3. Migrations run on every connection open — no separate migration tool needed

## Running Tests

```bash
# Preparation workflow tests (most comprehensive)
pytest tests/test_preparation_workflow.py -v

# Episode manager tests
pytest tests/test_episode_manager.py -v

# Full eval suite (requires running app)
python -m uvicorn main:app --port 8000 &
python evals/run_evals.py --base-url http://localhost:8000

# With Claude-powered failure analysis
python evals/run_evals.py --base-url http://localhost:8000 --grade
```

## Common Patterns

### Per-show state access in routers
```python
state = _get_resolve_show()(show_id)  # Returns ShowState
show = state.show                      # ShowConfig
db_path = show.db_path                 # Path to show's SQLite DB
```

### Database calls always pass db_path
```python
database.get_digest(date, db_path=state.show.db_path)
```

### GCS uploads are dev-safe
```python
# This is a no-op in dev mode — safe to call unconditionally
gcs_storage.upload_db(show.db_path, show.show_id)
```
