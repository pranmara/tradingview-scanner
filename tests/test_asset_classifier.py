import pytest

from app.asset_classifier import classify
from app.schemas import AssetClass


@pytest.mark.parametrize(
    ("raw", "asset_class", "symbol", "binance"),
    [
        ("BTCUSDT", AssetClass.CRYPTO, "BTCUSDT", "BTCUSDT"),
        ("btc/usd", AssetClass.CRYPTO, "BTCUSD", "BTCUSDT"),
        ("BINANCE:SOLUSDT.P", AssetClass.CRYPTO, "SOLUSDT", "SOLUSDT"),
        ("ETH", AssetClass.CRYPTO, "ETHUSDT", "ETHUSDT"),
        ("AAPL", AssetClass.STOCK, "AAPL", "AAPLUSDT"),
        ("NASDAQ:TSLA", AssetClass.STOCK, "TSLA", "TSLAUSDT"),
        ("stock:LINK", AssetClass.STOCK, "LINK", "LINKUSDT"),
        ("crypto:XYZ", AssetClass.CRYPTO, "XYZUSDT", "XYZUSDT"),
    ],
)
def test_classify(raw: str, asset_class: AssetClass, symbol: str, binance: str) -> None:
    info = classify(raw)
    assert info.asset_class is asset_class
    assert info.symbol == symbol
    assert info.binance_symbol == binance


def test_yahoo_symbol() -> None:
    assert classify("BTCUSDT").yahoo_symbol == "BTC-USD"
    assert classify("BRK.B").yahoo_symbol == "BRK-B"


def test_invalid() -> None:
    with pytest.raises(ValueError):
        classify("  ")
