"""Audio transcription and per-topic word count analysis via Gemini API."""

import json
import logging
import time
from pathlib import Path

import requests

from config import settings
from src.exceptions import AudioTranscriptionError
from src.show_bible_context import SHOW_BIBLE_RULES

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com"
UPLOAD_URL = f"{API_BASE}/upload/v1beta/files"
FILES_URL = f"{API_BASE}/v1beta/files"
GENERATE_URL = f"{API_BASE}/v1beta/models/gemini-2.5-flash:generateContent"

MAX_RETRIES = 3
RETRY_BACKOFF = [5, 10, 20]
FILE_POLL_INTERVAL = 5  # seconds
FILE_POLL_TIMEOUT = 120  # seconds
GENERATE_TIMEOUT = 300  # seconds


def _api_key() -> str:
    if not settings.gemini_api_key:
        raise AudioTranscriptionError("No GEMINI_API_KEY configured")
    return settings.gemini_api_key


def upload_to_gemini(mp3_path: Path) -> str:
    """Upload MP3 to Gemini Files API, return file name (e.g. 'files/abc123').

    Uses resumable upload protocol for large files.
    """
    key = _api_key()
    file_size = mp3_path.stat().st_size
    display_name = mp3_path.name

    logger.info("Uploading %s (%d bytes) to Gemini Files API...", display_name, file_size)

    # Step 1: Initiate resumable upload
    metadata = json.dumps({"file": {"display_name": display_name}})
    init_resp = requests.post(
        f"{UPLOAD_URL}?key={key}",
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(file_size),
            "X-Goog-Upload-Header-Content-Type": "audio/mpeg",
            "Content-Type": "application/json",
        },
        data=metadata,
        timeout=30,
    )
    init_resp.raise_for_status()

    upload_url = init_resp.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise AudioTranscriptionError("No upload URL returned from Gemini Files API")

    # Step 2: Upload the file data
    with open(mp3_path, "rb") as f:
        file_data = f.read()

    upload_resp = requests.put(
        upload_url,
        headers={
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
            "Content-Length": str(file_size),
        },
        data=file_data,
        timeout=300,
    )
    upload_resp.raise_for_status()

    file_info = upload_resp.json().get("file", {})
    file_name = file_info.get("name", "")
    file_uri = file_info.get("uri", "")
    state = file_info.get("state", "")

    if not file_name:
        raise AudioTranscriptionError(f"No file name in upload response: {upload_resp.text[:200]}")

    logger.info("File uploaded: %s (state=%s)", file_name, state)

    # Step 3: Poll until file is ACTIVE
    if state != "ACTIVE":
        start_time = time.time()
        while time.time() - start_time < FILE_POLL_TIMEOUT:
            time.sleep(FILE_POLL_INTERVAL)
            status_resp = requests.get(
                f"{FILES_URL}/{file_name}?key={key}",
                timeout=15,
            )
            status_resp.raise_for_status()
            file_info = status_resp.json()
            state = file_info.get("state", "")
            logger.info("File %s state: %s", file_name, state)
            if state == "ACTIVE":
                file_uri = file_info.get("uri", file_uri)
                break
            if state == "FAILED":
                raise AudioTranscriptionError(f"File processing failed: {file_info}")
        else:
            raise AudioTranscriptionError(
                f"File {file_name} did not become ACTIVE within {FILE_POLL_TIMEOUT}s"
            )

    logger.info("File ready: %s (uri=%s)", file_name, file_uri)
    return file_name, file_uri


def analyze_audio(file_uri: str, segment_order: list[str],
                  segment_durations: dict[str, int] | None = None,
                  db_path=None) -> dict:
    """Send audio to Gemini for topic-level word count + coverage/tone analysis.

    Returns dict with keys: word_counts, coverage_gaps, tone_findings,
    runtime_seconds, both_hosts_present.
    """
    key = _api_key()
    wpm = 150  # words per minute

    # Check for prompt overrides from the learning system
    prompt_overrides = {}
    try:
        from src import database
        prompt_overrides = database.get_prompt_overrides(db_path=db_path)
    except Exception:
        pass

    # Build topic list with word budgets for prompt
    topic_lines = []
    budget_lines = []
    for i, name in enumerate(segment_order, 1):
        mins = segment_durations.get(name, 1) if segment_durations else 1
        word_budget = mins * wpm
        topic_lines.append(f"{i}. {name} ({mins} min budget)")
        budget_lines.append(f"- {name}: {word_budget} words (~{mins} min)")
    topics_text = "\n".join(topic_lines)
    budgets_text = "\n".join(budget_lines)

    system_prompt = prompt_overrides.get(
        "transcription_system",
        "You are an audio analyst. You listen to podcast episodes and perform "
        "detailed word count, coverage gap, and tone/framing analysis. Be precise and thorough."
    )

    user_prompt = (
        "Listen to this podcast episode and analyze the spoken content.\n"
        "The podcast has two conversational hosts discussing news and current events.\n"
        f"It covers these topics (in approximate order):\n{topics_text}\n\n"
        "For each topic, count approximately how many words were spoken about it by both hosts combined.\n"
        "Topics may blend during natural transitions — assign words to whichever topic is the primary focus.\n\n"
        "IMPORTANT distinctions:\n"
        "- Exclude intro banter (~1 minute welcome/weather) and outro (~1 minute farewell) from ALL counts.\n"
        "- Exclude general chatter, transitions between topics, jokes, tangents, and filler conversation from ALL counts.\n"
        '- "Misc" should ONLY count words about actual miscellaneous news stories or articles — NOT banter, chatter, or off-topic conversation.\n'
        "- Only count words that are substantively discussing a specific news story, article, or topic.\n\n"
        "COVERAGE GAP ANALYSIS:\n"
        "After counting words, compare each topic's actual word count against its target budget.\n"
        "Flag any topic where actual is more than 30% above or below budget.\n"
        f"Target budgets:\n{budgets_text}\n\n"
        "TONE & FRAMING ANALYSIS:\n"
        f"{SHOW_BIBLE_RULES}\n"
        "Evaluate the audio against these show rules:\n"
        '1. INDIA INSIDER RULE: When covering Indian Politics, Indian Cricket, or Badminton, '
        'the host must speak as someone from India — not as a foreign correspondent. '
        'Flag any "in India, people believe..." or outsider-framing constructions as india_outsider_language (critical).\n'
        "2. TWO HOST RULE: Both hosts must be present and speaking in every segment. "
        "Flag any segment that sounds like a solo monologue as missing_second_host (warning).\n"
        "3. SORKIN RULE: Dialogue should be fast, witty, and warm. "
        "Flag any segment that sounds stiff, robotic, or like a news reader as sorkin_violation (warning).\n\n"
        "RUNTIME: Estimate total runtime in seconds from the audio.\n\n"
        "Return ONLY a JSON object with this exact structure:\n"
        "{\n"
        '  "word_counts": {"Latest in Tech": 523, ...all 14 topics},\n'
        '  "coverage_gaps": [{"topic": "...", "budget_words": 750, "actual_words": 400, '
        '"gap_percent": 47, "direction": "under"}],\n'
        '  "tone_findings": [{"topic": "...", "issue": "correspondent_framing" | '
        '"missing_second_host" | "sorkin_violation" | "india_outsider_language", '
        '"description": "...", "severity": "warning" | "critical"}],\n'
        '  "runtime_seconds": 0,\n'
        '  "both_hosts_present": true\n'
        "}\n"
        "Use the exact topic names listed above as keys in word_counts. "
        "Set count to 0 for topics not discussed at all. "
        "coverage_gaps should only include topics with >30% deviation. "
        "tone_findings can be empty if no issues found."
    )

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{
            "parts": [
                {"file_data": {"file_uri": file_uri, "mime_type": "audio/mpeg"}},
                {"text": user_prompt},
            ]
        }],
        "generationConfig": {
            "maxOutputTokens": 16384,
            "temperature": 0.0,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 8192},
        },
    }

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            logger.info("Analyzing audio (attempt %d/%d)...", attempt + 1, MAX_RETRIES)
            resp = requests.post(
                f"{GENERATE_URL}?key={key}",
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=GENERATE_TIMEOUT,
            )

            if resp.status_code == 429 or resp.status_code >= 500:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                logger.warning(
                    "Gemini API %d (attempt %d/%d), retrying in %ds...",
                    resp.status_code, attempt + 1, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                last_error = f"{resp.status_code}: {resp.text[:200]}"
                continue

            resp.raise_for_status()
            data = resp.json()

            # Check for finish reason issues
            candidate = data.get("candidates", [{}])[0]
            finish_reason = candidate.get("finishReason", "")
            if finish_reason and finish_reason not in ("STOP", ""):
                logger.warning("Gemini finish reason: %s", finish_reason)

            # Find the text part (skip thinking parts)
            text = None
            for part in candidate["content"]["parts"]:
                if "text" in part:
                    text = part["text"]
            if text is None:
                raise AudioTranscriptionError("No text part in Gemini response")
            logger.info("Raw Gemini response (%d chars): %s", len(text), text[:500])

            # Parse JSON response — repair truncated output if needed
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                if finish_reason == "MAX_TOKENS":
                    repaired = text.rstrip().rstrip(",") + "}"
                    logger.warning("Attempting JSON repair for MAX_TOKENS truncation")
                    result = json.loads(repaired)
                else:
                    raise
            if not isinstance(result, dict):
                raise AudioTranscriptionError(f"Expected dict, got {type(result)}: {text[:200]}")

            # Handle both old format (flat word counts) and new format (nested)
            if "word_counts" in result:
                word_counts = result["word_counts"]
            else:
                # Old format: the entire dict is word counts
                word_counts = result
                result = {"word_counts": word_counts, "coverage_gaps": [],
                          "tone_findings": [], "runtime_seconds": 0,
                          "both_hosts_present": True}

            # Validate and normalize word counts: ensure all topics present
            normalized = {}
            for name in segment_order:
                if name in word_counts:
                    normalized[name] = int(word_counts[name])
                else:
                    lower_map = {k.lower(): v for k, v in word_counts.items()}
                    normalized[name] = int(lower_map.get(name.lower(), 0))

            result["word_counts"] = normalized
            total = sum(normalized.values())
            logger.info("Audio analysis complete: %d total words across %d topics, "
                        "%d coverage gaps, %d tone findings",
                        total, sum(1 for v in normalized.values() if v > 0),
                        len(result.get("coverage_gaps", [])),
                        len(result.get("tone_findings", [])))
            return result

        except (requests.exceptions.HTTPError, json.JSONDecodeError, KeyError, ValueError) as e:
            last_error = str(e)
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                logger.warning("Audio analysis error (attempt %d/%d): %s, retrying in %ds...",
                               attempt + 1, MAX_RETRIES, e, wait)
                time.sleep(wait)
            continue

    raise AudioTranscriptionError(f"Audio analysis failed after {MAX_RETRIES} retries: {last_error}")


def delete_from_gemini(file_name: str) -> None:
    """Delete an uploaded file from Gemini Files API."""
    try:
        key = _api_key()
        resp = requests.delete(f"{FILES_URL}/{file_name}?key={key}", timeout=15)
        if resp.ok:
            logger.info("Deleted Gemini file: %s", file_name)
        else:
            logger.warning("Failed to delete Gemini file %s: %s", file_name, resp.status_code)
    except Exception as e:
        logger.warning("Error deleting Gemini file %s: %s", file_name, e)


def transcribe_episode(mp3_path: Path, segment_order: list[str],
                       segment_durations: dict[str, int] | None = None,
                       db_path=None) -> dict:
    """Full pipeline: upload audio → analyze topics → cleanup → return full analysis.

    Args:
        mp3_path: Path to the MP3 file.
        segment_order: List of topic names in order.
        segment_durations: Optional dict of topic name → allocated minutes.
        db_path: Optional database path for loading prompt overrides.

    Returns:
        Full analysis dict with keys: word_counts, coverage_gaps, tone_findings,
        runtime_seconds, both_hosts_present.

    Raises:
        AudioTranscriptionError: If any step fails.
    """
    if not mp3_path.exists():
        raise AudioTranscriptionError(f"MP3 file not found: {mp3_path}")

    file_name = None
    try:
        file_name, file_uri = upload_to_gemini(mp3_path)
        result = analyze_audio(file_uri, segment_order, segment_durations, db_path=db_path)
        return result
    except AudioTranscriptionError:
        raise
    except Exception as e:
        raise AudioTranscriptionError(f"Transcription failed: {e}") from e
    finally:
        if file_name:
            delete_from_gemini(file_name)
