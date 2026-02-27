"""Classify newsletter articles into podcast topic segments using Gemini."""

import json
import logging
import re
from enum import StrEnum

from src.models import Article

logger = logging.getLogger(__name__)


class Topic(StrEnum):
    """Podcast segment topics in presentation order."""

    WORLD_POLITICS = "World Politics"
    US_POLITICS = "US Politics"
    INDIAN_POLITICS = "Indian Politics"
    TECH_AI = "Latest in Tech"
    ENTERTAINMENT = "Entertainment"
    PRODUCT_MANAGEMENT = "Product Management"
    CROSSFIT = "CrossFit"
    F1 = "Formula 1"
    ARSENAL = "Arsenal"
    INDIAN_CRICKET = "Indian Cricket"
    BADMINTON = "Badminton"
    SPORTS = "Sports"
    SEATTLE = "Seattle"
    OTHER = "Misc"


# Ordered list for segment rendering (priority order)
SEGMENT_ORDER: list[Topic] = [
    Topic.TECH_AI,
    Topic.PRODUCT_MANAGEMENT,
    Topic.WORLD_POLITICS,
    Topic.US_POLITICS,
    Topic.INDIAN_POLITICS,
    Topic.CROSSFIT,
    Topic.ENTERTAINMENT,
    Topic.F1,
    Topic.ARSENAL,
    Topic.INDIAN_CRICKET,
    Topic.BADMINTON,
    Topic.SPORTS,
    Topic.SEATTLE,
    Topic.OTHER,
]

# Suggested duration labels per segment (total: 30 minutes)
SEGMENT_DURATIONS: dict[Topic, str] = {
    Topic.TECH_AI: "~5 minutes",
    Topic.PRODUCT_MANAGEMENT: "~4 minutes",
    Topic.WORLD_POLITICS: "~4 minutes",
    Topic.US_POLITICS: "~3 minutes",
    Topic.INDIAN_POLITICS: "~3 minutes",
    Topic.ENTERTAINMENT: "~3 minutes",
    Topic.CROSSFIT: "~2 minutes",
    Topic.F1: "~2 minutes",
    Topic.ARSENAL: "~1 minute",
    Topic.INDIAN_CRICKET: "~1 minute",
    Topic.BADMINTON: "~1 minute",
    Topic.SPORTS: "~1 minute",
    Topic.SEATTLE: "~1 minute",
    Topic.OTHER: "~1 minute",
}

# Transactional senders to filter out entirely
FILTERED_SENDERS: set[str] = {
    "google",
    "google one",
    "google play",
    "google gemini",
    "gmail team",
    "notebooklm",
    "noreply",
    "no-reply",
    "substack",
}

# Valid topic names for parsing Gemini response
_VALID_TOPICS: dict[str, Topic] = {t.value.lower(): t for t in Topic}


def _normalize(text: str) -> str:
    """Normalize text for matching: lowercase, strip, and replace curly quotes."""
    return (
        text.lower()
        .strip()
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def _is_filtered_sender(sender: str) -> bool:
    """Check if an email sender should be filtered out."""
    sender_norm = _normalize(sender)
    return sender_norm in FILTERED_SENDERS


def _build_classification_prompt(articles: list[tuple[int, Article]]) -> tuple[str, str]:
    """Build the system and user prompts for Gemini classification.

    Args:
        articles: List of (original_index, Article) tuples to classify.

    Returns:
        Tuple of (system_prompt, user_message).
    """
    topic_descriptions = "\n".join(
        f"- \"{t.value}\" ({SEGMENT_DURATIONS.get(t, '~1 minute')})"
        for t in Topic
    )

    system = f"""\
You are a news article classifier for a daily podcast. Your job is to assign each article to exactly one topic segment.

AVAILABLE TOPICS (with segment duration):
{topic_descriptions}

CLASSIFICATION RULES:
- Read the source, title, and content snippet of each article carefully.
- Assign the single BEST matching topic based on the article's primary subject matter.
- "Arsenal" = Arsenal Football Club ONLY. Other Premier League / football news goes to "Sports".
- "Indian Cricket" = cricket involving India's national team, IPL, BCCI. Other cricket goes to "Sports".
- "Badminton" = all badminton news regardless of country.
- "Formula 1" = F1, Grand Prix, F1 drivers (Verstappen, Hamilton, etc.).
- "CrossFit" = CrossFit workouts, competitions, CrossFit Games.
- "Seattle" = local Seattle / Washington state / Pacific Northwest news, events, restaurants, crime.
- "Indian Politics" = India government, politics, diplomacy, economy. NOT Indian sports.
- "US Politics" = US government, Congress, White House, elections, policy, economy.
- "World Politics" = international affairs, geopolitics, non-US/non-India politics.
- "Latest in Tech" = technology, AI, startups, software, cybersecurity.
- "Entertainment" = movies, TV, music, Hollywood, streaming, celebrities, books.
- "Product Management" = product strategy, PM frameworks, user research, metrics.
- "Sports" = any sport NOT covered by Arsenal/Indian Cricket/Badminton/F1/CrossFit.
- "Misc" = articles that don't fit any of the above, promotional emails, or trivia.
- If an article is clearly promotional, transactional, or has no news value, classify as "SKIP".

RESPONSE FORMAT:
Return a JSON object mapping article ID to topic name. Example:
{{"0": "Latest in Tech", "3": "Seattle", "5": "SKIP"}}

Return ONLY the JSON object, no other text."""

    # Build article list — truncate content to ~200 words to save tokens
    article_lines = []
    for idx, article in articles:
        content_words = article.content.split()
        snippet = " ".join(content_words[:200])
        if len(content_words) > 200:
            snippet += "..."
        article_lines.append(
            f"[{idx}] Source: {article.source}\n"
            f"    Title: {article.title}\n"
            f"    Content: {snippet}"
        )

    user_message = "Classify these articles:\n\n" + "\n\n".join(article_lines)

    return system, user_message


def _parse_gemini_response(response_text: str, article_count: int) -> dict[int, Topic | None]:
    """Parse Gemini's JSON response into a mapping of index -> Topic.

    Args:
        response_text: Raw text from Gemini (should be JSON).
        article_count: Expected number of articles (for validation).

    Returns:
        Mapping of article index to Topic (or None for SKIP).
    """
    # Strip markdown code fences if present
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    # Fix truncated JSON: if it doesn't end with }, try to close it
    cleaned = cleaned.strip()
    if not cleaned.endswith("}"):
        # Truncated — remove the last incomplete line and close the object
        last_complete = cleaned.rfind(",")
        if last_complete > 0:
            cleaned = cleaned[:last_complete] + "}"
        else:
            cleaned = cleaned + "}"

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error("Failed to parse Gemini classification response: %s", response_text[:500])
        return {}

    results: dict[int, Topic | None] = {}
    for key, value in data.items():
        try:
            idx = int(key)
        except ValueError:
            continue

        if isinstance(value, str):
            value_lower = value.strip().lower()
            if value_lower == "skip":
                results[idx] = None
            elif value_lower in _VALID_TOPICS:
                results[idx] = _VALID_TOPICS[value_lower]
            else:
                logger.warning("Unknown topic from Gemini: '%s' for article %d", value, idx)
                results[idx] = Topic.OTHER

    return results


def classify_articles_batch(articles: list[Article]) -> dict[int, Topic | None]:
    """Classify articles by sending them to Gemini in a single batch.

    Filters transactional senders first, then sends all remaining articles
    to Gemini Flash for classification. Falls back to keyword-based
    classification if the API call fails.

    Args:
        articles: Articles to classify (must have source, title, content set).

    Returns:
        Mapping of article list index to Topic (or None if filtered/skipped).
    """
    results: dict[int, Topic | None] = {}
    to_classify: list[tuple[int, Article]] = []

    # Pre-filter transactional senders
    for i, article in enumerate(articles):
        if _is_filtered_sender(article.source):
            results[i] = None
        else:
            to_classify.append((i, article))

    if not to_classify:
        return results

    # Try Gemini classification
    try:
        from src.llm_client import call_fast

        system_prompt, user_message = _build_classification_prompt(to_classify)
        response = call_fast(system_prompt, user_message, max_tokens=8192, timeout=60)
        gemini_results = _parse_gemini_response(response, len(to_classify))

        if gemini_results:
            results.update(gemini_results)
            # Check for any articles Gemini missed and default to Misc
            for idx, _article in to_classify:
                if idx not in results:
                    logger.warning("Gemini missed article %d ('%s'), defaulting to Misc",
                                   idx, _article.title[:60])
                    results[idx] = Topic.OTHER

            classified_count = sum(1 for v in gemini_results.values() if v is not None)
            skipped_count = sum(1 for v in gemini_results.values() if v is None)
            logger.info("Gemini classified %d articles (%d skipped)", classified_count, skipped_count)
            return results

        logger.warning("Gemini returned empty results, falling back to keyword classification")

    except Exception as e:
        logger.warning("Gemini classification failed (%s), falling back to keywords", e)

    # Fallback: keyword-based classification
    for idx, article in to_classify:
        results[idx] = _classify_by_keywords(article)

    return results


# --- Keyword fallback (used when Gemini is unavailable) ---

# Keyword lists for fallback classification
_TOPIC_KEYWORDS: dict[Topic, list[str]] = {
    Topic.WORLD_POLITICS: [
        r"\bUN\b", r"\bNATO\b", r"\bEU\b", r"\bglobal\b",
        r"\bwar\b", r"\bconflict\b", r"\bUkraine\b", r"\bRussia\b",
        r"\bChina\b", r"\bIsrael\b", r"\bPalestine\b", r"\bGaza\b",
        r"\bIran\b", r"\bgeopolit", r"\bceasefire\b", r"\bdiplomat",
    ],
    Topic.US_POLITICS: [
        r"\bCongress\b", r"\bSenate\b", r"\bWhite House\b",
        r"\bPresident\b", r"\bRepublican", r"\bDemocrat",
        r"\bTrump\b", r"\bBiden\b", r"\belection\b", r"\bfederal\b",
    ],
    Topic.INDIAN_POLITICS: [
        r"\bIndia\b.*\b(?:government|politic|minister|parliament)",
        r"\bModi\b", r"\bBJP\b", r"\bDelhi\b",
        r"\bLok Sabha\b", r"\bRajya Sabha\b", r"\bRBI\b",
    ],
    Topic.TECH_AI: [
        r"\bAI\b", r"\bartificial intelligence\b", r"\bGPT\b",
        r"\bOpenAI\b", r"\btech\b", r"\bsoftware\b", r"\bLLM\b",
        r"\bstartup\b", r"\bcrypto\b", r"\bmachine learning\b",
    ],
    Topic.ENTERTAINMENT: [
        r"\bmovie\b", r"\bfilm\b", r"\bNetflix\b", r"\bstreaming\b",
        r"\bbox office\b", r"\bOscar", r"\bmusic\b", r"\bconcert\b",
        r"\bHollywood\b", r"\bcelebrit",
    ],
    Topic.CROSSFIT: [r"\bCrossFit\b", r"\bWOD\b", r"\bAMRAP\b", r"\bEMOM\b"],
    Topic.F1: [
        r"\bFormula 1\b", r"\bF1\b", r"\bGrand Prix\b",
        r"\bVerstappen\b", r"\bHamilton\b", r"\bMcLaren\b",
    ],
    Topic.ARSENAL: [r"\bArsenal\b", r"\bGunners\b", r"\bArteta\b", r"\bEmirates Stadium\b"],
    Topic.INDIAN_CRICKET: [
        r"\bIPL\b", r"\bBCCI\b", r"\bIndia\b.*\bcricket",
        r"\bcricket\b.*\bIndia", r"\bKohli\b", r"\bBumrah\b",
    ],
    Topic.BADMINTON: [r"\bbadminton\b", r"\bBWF\b", r"\bSindhu\b", r"\bSrikanth\b"],
    Topic.SPORTS: [
        r"\bNFL\b", r"\bNBA\b", r"\bMLB\b", r"\bOlympic",
        r"\btennis\b", r"\bgolf\b", r"\bUFC\b", r"\bsoccer\b",
    ],
    Topic.SEATTLE: [
        r"\bSeattle\b", r"\bPuget Sound\b", r"\bKing County\b",
        r"\bSeahawks\b", r"\bCapitol Hill\b", r"\bWA\b",
        r"\bPierce County\b", r"\bTacoma\b", r"\bBellevue\b",
    ],
}


# Source-based hints for keyword fallback
_SOURCE_TOPIC_MAP: dict[str, Topic] = {
    "the neuron": Topic.TECH_AI, "cassidoo": Topic.TECH_AI,
    "tldr": Topic.TECH_AI, "ben's bites": Topic.TECH_AI,
    "the verge": Topic.TECH_AI, "peter steinberger": Topic.TECH_AI,
    "lenny's newsletter": Topic.PRODUCT_MANAGEMENT,
    "the product compass": Topic.PRODUCT_MANAGEMENT,
    "department of product": Topic.PRODUCT_MANAGEMENT,
    "aakash gupta": Topic.PRODUCT_MANAGEMENT,
    "the athletic pulse": Topic.ENTERTAINMENT, "the athletic": Topic.ENTERTAINMENT,
    "the hollywood reporter": Topic.ENTERTAINMENT, "thr breaking news": Topic.ENTERTAINMENT,
    "thr today in entertainment": Topic.ENTERTAINMENT,
    "polygon": Topic.ENTERTAINMENT, "kirkus reviews": Topic.ENTERTAINMENT,
    "morning chalk up": Topic.CROSSFIT, "wodwell": Topic.CROSSFIT,
    "the hindu": Topic.INDIAN_POLITICS, "the hindu on tech": Topic.INDIAN_POLITICS,
    "the indian express": Topic.INDIAN_POLITICS, "mint": Topic.INDIAN_POLITICS,
    "the chai brief": Topic.INDIAN_POLITICS, "chai brief": Topic.INDIAN_POLITICS,
    "capitol hill seattle": Topic.SEATTLE, "capitolhillseattle.com": Topic.SEATTLE,
}

# Google Alert label → Topic (for fallback)
_GOOGLE_ALERT_MAP: dict[str, Topic] = {
    "seattle": Topic.SEATTLE, "arsenal": Topic.ARSENAL,
    "badminton": Topic.BADMINTON, "formula 1": Topic.F1, "f1": Topic.F1,
    "india cricket": Topic.INDIAN_CRICKET, "indian cricket": Topic.INDIAN_CRICKET,
    "cricket": Topic.INDIAN_CRICKET, "max verstappen": Topic.F1,
    "verstappen": Topic.F1, "crossfit": Topic.CROSSFIT,
}


def _classify_by_keywords(article: Article) -> Topic:
    """Classify an article using source map + keyword scoring (fallback)."""
    source_lower = _normalize(article.source)

    # Google Alert direct mapping
    alert_match = re.search(r"google alerts?\s*\((.+?)\)", source_lower)
    if alert_match:
        label = alert_match.group(1).strip()
        if label in _GOOGLE_ALERT_MAP:
            return _GOOGLE_ALERT_MAP[label]

    # Known single-topic sources
    for source_key, topic in _SOURCE_TOPIC_MAP.items():
        if source_key in source_lower or source_lower in source_key:
            return topic

    # Keyword scoring
    text = f"{article.title} {article.content[:2000]}"
    best_topic = Topic.OTHER
    best_score = 0

    for topic in Topic:
        if topic == Topic.OTHER:
            continue
        keywords = _TOPIC_KEYWORDS.get(topic, [])
        score = sum(1 for p in keywords if re.search(p, text, re.IGNORECASE))
        if score > best_score:
            best_score = score
            best_topic = topic

    if best_score < 1:
        best_topic = Topic.OTHER

    return best_topic


# Keep single-article classify for backward compat
def classify_article(article: Article) -> Topic | None:
    """Classify a single article."""
    if _is_filtered_sender(article.source):
        return None
    return _classify_by_keywords(article)
