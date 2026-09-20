from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_ERROR_LIMIT = 200


def describe_error(exc: BaseException, limit: int = _ERROR_LIMIT) -> str:
    """Class plus the API's own message. The class alone says 'something broke'; the message says what."""
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {text[:limit]}"


def log_call(feature: str, started: float, outcome: str, **fields: Any) -> None:
    """One line per request actually sent, whatever the outcome — cache hits deliberately do not log."""
    logger.info(
        "typesafe call",
        extra={"feature": feature, "outcome": outcome,
               "ms": round((time.monotonic() - started) * 1000), **fields},
    )


def build_client(settings: Any) -> Any | None:
    """One TypeSafe client for every feature that needs one; None when no key is configured."""
    if settings.typesafe_api_key is None:
        return None
    try:
        from typesafe_sdk import AsyncTypeSafeClient
    except ImportError:
        logger.warning("TYPESAFE_API_KEY is set but typesafe-sdk is not installed; TypeSafe features disabled")
        return None
    return AsyncTypeSafeClient(
        api_key=settings.typesafe_api_key.get_secret_value(),
        model=settings.typesafe_model,
        timeout=settings.typesafe_timeout_seconds,
    )


async def aclose(client: Any | None) -> None:
    if client is None:
        return
    try:
        await client.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.warning("typesafe client close failed", extra={"error": str(exc)})


__all__ = ["aclose", "build_client"]
