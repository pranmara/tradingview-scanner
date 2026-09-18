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
