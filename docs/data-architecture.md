# Data Architecture

## Data Models (`src/models.py`)

Five pure dataclasses define the pipeline's data flow:

### EmailMessage
```python
@dataclass
class EmailMessage:
    subject: str
    sender: str
    date: datetime
    body_html: str
    body_text: str = ""
```
Raw email from Gmail. `body_html` is the primary content; `body_text` is fallback.

### Article
```python
@dataclass
class Article:
    source: str        # Newsletter name (e.g., "Morning Brew")
    title: str
    content: str       # Cleaned text
    estimated_words: int
    topic: str = ""    # Assigned by topic_classifier (e.g., "Latest in Tech")
```
Single article extracted from an email. One email may produce multiple articles (especially Google Alerts).

### DailyDigest
```python
@dataclass
class DailyDigest:
    articles: list[Article]
    total_words: int
    date: datetime = field(default_factory=lambda: datetime.now(UTC))
```
Collection of all articles for one day, after parsing and classification.

### CompiledDigest
```python
@dataclass
class CompiledDigest:
    text: str                    # Full markdown document
    article_count: int
    total_words: int
    date: str                    # YYYY-MM-DD
    topics_summary: str          # Human-readable topic list
    rss_summary: str = ""        # Short summary for RSS feed
    email_count: int = 0
    segment_counts: dict[str, int] = field(default_factory=dict)    # {"Latest in Tech": 3, ...}
    segment_sources: dict[str, list[str]] = field(default_factory=dict)  # {"Latest in Tech": ["TechCrunch", ...]}
    quality_report: dict = field(default_factory=dict)
```
Output of `digest_compiler.compile()`. The `text` field contains the full structured markdown that gets uploaded to NotebookLM. `segment_counts` and `segment_sources` power the coverage analytics.

### EpisodeMetadata
```python
@dataclass
class EpisodeMetadata:
    date: str              # YYYY-MM-DD
    file_path: Path
    file_size_bytes: int
    duration_seconds: int
    duration_formatted: str  # HH:MM:SS
    topics_summary: str
    rss_summary: str = ""
    gcs_url: str = ""
```
Returned by `episode_manager.process()` after MP3 validation and GCS upload.

## SQLite Schema (`src/database.py`)

All tables are created in `_create_tables()` with auto-migration for columns added after initial release.

### Table: `digests`
| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `date` | TEXT UNIQUE | YYYY-MM-DD, indexed |
| `markdown_text` | TEXT | Full digest document |
| `article_count` | INTEGER | Number of articles |
| `total_words` | INTEGER | Word count |
| `topics_summary` | TEXT | Human-readable topic list |
| `rss_summary` | TEXT | Short RSS summary (migrated column) |
| `email_count` | INTEGER | Number of source emails (migrated) |
| `segment_counts` | TEXT | JSON `{"topic": count}` (migrated) |
| `segment_sources` | TEXT | JSON `{"topic": ["source1", ...]}` (migrated) |
| `quality_report` | TEXT | JSON quality metrics from compiler (migrated) |
| `created_at` | TEXT | ISO 8601 timestamp |

**UPSERT behavior**: `save_digest()` uses `ON CONFLICT(date) DO UPDATE`. Refuses to overwrite if an episode exists for the date (locked), unless `force=True`.

### Table: `episodes`
| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `date` | TEXT UNIQUE | YYYY-MM-DD, indexed |
| `file_size_bytes` | INTEGER | MP3 file size |
| `duration_seconds` | INTEGER | Episode duration |
| `duration_formatted` | TEXT | HH:MM:SS |
| `topics_summary` | TEXT | Topic list |
| `rss_summary` | TEXT | RSS summary |
| `gcs_url` | TEXT | Public GCS URL (migrated) |
| `published_at` | TEXT | ISO 8601 timestamp |
| `audio_segment_words` | TEXT | JSON `{"topic": word_count}` from transcription (migrated) |
| `audio_analysis_status` | TEXT | "none", "pending", "running", "complete", "failed" (migrated) |
| `audio_analysis_full` | TEXT | JSON full transcription analysis (migrated) |

### Table: `pipeline_runs`
| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `run_id` | TEXT | UUID hex (12 chars) |
| `started_at` | TEXT | ISO 8601 |
| `finished_at` | TEXT | ISO 8601 or NULL |
| `status` | TEXT | "running", "success", "failed" |
| `current_step` | TEXT | Last step name |
| `error_message` | TEXT | Error details if failed |
| `steps_log` | TEXT | JSON array of step entries |

Each step entry in `steps_log`:
```json
{"step": "1. Fetch emails", "status": "success", "message": "Fetched 12 emails", "timestamp": "..."}
```

### Table: `findings`
| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `episode_date` | TEXT | Links to episode, indexed |
| `job` | TEXT | Analysis type: "coverage_gap", "tone_issue", "word_budget" |
| `severity` | TEXT | "info", "warning", "critical" |
| `topic` | TEXT | Optional topic name |
| `finding` | TEXT | Human-readable finding |
| `data` | TEXT | JSON with additional context |
| `created_at` | TEXT | ISO 8601 |

### Table: `suggestions`
| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `episode_date` | TEXT | Links to episode, indexed |
| `type` | TEXT | "prompt_edit", "source_add", "source_remove", "manual" |
| `status` | TEXT | "pending", "approved", "dismissed", "snoozed" — indexed |
| `title` | TEXT | Short description |
| `detail` | TEXT | Full explanation |
| `current_value` | TEXT | Current prompt text (for prompt_edit) |
| `suggested_value` | TEXT | Suggested replacement |
| `finding_ids` | TEXT | JSON array of finding IDs that led to this suggestion |
| `reviewed_at` | TEXT | ISO 8601 or NULL |
| `created_at` | TEXT | ISO 8601 |

### Table: `prompt_overrides`
| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `prompt_key` | TEXT UNIQUE | e.g., "digest_system", "digest_preamble" |
| `original_value` | TEXT | Original prompt text |
| `override_value` | TEXT | Approved replacement |
| `approved_from_suggestion_id` | INTEGER FK | Links to suggestions(id) |
| `applied_at` | TEXT | ISO 8601 |

### Indexes
```sql
idx_digests_date ON digests(date)
idx_episodes_date ON episodes(date)
idx_runs_started ON pipeline_runs(started_at)
idx_findings_date ON findings(episode_date)
idx_suggestions_date ON suggestions(episode_date)
idx_suggestions_status ON suggestions(status)
```

## Data Flow

```
Gmail API
   │
   ▼
EmailMessage[]  ──►  content_parser  ──►  DailyDigest (Article[])
                                               │
                                               ▼
                                        digest_compiler  ──►  CompiledDigest
                                               │                    │
                                               │              [in-memory during preparation]
                                               │                    │
                                               ▼                    ▼
                                          digests table    preparation_digest (ShowState)
                                               │
                                               ▼
                              [manual audio upload → .prep.mp3]
                                               │
                                               ▼
                                        episode_manager  ──►  EpisodeMetadata
                                               │                    │
                                               ▼                    ▼
                                        episodes table       GCS (MP3 + DB)
                                               │
                                               ▼
                                        feed_builder  ──►  feed.xml + episodes.json
                                               │
                                               ▼
                                      audio_transcriber  ──►  episodes.audio_analysis_full
                                               │
                                               ▼
                                      episode_analyzer  ──►  findings + suggestions tables
```

## GCS vs SQLite: Division of Responsibility

| Concern | SQLite | GCS |
|---------|--------|-----|
| Digests (text, metadata) | Primary store | Backed up via DB upload |
| Episodes (metadata) | Primary store | Backed up via DB upload |
| Episode MP3 files | Not stored | Primary store (public URLs in RSS) |
| Pipeline runs, findings, suggestions | Primary store | Backed up via DB upload |
| RSS feed (feed.xml) | Not stored | Not stored (rebuilt from DB) |
| Episode catalog (episodes.json) | Not stored | Not stored (rebuilt from DB) |

**Key insight**: SQLite is the operational database. GCS is the persistence layer. On startup, the DB is downloaded from GCS. After writes, the DB is uploaded back (prod only). This is a "download-mutate-upload" pattern, not a distributed database.

## Migration Strategy

New columns are added via `ALTER TABLE` wrapped in try/except:

```python
try:
    conn.execute("ALTER TABLE digests ADD COLUMN rss_summary TEXT NOT NULL DEFAULT ''")
except sqlite3.OperationalError:
    pass  # Column already exists
```

There are currently **9 migrations** (rss_summary, segment_counts, segment_sources, gcs_url, email_count, audio_segment_words, audio_analysis_status, quality_report, audio_analysis_full). Migrations run on every connection open via `_create_tables()`.

## Validation Gaps

1. **No schema validation on JSON columns** — `segment_counts`, `segment_sources`, `quality_report`, `audio_analysis_full`, `steps_log`, `finding_ids`, and `data` are all stored as JSON text with no schema enforcement. Malformed JSON would cause `json.loads()` to raise at read time.

2. **No foreign key enforcement** — the `prompt_overrides.approved_from_suggestion_id` FK is declared but SQLite doesn't enforce FKs by default (requires `PRAGMA foreign_keys=ON`).

3. **Date format not validated at DB level** — dates are stored as TEXT in YYYY-MM-DD format, validated only at the API layer via regex.

4. **No connection pooling** — each function call opens and closes a connection. For the current workload (single operator) this is fine, but would need addressing for concurrent users.

5. **Digest locking is advisory** — `save_digest()` checks `has_episode()` before overwriting, but this is not an atomic operation. A race condition could theoretically overwrite a locked digest.

6. **WAL checkpoint only before GCS upload** — `_checkpoint_wal()` is called in `upload_db()` but not on regular writes. If the process crashes, WAL data is in a separate file that may not be included in backups.
