# Known Issues

## Active Issues

### 1. No Authentication on Dashboard or API
**Severity**: High
**Status**: By design (single-operator tool), but risky if URL leaks

The dashboard and all API endpoints (except `/api/cron/generate`) have no authentication. Anyone with the Replit URL can view digests, upload audio, publish episodes, and modify prompt configuration. The cron endpoint is protected by `CRON_SECRET`.

**Mitigation**: Keep the deployment URL private. Consider adding basic auth or API key middleware if sharing access.

### 2. Preparation State Lost on Restart
**Severity**: Medium
**Status**: By design

The in-memory `ShowState` (preparation digest, generation status, uploaded `.prep.mp3`) is lost when the server restarts. If the operator has prepared a digest and uploaded audio but hasn't published, a Replit restart means they must re-prepare.

**Mitigation**: Publish promptly after uploading audio. The published episode and digest are persisted to DB and GCS.

### 3. Pre-existing Test Failures
**Severity**: Low
**Status**: Known, not yet fixed

Several test files have pre-existing failures unrelated to the router refactor:
- `tests/test_database.py` — references old `DB_PATH` global that no longer exists (migrated to `db_path` parameter)
- `tests/test_feed_builder.py` — same issue with `DB_PATH` global
- `tests/test_content_parser.py::test_parse_emails_falls_back_to_text` — test expectation mismatch
- `tests/test_topic_classifier.py` — import error

These tests were written for an older code structure and need updating to use the `db_path=` parameter pattern.

### 4. GCS Upload Failures Are Silent
**Severity**: Medium
**Status**: By design (non-fatal)

`gcs_storage.upload_db()` catches all exceptions and returns `False`. If GCS is down or credentials expire, the app continues operating but data is not backed up. There's no alerting mechanism.

**Impact**: If the Replit container restarts after a failed GCS upload, the latest state (new episodes, learning data) could be lost.

### 5. Foreign Keys Not Enforced
**Severity**: Low
**Status**: Known

SQLite foreign key enforcement requires `PRAGMA foreign_keys=ON`, which is not set. The `prompt_overrides.approved_from_suggestion_id` FK reference to `suggestions(id)` is declared but not enforced.

### 6. No Rate Limiting on AI Calls
**Severity**: Low
**Status**: Known

Gemini API calls in `llm_client.py` have retry logic but no rate limiting or cost controls. Rapidly triggering preparation for multiple shows could generate many concurrent API calls.

### 8. ffmpeg Shared Library I/O Error on Audio Upload
**Severity**: High (intermittent, blocks episode publishing)
**Status**: Known, transient Nix store issue

When uploading non-MP3 audio (e.g., M4A, WAV), ffmpeg conversion fails with:
```
Audio conversion failed: /nix/store/jfybfbnknyiwggcrhi4v9rsx5g4hksvf-ffmpeg-full-6.1.1-bin/bin/ffmpeg:
error while loading shared libraries:
/nix/store/za8jy1778jj4mm2xbq76krpiwqdk2j93-libjack2-1.9.22/lib/libjack.so.0:
cannot read file data: Input/output error
```

This is a **transient Nix store I/O error** on Replit — the ffmpeg binary itself is fine, but one of its shared libraries (`libjack.so.0`) intermittently becomes unreadable. The issue is at the Nix/filesystem level, not in the application code.

**Workaround**: Retry the upload — the error is transient and usually resolves on the next attempt. Alternatively, convert to MP3 locally before uploading (MP3 uploads skip ffmpeg entirely).

**Root cause**: Replit's Nix store can experience I/O errors when the underlying filesystem has hiccups. The ffmpeg binary links against many shared libraries, and `libjack2` (audio jack support) is one that occasionally fails to load.

### 7. Dashboard is a Single Large HTML File
**Severity**: Low (maintenance concern)
**Status**: Known

`templates/dashboard.html` is ~3800 lines of inline JS and CSS. No build step, no component framework. This works well for a single-operator tool but makes changes error-prone.

## Resolved Issues

### Resolved: Missing `/api/cancel-preparation` Endpoint
**Fixed in**: Router extraction (Hour 3)
The cancel-preparation endpoint was lost during the initial router extraction from main.py. The dashboard JS calls this endpoint, causing a silent 404 when users tried to cancel. Restored in `routers/pipeline.py`.

### Resolved: Tests Broken After Router Refactor
**Fixed in**: Router extraction (Hour 3)
`tests/test_preparation_workflow.py` referenced old flat globals (`main._preparation_active`, `main.EPISODES_DIR`, etc.) that were replaced by per-show `ShowState`. Test fixture rewritten to create a `ShowConfig` with temp paths and inject `ShowState` into `_show_states`.
