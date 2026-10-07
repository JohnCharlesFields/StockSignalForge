from __future__ import annotations

import market_data_service as service


def test_undated_fmp_target_remains_raw_without_projected_upside(monkeypatch) -> None:
    def fmp(endpoint: str, symbol: str, **params):
        if endpoint == "grades-historical":
            return [{"date": "2026-09-25", "analystRatingsStrongBuy": 2,
                     "analystRatingsBuy": 8, "analystRatingsHold": 3,
                     "analystRatingsSell": 1, "analystRatingsStrongSell": 0}]
        if endpoint == "price-target-summary":
            return [{"lastQuarterAvgPriceTarget": 200.0, "lastQuarterCount": 12}]
        raise AssertionError(f"unexpected FMP endpoint: {endpoint}")

    monkeypatch.setattr(service, "_fmp_get", fmp)
    monkeypatch.setattr(service, "get_company_fundamentals", lambda symbol: (_ for _ in ()).throw(
        AssertionError("undated target must not fetch a price for upside calculation")))

    view = service.get_analyst_view("TEST")

    assert view["target_avg_quarter"] == 200.0
    assert view["target_count_quarter"] == 12
    assert view["rating_stats"]["buy"] == 10
    assert "target_upside" not in view
    assert "target_price_reference" not in view
    assert "target_signal" not in view


def test_target_only_response_does_not_invent_rating_or_upside(monkeypatch) -> None:
    monkeypatch.setattr(service, "_fmp_get", lambda endpoint, symbol, **params: (
        [{"lastQuarterAvgPriceTarget": 125.0, "lastQuarterCount": 2}]
        if endpoint == "price-target-summary" else []))

    view = service.get_analyst_view("TEST")

    assert view["available"] is True
    assert view["target_avg_quarter"] == 125.0
    assert view["rating_stats"]["total"] == 0
    assert "target_upside" not in view
