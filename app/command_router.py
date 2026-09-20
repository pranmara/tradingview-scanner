from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from app.asset_classifier import KNOWN_CRYPTO_BASES, classify
from app.clients.typesafe import build_client, describe_error, log_call
from app.schemas import Timeframe
from app.timeframes import parse_timeframe

logger = logging.getLogger(__name__)

# `/scan BTCUSDT 4h` is parsed in code and never reaches this module. This is the fallback for everything a
# strict parser has to reject — "is btc worth a long on the 4h" — plus plain messages with no command at all.

INTENT_CRITERIA: dict[str, Any] = {
    "scan": {
        "what": (
            "Analyse one named market and report whether there is a trade in it: a confluence score, a direction, "
            "an entry, a stop and targets."
        ),
        "includes": "Any request to look at, check, rate, or give an opinion on a specific instrument or ticker.",
        "examples": ["is btc worth a long on the 4h", "what do you make of NVDA daily", "eth setup?"],
    },
    "status": {
        "what": "Report whether the scanner's own data sources, cache and execution wiring are healthy.",
        "not_for": "Questions about a market. This is about the tool itself.",
        "examples": ["are the feeds up", "system status", "is nansen connected"],
    },
    "indicators": {
        "what": "List, activate, inspect or remove the TradingView scripts the scanner pulls on every scan.",
        "examples": ["what indicators are active", "show my scripts", "remove MyOsc"],
    },
    "help": {
        "what": "Explain what this bot can do or how to phrase a command.",
        "examples": ["what can you do", "how do I use this", "commands"],
    },
    "other": {
        "what": "Anything else: small talk, an unrelated question, or something this trading scanner does not do.",
        "examples": ["place the order for me", "thanks", "what's the weather"],
    },
}

TIMEFRAME_CRITERIA: dict[str, str] = {
    "5m": "Five-minute candles.",
    "15m": "Fifteen-minute candles.",
    "30m": "Thirty-minute candles.",
    "1h": "One-hour candles, also spoken as hourly or 60 minute.",
    "4h": "Four-hour candles, also spoken as the 4 hour or 240 minute.",
    "1d": "Daily candles, also spoken as the daily or 1D.",
    "1w": "Weekly candles, also spoken as the weekly or 1W.",
    "not_stated": "The message does not say which candle timeframe to use.",
}

_NO_INSTRUMENT = "none_named"
_NOT_CRYPTO = "not_a_listed_crypto"

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,19}")
_STOPWORDS = frozenset(
    """a an the is are am be was were do does did can could should would will shall may might must
    i me my we us you your it its this that these those there here what which who whom whose why how when where
    and or but not no yes if then than so as at by for from in into of off on out over to up down with without about
    please now today tonight good bad long short buy sell trade trading setup chart charts look looking check
    scan scanning analyse analyze analysis opinion think thoughts worth take give show tell me update
    price action entry stop target score signal right worthwhile make made get got find pull run
    going doing happening any some one just still next candle candles timeframe tf""".split()
)


class _SystemOneClient(Protocol):
    async def system_one(self, state: Any, questions: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class Route:
    """Where a free-text message should go. `symbol` and `timeframe` are only meaningful for intent 'scan'."""

    intent: str
    confidence: float = 0.0
    symbol: str | None = None
    timeframe: Timeframe | None = None
    note: str = ""

    @property
    def scannable(self) -> bool:
        return self.intent == "scan" and self.symbol is not None


def candidate_tokens(text: str, limit: int = 20) -> list[str]:
    """Words the instrument could be. The model selects among these; it never invents a ticker."""
    out: list[str] = []
    for match in _TOKEN.finditer(text):
        tok = match.group(0).strip("._-/")
        if not tok or tok.lower() in _STOPWORDS or parse_timeframe(tok) is not None:
            continue
        if tok.isdigit():
            continue
        upper = tok.upper()
        if upper not in out:
            out.append(upper)
        if len(out) >= limit:
            break
    return out


class CommandRouter:
    """Maps a free-text message to one of the bot's commands and its typed arguments."""

    def __init__(self, client: _SystemOneClient | None, min_confidence: float = 0.55, model: str | None = None) -> None:
        self._client = client
        self._min_confidence = min_confidence
        self._model = model

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def route(self, text: str, default_timeframe: Timeframe = Timeframe.H4) -> Route:
        if self._client is None:
            return Route(intent="unavailable", note="Natural language is off (no TYPESAFE_API_KEY).")
        message = text.strip()
        if not message:
            return Route(intent="other")

        tokens = candidate_tokens(message)
        try:
            from typesafe_sdk import Choice, Noul
        except ImportError:  # pragma: no cover - guarded at construction
            return Route(intent="unavailable", note="typesafe-sdk is not installed.")

        # All four are asked together and evaluated in parallel; the argument questions are speculative and the
        # answers are simply ignored when the intent turns out not to be a scan.
        questions: dict[str, Any] = {
            "intent": Choice(
                instructions=(
                    "This message was sent to a Telegram bot that analyses trading charts on request. What is the "
                    "user asking it to do? Judge `message`."
                ),
                criteria=dict(INTENT_CRITERIA),
            ),
            "timeframe": Choice(
                instructions=(
                    "If the user is asking for a chart to be analysed, which candle timeframe do they want? Judge "
                    "`message`, and answer not_stated when they never say."
                ),
                criteria=dict(TIMEFRAME_CRITERIA),
            ),
            "crypto_asset": Choice(
                instructions=(
                    "If the user is asking about a cryptocurrency, which one is it? The options are ticker symbols; "
                    "the user may instead use the asset's spoken name, for example Bitcoin for BTC or Solana for "
                    "SOL. Judge `message`, and answer not_a_listed_crypto for a stock, an index, or anything not "
                    "in the list."
                ),
                criteria={base: f"The cryptocurrency whose ticker is {base}." for base in sorted(KNOWN_CRYPTO_BASES)}
                | {_NOT_CRYPTO: "The message is not about any of these cryptocurrencies."},
            ),
            "names_company_not_ticker": Noul(
                instructions=(
                    "The user refers to a company or fund by its name rather than by its exchange ticker symbol - "
                    "'apple' or 'tesla' rather than AAPL or TSLA. Judge `message`."
                )
            ),
        }
        if tokens:
            questions["instrument"] = Choice(
                instructions=(
                    "Which word in the message names the market the user wants analysed? The options are the words "
                    "that actually appear in `message`. Pick the one that is an instrument or ticker, not a verb, "
                    "a timeframe or a direction. Answer none_named when no word names a market."
                ),
                criteria={tok: f"The word {tok!r} as it appears in the message." for tok in tokens}
                | {_NO_INSTRUMENT: "No word in the message names a market to analyse."},
            )

        started = time.monotonic()
        try:
            kwargs: dict[str, Any] = {"model": self._model} if self._model else {}
            response = await self._client.system_one(state={"message": message}, questions=questions, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a routing failure must not swallow the user's message
            detail = describe_error(exc)
            log_call("natural_language", started, "error", error=detail)
            logger.warning("typesafe routing failed", extra={"error": str(exc)})
            return Route(intent="unavailable", note=detail)

        route = self._interpret(response, tokens, default_timeframe)
        log_call("natural_language", started, route.intent,
                 confidence=round(route.confidence, 3), resolved_symbol=route.symbol)
        return route

    def _interpret(self, response: Any, tokens: list[str], default_timeframe: Timeframe) -> Route:
        try:
            choices, nouls = response.choices, response.nouls
            intent_ans = choices["intent"]
        except (AttributeError, KeyError, TypeError) as exc:
            logger.warning("typesafe routing response not understood", extra={"error": str(exc)})
            return Route(intent="unavailable", note="TypeSafe returned an unexpected response.")

        intent = str(intent_ans.choice)
        confidence = float(intent_ans.confidence)
        if intent not in INTENT_CRITERIA:
            return Route(intent="other", confidence=confidence)
        if confidence < self._min_confidence:
            return Route(intent="unsure", confidence=confidence,
                         note=f"Best guess was {intent} at {confidence:.0%}.")
        if intent != "scan":
            return Route(intent=intent, confidence=confidence)

        timeframe = self._timeframe(choices.get("timeframe"), default_timeframe)
        symbol, note = self._symbol(choices, nouls, tokens)
        return Route(intent="scan", confidence=confidence, symbol=symbol, timeframe=timeframe, note=note)

    def _timeframe(self, answer: Any, default: Timeframe) -> Timeframe:
        if answer is None:
            return default
        label = str(answer.choice)
        if label == "not_stated" or float(answer.confidence) < self._min_confidence:
            return default
        return parse_timeframe(label) or default

    def _symbol(self, choices: Any, nouls: Any, tokens: list[str]) -> tuple[str | None, str]:
        """Crypto resolves by name through a closed list; everything else must be a ticker the user actually typed."""
        crypto = choices.get("crypto_asset")
        if crypto is not None and str(crypto.choice) != _NOT_CRYPTO and str(crypto.choice) in KNOWN_CRYPTO_BASES:
            if float(crypto.confidence) >= self._min_confidence:
                return str(crypto.choice), ""

        instrument = choices.get("instrument")
        if instrument is not None:
            picked = str(instrument.choice)
            if picked != _NO_INSTRUMENT and picked in tokens and float(instrument.confidence) >= self._min_confidence:
                try:
                    classify(picked)
                except ValueError:
                    return None, f"{picked} is not a symbol I can scan."
                return picked, ""

        by_name = nouls.get("names_company_not_ticker")
        if by_name is not None and float(by_name.noul) >= 0.6:
            return None, "I resolve stocks by ticker, not by company name — try the symbol, e.g. AAPL."
        return None, "I could not tell which instrument you meant."


def build_router(settings: Any, client: Any | None = None) -> CommandRouter | None:
    """None when natural language is off; a disabled router is never constructed."""
    if not (settings.typesafe_active and settings.typesafe_natural_language):
        return None
    client = client if client is not None else build_client(settings)
    if client is None:
        return None
    return CommandRouter(client, min_confidence=settings.typesafe_min_confidence, model=settings.typesafe_model)


__all__ = ["CommandRouter", "Route", "build_router", "candidate_tokens", "INTENT_CRITERIA", "TIMEFRAME_CRITERIA"]
