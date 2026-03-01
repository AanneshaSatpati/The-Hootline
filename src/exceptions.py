"""Custom exception hierarchy for Noctua."""


class NoctuaError(Exception):
    """Base exception for all Noctua errors."""


class EmailFetchError(NoctuaError):
    """Raised when fetching emails from Gmail fails."""


class ContentParseError(NoctuaError):
    """Raised when parsing email content fails."""


class DigestCompileError(NoctuaError):
    """Raised when compiling the daily digest fails."""


class EpisodeProcessError(NoctuaError):
    """Raised when processing a downloaded episode fails."""


class LLMAPIError(NoctuaError):
    """Raised when an LLM API call fails."""


# Backward-compat alias
ClaudeAPIError = LLMAPIError


class AudioTranscriptionError(NoctuaError):
    """Raised when audio transcription or analysis fails."""


class FeedBuildError(NoctuaError):
    """Raised when building the RSS feed fails."""
