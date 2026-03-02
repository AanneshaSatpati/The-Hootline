# System Design

## What This App Does

The Hootline is a daily podcast generator that automatically fetches newsletter emails from Gmail, parses and classifies articles by topic using AI, compiles them into a structured digest document, and publishes the result as a podcast episode via RSS. A web dashboard lets the operator prepare digests, upload audio (recorded externally via NotebookLM), publish episodes, review topic coverage analytics, and manage AI-generated improvement suggestions through a learning system.

## Architecture Overview

```
Gmail ──► email_fetcher ──► content_parser ──► digest_compiler ──► [Manual audio via NotebookLM]
                                  │                   │                        │
                            topic_classifier      llm_client              episode_manager
                            (Gemini 2.5 Flash)   (Gemini 2.5 Flash)      (ffmpeg + mutagen)
                                                                               │
                                                                          feed_builder ──► RSS XML
                                                                               │
                                                                          gcs_storage ──► GCS bucket
                                                                               │
                                                                      audio_transcriber ──► episode_analyzer
                                                                      (Gemini Files API)    (learning system)
```

## Components and Modules

### Entry Points

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app factory, lifespan management, background scheduler, per-show state registry (`ShowState`, `_show_states`), `_run_generation()` task runner, `_transcribe_episode_background()` helper. Wires all routers and mounts static files. ~297 lines. |
| `generate.py` | Pipeline orchestrator for steps 1-3 (fetch → parse → compile). `generate_digest_only()` is the core function, called by `_run_generation()` in main.py and directly via CLI. Logs each step to `pipeline_runs` table. |
| `config.py` | Centralized configuration. `Settings` (pydantic-settings from `.env`), `ShowConfig` (frozen dataclass per show), `ShowFormat` (segment structure), `is_prod()`/`is_dev()` environment helpers, `load_shows()` multi-show discovery. |

### Routers

| File | Prefix | Owns |
|------|--------|------|
| `routers/pipeline.py` | — | `/health`, `/health/detail`, `/api/runs`, `/api/cron/generate`, `/api/start-preparation`, `/api/cancel-preparation`, `/api/preparation-digest` |
| `routers/episodes.py` | — | `/api/latest-episode`, `/api/episodes`, `/api/publish-episode`, `/api/upload-episode`, `/api/transcribe-episode`, `/api/transcription-status`, `/api/bump-revision`, `/feed.xml`, `/{show_id}/feed.xml`, `/episodes/{filename}`, `/{show_id}/episodes/{filename}` |
| `routers/digests.py` | — | `/api/digests`, `/api/digests/{date}`, `/api/topic-coverage`, `/api/topic-coverage-3d`, `/api/coverage-dashboard`, `/api/history`, `/api/export-episodes`, `/api/export-weeks`, `/api/download-export/{filename}`, `/api/prompt-config`, `/digests/{date}.html`, `/digests/{date}.md`, `/{show_id}/digests/*` |
| `routers/dashboard.py` | — | `/`, `/{show_id}/`, `/api/shows`, `/api/show-format` |
| `routers/learning.py` | — | `/api/learning/episodes`, `/api/learning/episode/{date}`, `/api/learning/approve-suggestion`, `/api/learning/dismiss-suggestion`, `/api/learning/snooze-suggestion`, `/api/learning/prompt-overrides` |

All routers use **late imports** (`from main import _resolve_show`) to avoid circular dependencies with `main.py`.

### Source Modules (`src/`)

| File | Purpose |
|------|---------|
| `src/models.py` | 5 dataclasses: `EmailMessage`, `Article`, `DailyDigest`, `CompiledDigest`, `EpisodeMetadata`. Pure data containers with no behavior. |
| `src/database.py` | SQLite interface with WAL mode. 7 tables (see data-architecture.md). All functions accept an optional `db_path` parameter for multi-show isolation. Auto-migration via `ALTER TABLE` in `_create_tables()`. |
| `src/email_fetcher.py` | Gmail API integration. Fetches emails from a labeled folder within a 24-hour rolling window. Returns `list[EmailMessage]`. |
| `src/content_parser.py` | HTML cleaning with BeautifulSoup, deduplication (Jaccard similarity on trigrams), Google Alerts email splitting, batch AI classification via `topic_classifier`. Returns `DailyDigest`. |
| `src/topic_classifier.py` | 14-topic `Topic` enum. Gemini batch classification with JSON output parsing and keyword-based fallback when AI fails. |
| `src/digest_compiler.py` | Single Gemini API call that takes classified articles and produces a structured digest document (markdown), RSS summary, and quality report. Respects per-show `ShowFormat` segment structure. Loads prompt overrides from DB. |
| `src/llm_client.py` | Thin wrapper around `google.generativeai` (Gemini 2.5 Flash). Handles model configuration, retry logic, and response extraction. |
| `src/feed_builder.py` | RSS feed generation using `feedgen`. Manages `episodes.json` catalog, adds/removes episodes, supports revision bumping, and syncs catalog from DB on startup. |
| `src/episode_manager.py` | MP3 validation with `mutagen`, ffmpeg conversion for non-MP3 uploads, metadata extraction (duration, file size), GCS upload for episode files. |
| `src/gcs_storage.py` | Google Cloud Storage client. `upload_episode()` for MP3s, `upload_db()`/`download_db()` for SQLite sync. **Dev mode is a no-op for uploads.** |
| `src/audio_transcriber.py` | Uploads MP3 to Gemini Files API, analyzes audio for per-segment word counts, coverage gaps, and tone findings. |
| `src/episode_analyzer.py` | Post-transcription learning analysis. Compares digest intent vs audio output, generates findings and suggestions. Runs weekly trend analysis on Sundays. |
| `src/show_bible_context.py` | Returns show bible rules (tone, style guidelines) for Gemini tone analysis prompts. |
| `src/exceptions.py` | Custom exception hierarchy. `NoctuaError` base, with `EmailFetchError`, `ContentParseError`, `DigestCompileError` subclasses. |

### Frontend

| File | Purpose |
|------|---------|
| `templates/dashboard.html` | Single-page application (~3800 lines). Vanilla JS with inline CSS. Tabs: Latest, History, Coverage, Learning, Prompts. Communicates with backend via fetch API. Template variables (`__SHOW_ID__`, `__SHOW_TITLE__`, `__SHOW_TAGLINE__`, `__BUILD_VERSION__`) are replaced server-side. |
| `static/` | Static assets (cover image). Mounted at `/static`. |

## The Full Pipeline

### Stage 1: Fetch (email_fetcher.py)
- Authenticates with Gmail API using OAuth2 credentials from `ShowConfig`
- Queries for emails in the configured label within a 24-hour window
- Returns raw `EmailMessage` objects (HTML + plain text)

### Stage 2: Parse & Classify (content_parser.py → topic_classifier.py)
- Cleans HTML with BeautifulSoup, extracts text content
- Splits Google Alerts emails into individual articles
- Deduplicates articles using trigram Jaccard similarity
- Batch-classifies all articles into 14 topics using Gemini 2.5 Flash
- Falls back to keyword matching if AI classification fails
- Returns `DailyDigest` with classified `Article` objects

### Stage 3: Compile (digest_compiler.py)
- Takes the `DailyDigest` and `ShowConfig`
- Loads prompt overrides from DB (if any approved suggestions exist)
- Makes a single Gemini API call with system prompt, preamble, and all articles
- Produces structured markdown with segments matching `ShowFormat`
- Generates RSS summary and quality report
- Returns `CompiledDigest`

### Stage 4: Audio Upload (manual, via dashboard)
- Operator records audio externally using NotebookLM
- Uploads audio file via `/api/upload-episode` (accepts MP3, M4A, WAV, OGG, WebM)
- Non-MP3 files are converted via ffmpeg
- Saved as `.prep.mp3` (preview, not yet published)

### Stage 5: Publish (routers/episodes.py)
- Operator clicks "Publish" in dashboard
- `.prep.mp3` renamed to canonical `noctua-{date}.mp3`
- Digest saved to DB (with `force=True` to allow overwrite)
- `episode_manager.process()` validates MP3, uploads to GCS, extracts metadata
- `feed_builder.add_episode()` updates RSS feed and `episodes.json`
- DB synced to GCS (in prod)

### Stage 6: Transcribe & Analyze (background)
- Automatically triggered after publish
- `audio_transcriber` uploads MP3 to Gemini Files API
- Analyzes for per-segment word counts, coverage gaps, tone findings
- `episode_analyzer` compares digest intent vs audio output
- Generates findings (factual observations) and suggestions (actionable improvements)
- Weekly trend analysis runs on Sundays

## External Services

| Service | Used By | Purpose |
|---------|---------|---------|
| **Gemini 2.5 Flash** (`google.generativeai`) | `llm_client.py`, `topic_classifier.py`, `digest_compiler.py`, `audio_transcriber.py` | AI classification, digest compilation, audio analysis |
| **Gemini Files API** | `audio_transcriber.py` | Upload MP3 for audio analysis (Gemini requires file upload for large audio) |
| **Google Cloud Storage** | `gcs_storage.py`, `episode_manager.py` | Permanent storage for episode MP3s and SQLite DB backup |
| **Gmail API** | `email_fetcher.py` | Fetch newsletter emails via OAuth2 |
| **SQLite** (local) | `database.py` | Primary data store for digests, episodes, pipeline runs, learning data |

## Dev/Prod Environment Model

The `NOCTUA_ENV` environment variable controls behavior:

### Dev Mode (default, `NOCTUA_ENV` unset or not "prod")
- `is_dev()` returns `True`, `is_prod()` returns `False`
- `gcs_storage.upload_db()` **is a no-op** — logs a warning and returns `False`
- `gcs_storage.upload_episode()` still works (episode uploads are always real)
- `gcs_storage.download_db()` still works (reads from prod GCS on startup)
- All API endpoints function normally
- Data changes are local-only and lost on redeploy

### Prod Mode (`NOCTUA_ENV=prod`)
- `is_prod()` returns `True`
- `gcs_storage.upload_db()` flushes WAL, uploads SQLite to GCS
- All writes are persisted to GCS

### Why GCS Is the Source of Truth
- The app runs on Replit where local disk is ephemeral
- On startup, `_deferred_startup()` downloads the DB from GCS for each show
- After every write operation (publish, transcribe, learning actions), DB is uploaded back to GCS
- This means the GCS copy is the canonical state; local disk is a working cache
- Episode MP3s are also stored in GCS and served via public URLs in RSS feeds

### Why Local Disk Is Ephemeral
- Replit containers can restart at any time
- The `output/` directory (episodes, DB, feed) is recreated from GCS on each startup
- `feed_builder.sync_catalog_from_db()` rebuilds `episodes.json` and `feed.xml` from DB
- `.prep.mp3` files (unpublished audio) are lost on redeploy — this is by design

## Where State Lives

| State | Location | Persistence |
|-------|----------|-------------|
| Digests, episodes, findings, suggestions | SQLite DB (`output/{show_id}/noctua.db`) | GCS-backed (uploaded after writes in prod) |
| Episode MP3 files | Local disk + GCS (`episodes/{show_id}/noctua-{date}.mp3`) | GCS is permanent; local is cache |
| RSS feed (`feed.xml`) | Local disk, rebuilt from DB on startup | Ephemeral (regenerated) |
| Episode catalog (`episodes.json`) | Local disk, rebuilt from DB on startup | Ephemeral (regenerated) |
| Preparation state (in-progress digest, generation status) | In-memory (`ShowState` dataclass in `_show_states`) | Lost on restart |
| Scheduler state (next run time) | In-memory (`_next_scheduled_run` global) | Lost on restart |
| Prompt overrides | SQLite DB (`prompt_overrides` table) | GCS-backed |
| Show configuration | Environment variables / `.env` file | Replit secrets |

## Multi-Show Architecture

- `config.load_shows()` discovers shows from `SHOW_IDS` env var
- Each show gets its own `ShowConfig` with isolated paths: `output/{show_id}/`
- Legacy single-show mode (no `SHOW_IDS`): uses `output/` directly (backward compatible)
- Per-show `ShowState` in `_show_states` dict, keyed by `show_id`
- Each show has its own DB, episodes dir, feed, and format definition
- All API endpoints accept `?show_id=` parameter; defaults to first configured show
- URL paths: legacy uses `/episodes/...`, multi-show uses `/{show_id}/episodes/...`

## Gaps and Risks

1. **No authentication on dashboard or API** — anyone with the URL can view digests, upload audio, and publish episodes. Only `/api/cron/generate` is protected by `CRON_SECRET`.

2. **Single-threaded SQLite** — all DB functions open/close connections per call. No connection pooling. WAL mode helps with concurrent reads, but heavy write loads could cause lock contention.

3. **No graceful handling of large audio files** — the upload endpoint reads the entire file into memory via chunks, but ffmpeg conversion has a 300-second timeout with no progress feedback.

4. **Preparation state is in-memory only** — if the server restarts between "prepare" and "publish", the digest and uploaded `.prep.mp3` are both lost. The operator must re-prepare.

5. **GCS upload failures are silent** — `upload_db()` catches all exceptions, logs an error, and returns `False`. If GCS upload fails after a publish, the episode exists locally but the DB state may not be backed up.

6. **No rate limiting** — AI calls (Gemini) have no rate limiting or cost controls. A burst of cron triggers could generate expensive API calls.

7. **Late imports for circular dependency avoidance** — every router uses `from main import ...` inside functions. This works but is fragile; adding new shared state requires updating multiple files.

8. **No input validation on digest markdown** — the compiled digest markdown is stored and rendered as-is. While the content comes from the AI (not user input), a prompt injection via newsletter content could affect output.
