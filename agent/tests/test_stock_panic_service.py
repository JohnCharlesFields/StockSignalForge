from stock_panic_service import stock_panic_evidence, stock_panic_proxy


def _ohlcv(closes: list[float], volumes: list[float] | None = None) -> list[dict]:
    volumes = volumes or [1_000_000.0] * len(closes)
    rows = []
    for i, close in enumerate(closes):
        rows.append({
            "date": f"2026-01-{(i % 28) + 1:02d}",
            "open": close * 0.99,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": volumes[i],
        })
    return rows


def test_stock_panic_proxy_varies_by_symbol_profile(monkeypatch) -> None:
    import stock_panic_service as service

    market = [100 + i * 0.2 for i in range(40)]
    monkeypatch.setattr(service, "_market_returns", lambda: service._frame_from_closes(market)["Close"].pct_change().dropna())
    calm = stock_panic_proxy(
        "CALM",
        {"recent_ohlcv": _ohlcv([100 + i * 0.1 for i in range(40)])},
        macro_regime={"available": True, "value": 28.0, "mode": "defensive"},
    )
    volatile = stock_panic_proxy(
        "FAST",
        {"recent_ohlcv": _ohlcv([100, 98, 103, 95, 108, 90, 110, 88, 112, 86] * 4)},
        macro_regime={"available": True, "value": 28.0, "mode": "defensive"},
    )

    assert calm["available"] is True
    assert volatile["available"] is True
    assert volatile["stock_vix_equivalent"] > calm["stock_vix_equivalent"]
    assert volatile["stock_panic_score"] > calm["stock_panic_score"]


def test_stock_panic_evidence_supports_panic_reclaim_without_event_risk() -> None:
    evidence = stock_panic_evidence(
        {"available": True, "stock_panic_score": 68, "gex_penalty": 0, "event_penalty": 0},
        {"mode": "panic_reclaim"},
    )

    assert evidence["id"] == "stock_vix_proxy"
    assert evidence["label"] == "个股恐慌承接"
    assert evidence["probability"] > 0.5


def test_stock_panic_evidence_penalizes_crisis_or_event_risk() -> None:
    evidence = stock_panic_evidence(
        {"available": True, "stock_panic_score": 70, "gex_penalty": 0, "event_penalty": 6},
        {"mode": "panic_reclaim"},
    )

    assert evidence["label"] == "个股恐慌风险"
    assert evidence["probability"] < 0.5

