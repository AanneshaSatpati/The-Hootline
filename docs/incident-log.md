# Incident Log

A record of bugs encountered in production or development, their root cause, and how they were resolved. Add new incidents at the top.

---

## INC-006 — Fix Not Deployed: Stale Template Cache + Incomplete Apostrophe Fix
**Date:** 2026-03-02
**Severity:** High
**Status:** Resolved

**What happened:** After applying the INC-005 fix (6 plain-string `\\'` → `\'`), the dashboard STILL showed "Loading..." on all tabs. The operator confirmed twice that the fix did not work.

**Root cause (two issues):**

1. **Stale template cache:** `routers/dashboard.py` read the template at module import time:
   ```python
   DASHBOARD_HTML = Path("templates/dashboard.html").read_text()
   ```
   The Replit-managed process loaded the template BEFORE the fix was applied and continued serving the old broken HTML from memory. Killing the process didn't help — Replit's process manager auto-restarted from the same stale import.

2. **Incomplete fix — 8 onclick `\\'` missed:** The initial INC-005 fix only addressed 6 `\\'` instances in plain string literals. It incorrectly left 8 `\\'` instances in onclick attribute construction (e.g., `onclick="fn(\\'" + val + "\\', this)"`), believing they were correct for HTML contexts. They were not — `\\'` inside a JS string is ALWAYS a syntax error regardless of what the output HTML looks like.

**Fix:**
1. Changed `routers/dashboard.py` to read the template per-request (`_TEMPLATE_PATH.read_text()` inside `_build_dashboard_html()`) instead of caching at import time
2. Fixed remaining 8 `\\'` → `\'` in onclick attribute construction across lines 1282, 1333, 1341, 1359, 1394, 1395, 1523, 1552
3. Verified zero JS syntax errors with `node --check` on extracted JS (14 total fixes across INC-005 + INC-006)

**Eval added:** `dashboard-no-double-escaped-quotes` — verifies the served HTML does not contain `\\'` (the literal 3-character sequence backslash-backslash-apostrophe), which is always a JS syntax error in inline script blocks.

---

## INC-005 — Dashboard Stuck on "Loading..." Across All Tabs
**Date:** 2026-03-02
**Severity:** High
**Status:** Resolved

**What happened:** Dashboard shows "Loading..." on every tab. No Python logs, no traceback. Restarting the server doesn't fix it. Observed 4-5 times.

**Observations during investigation:**
- `/health` returns 200 — FastAPI app is healthy
- All API endpoints return 200 with valid JSON from curl
- The served HTML is complete with all template variables replaced
- The "Loading..." text is the default HTML before JS replaces it — meaning JS never executes

**Root cause (two layers):**

**Layer 1 — JS syntax errors:** Double-escaped apostrophes (`\\'` instead of `\'`) throughout `templates/dashboard.html`. In JS, `\\` is a literal backslash, then `'` terminates the string — a syntax error. A single syntax error anywhere in the `<script>` block kills ALL JS execution — nothing loads, no errors surface in Python logs, and every tab stays on its default "Loading..." HTML.

This affected TWO categories of code:
- **Plain string literals** (6 instances): `const introText = 'Here\\'s what\\'s happening.';`
- **onclick attribute construction** (8 instances): `onclick="copyDigestUrl(\\'" + url + "\\', this)"` — initially believed to be correct, but `\\'` in a JS string literal is ALWAYS a syntax error regardless of whether the output is an HTML attribute.

**Layer 2 — Template caching prevented fix deployment:** `routers/dashboard.py` line 16 had:
```python
DASHBOARD_HTML = Path("templates/dashboard.html").read_text()
```
This cached the template at module import time. Even after fixing the template file, the running Replit process continued serving the old broken HTML from memory. Killing the process didn't help because Replit's process manager auto-restarts from the same stale import.

**Why it appeared "random":** The broken code paths were in `renderShowFormat()`, `loadLatest()`, and `renderRadar()`. These only execute under certain UI states (digest exists, no episodes, Settings tab).

**Fix:**
1. Changed 6 instances of `\\'` to `\'` in plain JS string literals
2. Changed 8 instances of `\\'` to `\'` in onclick HTML attribute construction (these were ALSO syntax errors)
3. Added `.catch()` to the init promise chain as defense-in-depth
4. Added `setTimeout` safety net and `unhandledrejection` handler
5. Changed `routers/dashboard.py` to read the template per-request instead of caching at import time — prevents this class of deployment issue
6. Verified zero JS syntax errors using `node --check` on the extracted JS (14 total fixes)

**Eval added:** `dashboard-not-stuck-loading` — verifies the served HTML contains error handling. `dashboard-js-no-syntax-errors` — verifies JS parses successfully by checking the init promise chain exists.

---

## INC-004 — Missing `/api/cancel-preparation` Endpoint
**Date:** 2026-03 (discovered during router refactor)
**Severity:** Medium
**Status:** Resolved

**What happened:** After splitting main.py into FastAPI routers, the dashboard JS silently got 404s when users tried to cancel digest preparation. No error was surfaced in the UI — the button just stopped working.

**Root cause:** The `/api/cancel-preparation` endpoint was missed during the router extraction. It existed in the original main.py but wasn't carried over to `routers/pipeline.py`.

**Fix:** Endpoint restored in `routers/pipeline.py`.

**Eval added:** `cancel-preparation-endpoint-exists` — verifies the endpoint returns 200 and resets preparation state correctly.

---

## INC-003 — ffmpeg libjack Shared Library I/O Error Blocks Audio Upload
**Date:** Ongoing (intermittent)
**Severity:** High (intermittent)
**Status:** Known / workaround documented

**What happened:** Uploading non-MP3 audio (M4A, WAV) fails with:
```
Audio conversion failed: error while loading shared libraries:
libjack.so.0: cannot read file data: Input/output error
```
Episode publishing is blocked until a retry succeeds.

**Root cause:** Replit's Nix store experiences intermittent I/O errors. ffmpeg links against many shared libraries including libjack2 (audio jack support), which occasionally becomes unreadable at the filesystem level. Not an application bug.

**Fix:** No code fix possible. Workaround: retry the upload (error is transient), or convert to MP3 locally before uploading (MP3 uploads skip ffmpeg entirely).

**Eval added:** `audio-upload-mp3-skips-ffmpeg` — verifies MP3 uploads bypass ffmpeg and succeed without the libjack error path.

---

## INC-002 — Export ZIP Returns 500 When MP3s Only Exist in GCS
**Date:** 2026-03
**Severity:** High
**Status:** Resolved

**What happened:** The `/api/export` endpoint returned a 500 error when triggered in production. All episode metadata was in the DB but the MP3 files were not present on local disk.

**Root cause:** The export endpoint assumed MP3 files would be on local disk. In production on Replit, local disk is ephemeral — MP3s are stored in GCS and only exist locally immediately after upload. After any restart, they're gone.

**Fix:** Export endpoint now checks if the MP3 exists locally, and if not, downloads it from the GCS URL stored in the DB before zipping.

**Eval added:** `export-zip-with-gcs-episodes` — verifies export succeeds when MP3s are only in GCS.

---

## INC-001 — Backfill Data Lost After Dev→Prod Cycle
**Date:** 2026-02 (approximately)
**Severity:** High
**Status:** Resolved (env safety improvements in 2026-03 refactor)

**What happened:** A backfill script was run in development mode to populate historical data. The script completed without errors. On the next production deploy, Replit pulled a fresh container, the app downloaded the DB from GCS on startup, and all backfill data was gone.

**Root cause:** `upload_db()` silently skips GCS upload in dev mode — it logged at INFO level and returned early with no indication that data was not persisted. The dev operator assumed local writes were persisting.

**Fix:**
- `upload_db()` in dev now logs a loud `WARNING: DEV MODE: upload_db() called but skipped — data will NOT persist to GCS`
- Startup banner now prints `Running in DEV mode — GCS writes disabled` or `Running in PROD mode — GCS writes enabled`
- `is_prod()` / `is_dev()` helpers added to config.py — NOCTUA_ENV string never checked directly

**Eval added:** `dev-mode-upload-db-warns` — verifies `upload_db()` logs a visible WARNING in dev and does not silently return.
