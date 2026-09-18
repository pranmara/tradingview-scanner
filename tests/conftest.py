from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token="123:test",
        tv_webhook_secret="test-secret",
        tv_webhook_enforce_ip_allowlist=False,
        redis_url=None,
    )
