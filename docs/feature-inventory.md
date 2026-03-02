# Feature Inventory

Every user-facing and operator-facing feature, with key files, status, and the 5 most critical features marked with a star.

## Features

### 1. Digest Preparation Workflow ⭐

**Description**: The core daily workflow. Fetches newsletter emails from Gmail, parses and classifies articles by topic, and compiles them into a structured markdown digest using Gemini AI. The digest is held in-memory until the operator reviews and publishes it.

**Key files**:
- `generate.py` — Pipeline orchestrator (fetch → parse → compile)
- `src/email_fetcher.py` — Gmail API integration
- `src/content_parser.py` — HTML parsing, deduplication, Google Alerts splitting
- `src/topic_classifier.py` — Gemini batch classification with keyword fallback
- `src/digest_compiler.py` — Single Gemini call to produce structured digest
- `src/llm_client.py` — Gemini 2.5 Flash wrapper
- `routers/pipeline.py` — `/api/start-preparation`, `/api/cancel-preparation`, `/api/preparation-digest`
- `main.py` — `_run_generation()`, `ShowState` (holds in-memory digest)

**Status**: Production. Runs daily via scheduler and external cron trigger.

---

### 2. Episode Upload & Publish ⭐

**Description**: After the operator records audio externally (via NotebookLM), they upload it through the dashboard. The system validates the audio, converts non-MP3 formats via ffmpeg, and holds it as a `.prep.mp3` preview. Publishing renames the file, saves the digest to DB, uploads the MP3 to GCS, updates the RSS feed, and triggers background transcription.

**Key files**:
- `routers/episodes.py` — `/api/upload-episode`, `/api/publish-episode`
- `src/episode_manager.py` — MP3 validation, ffmpeg conversion, GCS upload, metadata extraction
- `src/feed_builder.py` — RSS feed generation, episode catalog management
- `src/gcs_storage.py` — GCS upload for episodes and DB
- `main.py` — `_transcribe_episode_background()`

**Status**: Production. Supports MP3, M4A, WAV, OGG, WebM formats.

---

### 3. RSS Feed ⭐

**Description**: Generates and serves a standard RSS podcast feed. Each episode includes title, date, duration, file size, summary, and a link to the MP3 (either GCS URL or local). Feed is rebuilt from DB on startup via `sync_catalog_from_db()`. Supports per-show feeds at `/{show_id}/feed.xml` and a legacy route at `/feed.xml`.

**Key files**:
- `src/feed_builder.py` — `feedgen`-based RSS generation, `episodes.json` catalog
- `routers/episodes.py` — `/feed.xml`, `/{show_id}/feed.xml`
- `config.py` — `ShowConfig.feed_path`, `settings.base_url`

**Status**: Production. Compatible with Apple Podcasts, Overcast, and other podcast apps.

---

### 4. Dashboard SPA ⭐

**Description**: Single-page web application for the operator. Tabs: Latest (current episode + preparation controls), History (digest/episode archive), Coverage (topic analytics with radar chart and 3D visualization), Learning (findings and suggestions), Prompts (system prompt editor). All interaction via fetch API calls to the backend.

**Key files**:
- `templates/dashboard.html` — ~3800 lines of vanilla JS + inline CSS
- `routers/dashboard.py` — `/`, `/{show_id}/`, `/api/shows`, `/api/show-format`
- `routers/episodes.py` — `/api/latest-episode` (primary dashboard data source)

**Status**: Production. Single HTML file, no build step.

---

### 5. Episode Export (ZIP Download) ⭐

**Description**: Bundles all published episode MP3s and digest markdown files into a downloadable ZIP. Downloads missing MP3s from GCS before zipping. Caches the ZIP for 1 hour. Also supports weekly export ZIPs with auto-delete after download.

**Key files**:
- `routers/digests.py` — `/api/export-episodes`, `/api/export-weeks`, `/api/download-export/{filename}`
- `src/database.py` — `list_episodes()`, `list_digests()`, `get_digest()`
- `config.py` — `ShowConfig.exports_dir`

**Status**: Production. Large exports may be slow due to GCS downloads.

---

### 6. Topic Coverage Analytics

**Description**: Radar chart and 3D visualization showing how well each topic segment is covered across episodes. Compares actual article counts against target capacity (derived from segment duration). Generates subscribe/unsubscribe suggestions when topics are under/over-covered.

**Key files**:
- `routers/digests.py` — `/api/topic-coverage`, `/api/topic-coverage-3d`, `/api/coverage-dashboard`
- `src/database.py` — `get_topic_coverage()`, `get_digest_coverage_detail()`
- `templates/dashboard.html` — Coverage tab with radar chart and 3D canvas

**Status**: Production.

---

### 7. Audio Transcription & Analysis

**Description**: After an episode is published, the MP3 is uploaded to Gemini Files API for transcription analysis. Produces per-segment word counts, coverage gaps, and tone findings. Results stored in the episodes table.

**Key files**:
- `src/audio_transcriber.py` — Gemini Files API upload, audio analysis
- `main.py` — `_transcribe_episode_background()`
- `routers/episodes.py` — `/api/transcribe-episode`, `/api/transcription-status`
- `src/database.py` — `update_audio_analysis()`, `get_audio_analysis_full()`

**Status**: Production. Can be triggered manually or automatically after publish.

---

### 8. Learning System (Findings & Suggestions)

**Description**: Post-transcription analysis that compares digest intent vs audio output. Generates findings (factual observations about coverage, tone, word budget) and suggestions (actionable improvements like prompt edits or source changes). Suggestions can be approved, dismissed, or snoozed. Approved prompt_edit suggestions become active prompt overrides.

**Key files**:
- `src/episode_analyzer.py` — `analyze_episode()`, `run_weekly_trend_analysis()`
- `routers/learning.py` — All `/api/learning/*` endpoints
- `src/database.py` — findings, suggestions, prompt_overrides CRUD

**Status**: Production.

---

### 9. Prompt Configuration & Overrides

**Description**: The operator can view and edit the system prompt and podcast preamble used by the digest compiler. The learning system can also suggest prompt changes, which become active overrides when approved. Overrides are stored in DB and loaded at compile time.

**Key files**:
- `routers/digests.py` — `/api/prompt-config` (GET/POST)
- `routers/learning.py` — `/api/learning/prompt-overrides`, `/api/learning/approve-suggestion`
- `src/digest_compiler.py` — `get_prompt_config()`, `save_prompt_config()`, override loading
- `src/database.py` — `get_prompt_overrides()`, `save_prompt_override()`

**Status**: Production.

---

### 10. Multi-Show Support

**Description**: Multiple independent shows can run from one deployment. Each show has its own Gmail credentials, NotebookLM notebook, segment format, SQLite DB, episodes directory, and RSS feed. Configured via `SHOW_IDS` env var and per-show `SHOW_{ID}_*` env vars.

**Key files**:
- `config.py` — `load_shows()`, `ShowConfig`, `SHOW_FORMATS`
- `main.py` — `_show_states` registry, `_resolve_show()`
- All routers — `?show_id=` query parameter on every endpoint

**Status**: Production. Currently configured with "hootline" (14 segments, 34 min) and "sparrow" (2 segments, 5 min).

---

### 11. Health Checks

**Description**: Two health endpoints for monitoring. Basic health returns status, generation state, and schedule. Detailed health adds file system stats, DB counts, and ffmpeg availability.

**Key files**:
- `routers/pipeline.py` — `/health`, `/health/detail`

**Status**: Production.

---

### 12. Cron Trigger

**Description**: External cron service (e.g., cron-job.org) can trigger digest generation via authenticated HTTP request. Accepts secret via query parameter or Authorization Bearer header. Can trigger a single show or all shows.

**Key files**:
- `routers/pipeline.py` — `/api/cron/generate`

**Status**: Production. Protected by `CRON_SECRET`.

---

### 13. Background Scheduler

**Description**: Fallback scheduler that triggers daily generation if the external cron misses. Calculates next run time from `generation_hour`/`generation_minute` settings. Also runs weekly trend analysis on Sundays.

**Key files**:
- `main.py` — `_scheduler()`, `_calc_next_run()`

**Status**: Production.

---

### 14. Digest History & Views

**Description**: Full archive of all digests and episodes. Digests can be viewed as styled HTML pages or downloaded as markdown files. History API combines digest and episode data, including orphaned episodes (digest lost during redeploy).

**Key files**:
- `routers/digests.py` — `/api/history`, `/digests/{date}.html`, `/digests/{date}.md`, `/{show_id}/digests/*`
- `src/database.py` — `list_digests_with_char_count()`, `list_episodes()`

**Status**: Production.

---

### 15. Episode Revision Bumping

**Description**: Allows re-publishing an episode with updated metadata without changing the audio file. Increments a revision counter in the episodes.json catalog.

**Key files**:
- `routers/episodes.py` — `/api/bump-revision`
- `src/feed_builder.py` — `bump_revision()`

**Status**: Production.

---

### 16. GCS Database Sync

**Description**: SQLite database is synchronized with GCS. Downloaded on startup, uploaded after every write operation (publish, transcribe, learning actions). WAL is checkpointed before upload. Dev mode skips uploads.

**Key files**:
- `src/gcs_storage.py` — `upload_db()`, `download_db()`, `_checkpoint_wal()`
- `main.py` — `_deferred_startup()`

**Status**: Production.

---

### 17. Audio File Serving with Range Requests

**Description**: Serves episode MP3 files with HTTP range request support for streaming. Podcast apps use range requests for seeking and progressive download.

**Key files**:
- `routers/episodes.py` — `/episodes/{filename}`, `/{show_id}/episodes/{filename}`, `_serve_episode()`

**Status**: Production.

---

### 18. Pipeline Run Logging

**Description**: Every generation run is logged to the `pipeline_runs` table with a unique run_id, step-by-step progress, and final status. Viewable via API.

**Key files**:
- `routers/pipeline.py` — `/api/runs`, `/api/runs/{run_id}`
- `src/database.py` — `start_run()`, `log_step()`, `finish_run()`
- `generate.py` — logs steps during execution

**Status**: Production.

## Critical Features Summary

| # | Feature | Why Critical |
|---|---------|-------------|
| 1 | Digest Preparation Workflow ⭐ | Core value proposition — if this breaks, no episodes get produced |
| 2 | Episode Upload & Publish ⭐ | The publish path — if this breaks, episodes can't reach listeners |
| 3 | RSS Feed ⭐ | The distribution channel — if this breaks, podcast apps can't fetch episodes |
| 4 | Dashboard SPA ⭐ | The only operator interface — if this breaks, the operator is locked out |
| 5 | Episode Export (ZIP) ⭐ | Data portability — the only way to get a complete backup of all episodes |
