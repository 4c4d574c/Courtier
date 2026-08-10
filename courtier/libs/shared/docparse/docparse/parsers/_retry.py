"""Shared retry helper for external API calls (LLM, OCR).

Provides exponential-backoff retry with a retryability policy shared by
the LLM client and the OCR engine, so transient service failures get a
second chance while permanent ones fail fast.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests
from openai import APIStatusError

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0  # seconds, doubled each retry


def _is_retryable(exc: Exception) -> bool:
    """Decide whether a failed API call is worth retrying.

    FileNotFoundError (missing local input file) is permanent.  4xx
    client errors (openai.APIStatusError with 400 <= status < 500) are
    permanent — retrying the identical request will fail again — except
    429 (rate limit), which is transient.  requests.RequestException
    (network-layer failures of the OCR service), timeouts, and 5xx
    server errors are all retryable.
    """
    if isinstance(exc, FileNotFoundError):
        return False
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        if 400 <= status < 500 and status != 429:
            return False
        return True
    if isinstance(exc, requests.RequestException):
        return True
    return True


def _call_with_retry(
    call_fn: Callable[[], Any],
    description: str,
    *,
    max_retries: int = _MAX_RETRIES,
    base_delay: float = _BASE_DELAY,
) -> Any:
    """Call an API with exponential backoff retry on transient failures.

    Args:
        call_fn: Callable that performs the single API request.
        description: Human-readable label for log messages.
        max_retries: Maximum number of retries (default 3, for 4 total attempts).
        base_delay: Initial backoff delay in seconds, doubled each retry.

    Returns:
        The raw API response object on success.

    Raises:
        Exception: The original exception, unchanged, when it is not
            retryable (e.g. a 4xx client error other than 429).
        RuntimeError: When all attempts (1 + max_retries) are exhausted.
    """
    last_exc: Exception | None = None
    total_attempts = max_retries + 1
    for attempt in range(total_attempts):
        try:
            return call_fn()
        except Exception as exc:
            if not _is_retryable(exc):
                logger.error("%s failed with non-retryable error: %s", description, exc)
                raise
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                logger.warning(
                    "%s failed (attempt %d/%d), retrying in %.1fs: %s",
                    description,
                    attempt + 1,
                    total_attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "%s failed after %d attempts: %s",
                    description,
                    total_attempts,
                    exc,
                )
    raise RuntimeError(
        f"{description} failed after {total_attempts} attempts: {last_exc}"
    ) from last_exc
