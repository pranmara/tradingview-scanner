from app.orchestrator import scan_timeframes
from app.schemas import Timeframe
from app.timeframes import (
    BAR_MS,
    BINANCE_INTERVAL,
    TV_CHART_INTERVAL,
    TV_SCANNER_SUFFIX,
    TWELVEDATA_INTERVAL,
    YAHOO_FETCH,
    parse_timeframe,
)


def test_every_timeframe_mapped_everywhere() -> None:
    for tf in Timeframe:
        for table in (BINANCE_INTERVAL, YAHOO_FETCH, TV_SCANNER_SUFFIX, TV_CHART_INTERVAL, TWELVEDATA_INTERVAL, BAR_MS):
            assert tf in table, f"{tf} missing from {table}"


def test_parse_aliases() -> None:
    assert parse_timeframe("1W") is Timeframe.W1
    assert parse_timeframe("W") is Timeframe.W1
    assert parse_timeframe("5") is Timeframe.M5
    assert parse_timeframe("30m") is Timeframe.M30
    assert parse_timeframe("D") is Timeframe.D1
    assert parse_timeframe("2h") is None


def test_scan_set_adds_weekly_for_daily_scans() -> None:
    assert scan_timeframes(Timeframe.H4) == [Timeframe.H4, Timeframe.H1, Timeframe.D1]
    assert scan_timeframes(Timeframe.D1) == [Timeframe.D1, Timeframe.H1, Timeframe.H4, Timeframe.W1]
    assert scan_timeframes(Timeframe.W1)[0] is Timeframe.W1 and Timeframe.W1 in scan_timeframes(Timeframe.W1)
    assert scan_timeframes(Timeframe.M5) == [Timeframe.M5, Timeframe.H1, Timeframe.H4, Timeframe.D1]
