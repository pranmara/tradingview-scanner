from __future__ import annotations

from app.schemas import Timeframe

BINANCE_INTERVAL: dict[Timeframe, str] = {
    Timeframe.M5: "5m",
    Timeframe.M15: "15m",
    Timeframe.M30: "30m",
    Timeframe.H1: "1h",
    Timeframe.H4: "4h",
    Timeframe.D1: "1d",
    Timeframe.W1: "1w",
}

# (yahoo interval, yahoo range, resample factor)
YAHOO_FETCH: dict[Timeframe, tuple[str, str, int]] = {
    Timeframe.M5: ("5m", "60d", 1),
    Timeframe.M15: ("15m", "60d", 1),
    Timeframe.M30: ("30m", "60d", 1),
    Timeframe.H1: ("1h", "3mo", 1),
    Timeframe.H4: ("1h", "1y", 4),
    Timeframe.D1: ("1d", "2y", 1),
    Timeframe.W1: ("1wk", "10y", 1),
}

TV_SCANNER_SUFFIX: dict[Timeframe, str] = {
    Timeframe.M5: "|5",
    Timeframe.M15: "|15",
    Timeframe.M30: "|30",
    Timeframe.H1: "|60",
    Timeframe.H4: "|240",
    Timeframe.D1: "",
    Timeframe.W1: "|1W",
}

TV_CHART_INTERVAL: dict[Timeframe, str] = {
    Timeframe.M5: "5",
    Timeframe.M15: "15",
    Timeframe.M30: "30",
    Timeframe.H1: "60",
    Timeframe.H4: "240",
    Timeframe.D1: "1D",
    Timeframe.W1: "1W",
}

TWELVEDATA_INTERVAL: dict[Timeframe, str] = {
    Timeframe.M5: "5min",
    Timeframe.M15: "15min",
    Timeframe.M30: "30min",
    Timeframe.H1: "1h",
    Timeframe.H4: "4h",
    Timeframe.D1: "1day",
    Timeframe.W1: "1week",
}

_MIN = 60_000
BAR_MS: dict[Timeframe, int] = {
    Timeframe.M5: 5 * _MIN,
    Timeframe.M15: 15 * _MIN,
    Timeframe.M30: 30 * _MIN,
    Timeframe.H1: 60 * _MIN,
    Timeframe.H4: 240 * _MIN,
    Timeframe.D1: 1_440 * _MIN,
    Timeframe.W1: 10_080 * _MIN,
}

_ALIASES: dict[str, Timeframe] = {
    "5": Timeframe.M5, "5m": Timeframe.M5, "m5": Timeframe.M5,
    "15": Timeframe.M15, "15m": Timeframe.M15, "m15": Timeframe.M15,
    "30": Timeframe.M30, "30m": Timeframe.M30, "m30": Timeframe.M30,
    "60": Timeframe.H1, "1h": Timeframe.H1, "h1": Timeframe.H1,
    "240": Timeframe.H4, "4h": Timeframe.H4, "h4": Timeframe.H4,
    "d": Timeframe.D1, "1d": Timeframe.D1, "d1": Timeframe.D1, "daily": Timeframe.D1,
    "w": Timeframe.W1, "1w": Timeframe.W1, "w1": Timeframe.W1, "weekly": Timeframe.W1,
}

SUPPORTED_LABEL = ", ".join(tf.value for tf in Timeframe)


def parse_timeframe(raw: str) -> Timeframe | None:
    return _ALIASES.get(raw.strip().lower())


def normalize_timeframe_label(raw: str) -> str:
    tf = parse_timeframe(raw)
    return tf.value if tf else raw.strip().lower()
