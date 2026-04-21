"""
Retry policy for fan-out tracking runs (Prompt 29).
Classifies errors as retryable vs non-retryable and calculates backoff delays.
"""
import random
from typing import Union

RETRYABLE_SIGNALS = ["rate_limit", "timeout", "connection_error", "502", "503", "504",
                     "ratelimit", "too many requests", "service unavailable", "bad gateway",
                     "connection refused", "connection reset", "timed out"]

NON_RETRYABLE_SIGNALS = ["invalid_api_key", "insufficient_quota", "400", "model_not_found",
                         "authentication", "invalid key", "no such model", "billing"]

RETRY_DELAYS_MINUTES = [30, 120, 480]  # retry 1: 30m, retry 2: 2h, retry 3: 8h

def is_retryable(error: Union[str, Exception]) -> bool:
    """Return True if the error is transient and worth retrying."""
    msg = str(error).lower()
    for sig in NON_RETRYABLE_SIGNALS:
        if sig in msg:
            return False
    for sig in RETRYABLE_SIGNALS:
        if sig in msg:
            return True
    return False  # unknown errors are NOT retried (safe default)

def next_retry_delay(retry_count: int) -> int:
    """Return minutes to wait before the next retry attempt. Includes ±5 min jitter."""
    base = RETRY_DELAYS_MINUTES[min(retry_count, len(RETRY_DELAYS_MINUTES) - 1)]
    jitter = random.randint(-5, 5)
    return max(1, base + jitter)
