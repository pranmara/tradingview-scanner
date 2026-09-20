from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
