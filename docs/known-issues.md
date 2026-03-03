# Known Issues

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

### 3. No Rate Limiting on AI Calls
**Severity**: Low
**Status**: Known (mitigated by exponential backoff)

Gemini API calls in `llm_client.py` have retry logic with exponential backoff on 429/5xx errors but no proactive rate limiting or cost controls. Rapidly triggering preparation for multiple shows could generate many concurrent API calls.

### 4. Dashboard is a Single Large HTML File
**Severity**: Low (maintenance concern)
**Status**: Known

`templates/dashboard.html` is ~3800 lines of inline JS and CSS. No build step, no component framework. This works well for a single-operator tool but makes changes error-prone.
