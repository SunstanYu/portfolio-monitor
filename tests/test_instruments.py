import pytest

from portfolio_monitor.domain.instruments import (
    UnresolvedInstrument,
    default_currency,
    is_canonical,
    normalize,
    to_yahoo,
    unknown_id,
)


@pytest.mark.parametrize(
    ("raw", "market", "expected"),
    [
        # 同一只港股的各种写法
        ("0700.HK", None, "HK:00700"),
        ("700", "HK", "HK:00700"),
        ("00700", None, "HK:00700"),
        ("hk:700", None, "HK:00700"),
        ("9988.HK", None, "HK:09988"),
        # A 股
        ("600519.SS", None, "CN:600519"),
        ("600519.SH", None, "CN:600519"),
        ("000001.SZ", None, "CN:000001"),
        ("600519", None, "CN:600519"),
        # 美股
        ("NVDA", None, "US:NVDA"),
        (" nvda ", None, "US:NVDA"),
        ("BRK-B", None, "US:BRK.B"),
        ("BRK/B", None, "US:BRK.B"),
        ("BRK B", "US", "US:BRK.B"),
        # 加密货币必须带提示
        ("btc", "CRYPTO", "CRYPTO:BTC"),
        ("USDT", "crypto", "CRYPTO:USDT"),
        # 新加坡交易所
        ("D05.SI", None, "SG:D05"),
        ("c6l", "SG", "SG:C6L"),
    ],
)
def test_normalize(raw, market, expected):
    assert normalize(raw, market) == expected
    assert is_canonical(expected)


@pytest.mark.parametrize(
    ("raw", "market"),
    [("", None), ("123456", "HK"), ("12345", "CN"), ("TOOLONGTICKER", None), ("XX:1", None),
     ("NVDA", "JP"), ("$$$", None)],
)
def test_normalize_rejects(raw, market):
    with pytest.raises(UnresolvedInstrument):
        normalize(raw, market)


def test_unknown_id_is_canonical_placeholder():
    uid = unknown_id("ibkr", "weird sym/1")
    assert uid == "UNKNOWN:ibkr:weird_sym_1"
    assert is_canonical(uid)
    assert not is_canonical("0700.HK")


@pytest.mark.parametrize(
    ("instrument_id", "yahoo"),
    [("US:BRK.B", "BRK-B"), ("HK:00700", "0700.HK"), ("HK:09988", "9988.HK"),
     ("CN:600519", "600519.SS"), ("CN:000001", "000001.SZ"), ("CN:300750", "300750.SZ"),
     ("SG:D05", "D05.SI"), ("CRYPTO:BTC", "BTC-USD"), ("UNKNOWN:x:y", None)],
)
def test_to_yahoo(instrument_id, yahoo):
    assert to_yahoo(instrument_id) == yahoo


def test_default_currency():
    assert default_currency("HK:00700") == "HKD"
    assert default_currency("CN:600519") == "CNY"
    assert default_currency("SG:D05") == "SGD"
    assert default_currency("UNKNOWN:x:y") is None
