from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class RetryableError(Exception):
    """Transient upstream failure (rate limit, 5xx, timeout)."""


class RateLimitedError(RetryableError):
    pass


class UpstreamError(Exception):
    """Non-retryable upstream failure."""


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (RetryableError, httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def with_retry(attempts: int = 4, initial: float = 0.5, maximum: float = 8.0) -> Callable[[F], F]:
    return retry(  # type: ignore[return-value]
        reraise=True,
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=initial, max=maximum),
        retry=retry_if_exception(is_retryable),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )


def raise_for_status(resp: httpx.Response) -> None:
    code = resp.status_code
    if code == 429:
        raise RateLimitedError(f"{resp.request.url.host} rate limited (429)")
    if code >= 500:
        raise RetryableError(f"{resp.request.url.host} returned {code}")
    if code >= 400:
        raise UpstreamError(f"{resp.request.url.host} returned {code}: {resp.text[:200]}")
