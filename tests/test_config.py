from app.config import Settings


def test_blank_optional_values_are_unset() -> None:
    s = Settings(  # type: ignore[call-arg]
        _env_file=None, telegram_bot_token="1:x", tv_webhook_secret="s", redis_url=None,
        tv_webhook_hmac_key="", tv_mcp_url="  ", tv_session_id="", nansen_api_key="", execution_webhook_url="",
        execution_hmac_key="", twelvedata_api_key="",
    )
    assert s.tv_webhook_hmac_key is None
    assert s.tv_mcp_url is None
    assert s.tv_session_id is None and not s.tv_session_configured
    assert s.nansen_api_key is None and not s.nansen_active
    assert s.execution_webhook_url is None and not s.execution_live
    assert s.twelvedata_api_key is None


def test_set_values_survive() -> None:
    s = Settings(_env_file=None, telegram_bot_token="1:x", tv_webhook_secret="s", redis_url=None, tv_webhook_hmac_key="k")  # type: ignore[call-arg]
    assert s.tv_webhook_hmac_key is not None and s.tv_webhook_hmac_key.get_secret_value() == "k"


def test_a_trailing_comment_on_a_blank_value_is_unset() -> None:
    """Compose's env_file parser yields the comment as the value for `KEY=   # note`; it must not become a model."""
    s = Settings(  # type: ignore[call-arg]
        _env_file=None, telegram_bot_token="1:x", tv_webhook_secret="s", redis_url=None,
        typesafe_model="# blank = the SDK/account default (Jev)",
        typesafe_api_key="   # paste your key here",
    )
    assert s.typesafe_model is None
    assert s.typesafe_api_key is None and not s.typesafe_active


def test_a_value_that_merely_contains_a_hash_survives() -> None:
    s = Settings(_env_file=None, telegram_bot_token="1:x", tv_webhook_secret="s", redis_url=None,  # type: ignore[call-arg]
                 typesafe_model="jev-1.13#beta")
    assert s.typesafe_model == "jev-1.13#beta"
