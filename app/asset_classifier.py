from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas import AssetClass

CRYPTO_EXCHANGES = frozenset(
    {"BINANCE", "BYBIT", "COINBASE", "KRAKEN", "OKX", "KUCOIN", "BITGET", "MEXC", "GATEIO",
     "HUOBI", "HTX", "BITFINEX", "BITSTAMP", "CRYPTO", "BITMEX", "DERIBIT", "PHEMEX"}
)
STOCK_EXCHANGES = frozenset(
    {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "OTC", "NSE", "BSE", "LSE", "TSX", "TSXV",
     "ASX", "HKEX", "XETR", "FWB", "EURONEXT", "TSE", "SSE", "SZSE", "SIX", "BME", "MIL"}
)
QUOTE_ASSETS = ("USDT", "USDC", "BUSD", "FDUSD", "TUSD", "USD", "BTC", "ETH", "EUR", "BNB")
KNOWN_CRYPTO_BASES = frozenset(
    {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "DOT", "MATIC", "POL", "LINK",
     "UNI", "AAVE", "ATOM", "LTC", "BCH", "TRX", "TON", "NEAR", "APT", "SUI", "ARB", "OP",
     "PEPE", "SHIB", "WIF", "INJ", "TIA", "SEI", "FIL", "ETC", "XLM", "HBAR", "ICP", "RNDR",
     "RENDER", "FET", "TAO", "JUP", "PYTH", "ONDO", "ENA", "WLD", "STX", "IMX", "MKR", "LDO",
     "CRV", "GRT", "SAND", "MANA", "AXS", "APE", "GALA", "EGLD", "ALGO", "VET", "FTM", "S",
     "RUNE", "KAS", "BONK", "FLOKI", "ORDI", "HYPE", "TRUMP", "VIRTUAL", "AI16Z", "PENGU"}
)
_PERP_SUFFIX = re.compile(r"(\.P|PERP|-PERP|_PERP)$")


@dataclass(frozen=True)
class AssetInfo:
    raw: str
    symbol: str
    base: str
    quote: str | None
    asset_class: AssetClass
    exchange: str | None
    # True when nothing in the symbol actually said "stock" — no exchange, no quote asset, not a known crypto base —
    # and STOCK was simply the fallback. KNOWN_CRYPTO_BASES goes stale with every new listing, so a bare unknown
    # ticker is a guess, not a classification. Callers that can afford an async lookup may resolve it; the rest
    # keep the guess, which is what this module has always returned.
    ambiguous: bool = False

    @property
    def is_crypto(self) -> bool:
        return self.asset_class is AssetClass.CRYPTO

    @property
    def pair_symbol(self) -> str:
        """BASE+QUOTE as centralised venues write it; USD-ish quotes normalise to USDT. Binance and Bybit agree."""
        quote = self.quote or "USDT"
        if quote in {"USD", "USDC", "BUSD", "FDUSD", "TUSD"}:
            quote = "USDT"
        return f"{self.base}{quote}"

    @property
    def binance_symbol(self) -> str:
        return self.pair_symbol

    @property
    def yahoo_symbol(self) -> str:
        if self.is_crypto:
            return f"{self.base}-USD"
        return self.base.replace(".", "-")

    def tradingview_symbols(self, stock_exchanges: tuple[str, ...], crypto_exchange: str) -> list[str]:
        if self.exchange:
            return [f"{self.exchange}:{self.symbol}"]
        if self.is_crypto:
            return [f"{crypto_exchange}:{self.binance_symbol}"]
        return [f"{ex}:{self.symbol}" for ex in stock_exchanges]


def _split_quote(sym: str) -> tuple[str, str | None]:
    for quote in QUOTE_ASSETS:
        if sym.endswith(quote) and len(sym) > len(quote) + 1:
            return sym[: -len(quote)], quote
    return sym, None


def classify(raw: str) -> AssetInfo:
    text = raw.strip().upper()
    if not text:
        raise ValueError("empty symbol")

    forced: AssetClass | None = None
    if text.startswith("CRYPTO:"):
        forced, text = AssetClass.CRYPTO, text[7:]
    elif text.startswith("STOCK:"):
        forced, text = AssetClass.STOCK, text[6:]

    exchange: str | None = None
    if ":" in text:
        exchange, text = text.split(":", 1)
        exchange = exchange.strip() or None

    text = _PERP_SUFFIX.sub("", text.replace("/", "").replace("-", "").replace("_", ""))
    if not re.fullmatch(r"[A-Z0-9.]{1,20}", text):
        raise ValueError(f"invalid symbol: {raw!r}")

    if forced is AssetClass.STOCK or (exchange in STOCK_EXCHANGES):
        return AssetInfo(raw, text, text, None, AssetClass.STOCK, exchange)

    base, quote = _split_quote(text)
    looks_crypto = (
        forced is AssetClass.CRYPTO
        or exchange in CRYPTO_EXCHANGES
        or (quote is not None and quote != "USD")
        or (quote == "USD" and base in KNOWN_CRYPTO_BASES)
        or (quote is None and text in KNOWN_CRYPTO_BASES)
    )
    if looks_crypto:
        if quote is None:
            base, quote = text, "USDT"
        return AssetInfo(raw, f"{base}{quote}", base, quote, AssetClass.CRYPTO, exchange)

    # Nothing positively identified this as an equity either. With no exchange prefix to go on, STOCK here is the
    # fallback rather than a finding — true both for a bare ticker and for a USD pair whose base we don't know.
    return AssetInfo(raw, text, text, None, AssetClass.STOCK, exchange, exchange is None)
