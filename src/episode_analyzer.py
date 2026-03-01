"""Episode Analyzer — runs after audio transcription completes.

Takes quality report (from digest compilation) and audio analysis (from transcription)
and generates findings + suggestions stored in SQLite.
"""

import json
import logging
import time

import requests

from config import settings
from src import database
from src.exceptions import LLMAPIError

logger = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

EGREGIOUS_THRESHOLDS = {
    "runtime_outside_window": (28 * 60, 38 * 60),  # 28-38 minutes in seconds
    "coverage_gaps_critical": 3,
    "tone_failures_critical": 4,
    "quality_score_critical": 50,
}


def _gemini_json_call(prompt: str, temperature: float = 0.2) -> list | dict:
    """Make a Gemini API call expecting JSON response."""
    key = settings.gemini_api_key
    if not key:
        raise LLMAPIError("No GEMINI_API_KEY configured")

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 4096,
            "temperature": temperature,
            "responseMimeType": "application/json",
        },
    }

    resp = requests.post(
        f"{API_URL}?key={key}",
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )

    if resp.status_code == 429 or resp.status_code >= 500:
        time.sleep(10)
        resp = requests.post(
            f"{API_URL}?key={key}",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )

    resp.raise_for_status()
    data = resp.json()
    candidate = data.get("candidates", [{}])[0]
    text = candidate["content"]["parts"][0]["text"]
    return json.loads(text)


def analyze_episode(
    episode_date: str,
    audio_analysis: dict,
    quality_report: dict,
    db_path: str,
) -> dict:
    """Main entry point. Called after transcription completes.

    Returns summary of findings and suggestions created.
    """
    # Guard: skip if audio_analysis is empty or missing word_counts
    if not audio_analysis or not audio_analysis.get("word_counts"):
        logger.warning("Empty audio_analysis for %s — skipping learning analysis", episode_date)
        return {"findings_count": 0, "suggestions_count": 0, "is_egregious": False, "skipped": True}

    findings = []

    # --- Job 1: Coverage Gap Analysis ---
    for gap in audio_analysis.get("coverage_gaps", []):
        gap_pct = abs(gap.get("gap_percent", 0))
        severity = "critical" if gap_pct > 40 else "warning"
        findings.append({
            "job": "coverage_gap",
            "severity": severity,
            "topic": gap.get("topic"),
            "finding": (
                f"{gap.get('topic', '?')} was {gap.get('direction', '?')} budget by "
                f"{gap_pct}% ({gap.get('actual_words', 0)} vs "
                f"{gap.get('budget_words', 0)} target words)"
            ),
            "data": gap,
        })

    # --- Job 2: Tone & Framing Analysis ---
    for tone in audio_analysis.get("tone_findings", []):
        findings.append({
            "job": "tone_framing",
            "severity": tone.get("severity", "warning"),
            "topic": tone.get("topic"),
            "finding": tone.get("description", "Tone issue detected"),
            "data": tone,
        })

    # --- Job 3: Digest Quality ---
    for issue in quality_report.get("issues", []):
        severity = "critical" if issue.get("type") == "missing_thread" else "warning"
        findings.append({
            "job": "digest_quality",
            "severity": severity,
            "topic": issue.get("topic"),
            "finding": issue.get("description", "Quality issue detected"),
            "data": issue,
        })

    # --- Runtime check ---
    runtime = audio_analysis.get("runtime_seconds", 0)
    low, high = EGREGIOUS_THRESHOLDS["runtime_outside_window"]
    if runtime > 0 and (runtime < low or runtime > high):
        findings.append({
            "job": "coverage_gap",
            "severity": "warning",
            "topic": None,
            "finding": (
                f"Episode runtime {runtime // 60}m {runtime % 60}s "
                f"is outside the 28-38 minute target window"
            ),
            "data": {"runtime_seconds": runtime},
        })

    # Save findings to DB
    from pathlib import Path
    finding_ids = database.save_findings(episode_date, findings, db_path=Path(db_path))

    # --- Egregious alert check ---
    is_egregious = _check_egregious(audio_analysis, quality_report, findings)

    # --- Call 4: Generate suggestions via Gemini ---
    suggestions = []
    if findings:
        try:
            suggestions = _generate_suggestions(
                episode_date, findings, finding_ids, is_egregious,
            )
        except Exception as e:
            logger.error("Suggestion generation failed for %s: %s", episode_date, e)

    # Save suggestions to DB
    if suggestions:
        database.save_suggestions(episode_date, suggestions, db_path=Path(db_path))

    return {
        "findings_count": len(findings),
        "suggestions_count": len(suggestions),
        "is_egregious": is_egregious,
    }


def _check_egregious(audio_analysis: dict, quality_report: dict,
                     findings: list[dict]) -> bool:
    """Episode is egregious only if it hits 2+ critical thresholds."""
    # An episode with no audio data is unknown, not egregious
    if not audio_analysis or not audio_analysis.get("word_counts"):
        return False

    hits = 0

    runtime = audio_analysis.get("runtime_seconds", 0)
    low, high = EGREGIOUS_THRESHOLDS["runtime_outside_window"]
    if runtime > 0 and (runtime < low or runtime > high):
        hits += 1

    critical_gaps = sum(
        1 for f in findings
        if f["job"] == "coverage_gap" and f["severity"] == "critical"
    )
    if critical_gaps >= EGREGIOUS_THRESHOLDS["coverage_gaps_critical"]:
        hits += 1

    tone_failures = sum(1 for f in findings if f["job"] == "tone_framing")
    if tone_failures >= EGREGIOUS_THRESHOLDS["tone_failures_critical"]:
        hits += 1

    if quality_report.get("overall_score", 100) < EGREGIOUS_THRESHOLDS["quality_score_critical"]:
        hits += 1

    return hits >= 2


def _generate_suggestions(
    episode_date: str,
    findings: list[dict],
    finding_ids: list[int],
    is_egregious: bool,
) -> list[dict]:
    """Single Gemini call that takes all findings and returns structured suggestions."""
    findings_text = "\n".join(
        f"[{i + 1}] ({f['severity'].upper()}) {f['job']}: {f['finding']}"
        for i, f in enumerate(findings)
    )

    prompt = f"""You are analyzing a daily podcast episode called The Hootline.
Here are the findings from today's episode ({episode_date}):

{findings_text}

Based on these findings, generate a list of actionable suggestions for the podcast producer.
Each suggestion must be one of these types:
- prompt_edit: suggest a specific change to the Gemini digest compiler or audio transcription prompt
- subscription_flag: suggest adding or removing a newsletter subscription for a specific topic
- reclassification: suggest moving content from one topic category to another
- bad_episode_alert: flag this episode as needing attention (only if truly egregious)

For prompt_edit suggestions, you must include:
- current_value: the specific part of the prompt that needs changing (quote it exactly or describe it)
- suggested_value: the exact replacement text

Return ONLY a JSON array of suggestions:
[
  {{
    "type": "prompt_edit" | "subscription_flag" | "reclassification" | "bad_episode_alert",
    "title": "short label (max 8 words)",
    "detail": "full explanation of what to do and why (2-3 sentences)",
    "current_value": "current prompt text or null",
    "suggested_value": "suggested replacement or null",
    "finding_indices": [1, 2]
  }}
]

Rules:
- Only generate suggestions where findings are clear and actionable
- For bad_episode_alert, only generate if is_egregious is true
- Group related findings into one suggestion where possible
- Maximum 5 suggestions per episode
- is_egregious: {str(is_egregious).lower()}
"""

    try:
        raw = _gemini_json_call(prompt, temperature=0.2)
        if not isinstance(raw, list):
            raw = [raw] if isinstance(raw, dict) else []
        # Map finding indices back to IDs
        for s in raw:
            indices = s.pop("finding_indices", [])
            s["finding_ids"] = [
                finding_ids[i - 1]
                for i in indices
                if isinstance(i, int) and 0 < i <= len(finding_ids)
            ]
        return raw[:5]  # Max 5 suggestions
    except Exception as e:
        logger.error("Suggestion generation Gemini call failed: %s", e)
        return []


def run_weekly_trend_analysis(db_path: str) -> list[dict]:
    """Call 5 — weekly only.

    Looks at the last 7 episodes' findings to detect empty category trends.
    Returns subscription suggestions.
    """
    from pathlib import Path
    trends = database.get_recent_coverage_gap_trends(days=7, db_path=Path(db_path))

    if not trends:
        return []

    # Only proceed if any topic was flagged 3+ times
    frequent = [t for t in trends if t["count"] >= 3]
    if not frequent:
        logger.info("No topics flagged 3+ times in last 7 days, skipping weekly analysis")
        return []

    trends_text = "\n".join(
        f"- {t['topic']}: flagged {t['count']} times in last 7 episodes"
        for t in frequent
    )

    prompt = f"""The Hootline podcast has 14 topic segments.
Over the last 7 episodes, these topics had repeated coverage gaps:

{trends_text}

For any topic flagged 3 or more times, suggest specific newsletter subscriptions to add to Gmail
that would improve coverage of that topic. Focus on high-quality, free newsletters.

Return ONLY a JSON array:
[
  {{
    "type": "subscription_flag",
    "title": "Add newsletters for [topic]",
    "detail": "Suggested newsletters: [name] ([url]) — [one line why it helps]",
    "current_value": null,
    "suggested_value": null,
    "finding_ids": []
  }}
]
"""

    try:
        raw = _gemini_json_call(prompt, temperature=0.3)
        if not isinstance(raw, list):
            return []
        # Save as suggestions with a synthetic episode_date
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if raw:
            database.save_suggestions(today, raw, db_path=Path(db_path))
            logger.info("Weekly trend analysis: %d subscription suggestions", len(raw))
        return raw
    except Exception as e:
        logger.error("Weekly trend analysis failed: %s", e)
        return []
