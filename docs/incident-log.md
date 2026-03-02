# Incident Log

A record of bugs encountered in production or development, their root cause, and how they were resolved. Add new incidents at the top.

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

**Root cause:** JavaScript syntax errors in `templates/dashboard.html` caused by double-escaped apostrophes (`\\'` instead of `\'`) in plain JS string literals. Six instances across lines 1173, 1174, 1190, 1191, 1269, 1279, and 1355 had patterns like:
```js
const introText = 'Here\\'s what\\'s happening.';
//                      ^^ this is TWO backslashes
```
In JS, `\\` is a literal backslash, then `'` terminates the string, then `s` is a syntax error. A single syntax error anywhere in the `<script>` block kills ALL JS execution — nothing loads, no errors surface in Python logs, and every tab stays on its default "Loading..." HTML.

The `\\'` pattern IS correct when building HTML onclick attributes (e.g., `onclick="fn(\\'" + val + "\\')"`) because the HTML parser interprets `\\'` as `\'` in the attribute. But in plain JS string assignments, it's a syntax error.

**Why it appeared "random":** The broken code paths were in the `renderShowFormat()` function (intro/outro text) and the `loadLatest()` function (empty state and digest card). These only execute when:
- The Settings tab renders the show format card
- There are no episodes yet (empty state message)
- A digest exists (Today's Digest label)

So the bug was always present but only triggered certain UI states.

**Fix:**
1. Changed 6 instances of `\\'` to `\'` in plain JS strings (lines 1173, 1174, 1190, 1191, 1269, 1279, 1355)
2. Left `\\'` intact in onclick HTML attribute construction (those are correct)
3. Added `.catch()` to the init promise chain as defense-in-depth
4. Added `setTimeout` safety net and `unhandledrejection` handler
5. Verified zero JS syntax errors using `node --check` on the extracted JS

**Eval added:** `dashboard-not-stuck-loading` — verifies the served HTML contains error handling. `dashboard-js-no-syntax-errors` — extracts the JS from served HTML and verifies no double-escaped apostrophes exist in plain string contexts.

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
