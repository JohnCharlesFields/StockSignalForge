from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest


@pytest.fixture
def market_cap_cache(monkeypatch):
    import gildata_shadow_service
    import market_calendar
    import market_data_service

    state: dict = {}
    calls: list[str] = []
    monkeypatch.setattr(market_calendar, "most_recent_session", lambda: date(2026, 9, 25))
    monkeypatch.setattr(market_data_service, "_cache_path", lambda *args: Path("in-memory.json"))
    monkeypatch.setattr(market_data_service, "_read_json", lambda path: state.get("cached"))
    monkeypatch.setattr(market_data_service, "_write_json", lambda path, value: state.update(cached=value))
    monkeypatch.setattr(gildata_shadow_service, "cached_equity", lambda symbol: state.get("shadow"))

    def massive(path):
        calls.append(path)
        return {"results": {"market_cap": 4_977_636_972_600}}

    monkeypatch.setattr(market_data_service, "_massive_get", massive)
    return state, calls, market_data_service


def test_market_cap_is_refreshed_once_per_completed_session(market_cap_cache) -> None:
    state, calls, service = market_cap_cache
    state["cached"] = {"market_cap": 4_300_000_000_000, "refreshed_for_session": "2026-09-24"}

    fresh = service.get_market_cap_snapshot("AAPL", refresh=True)
    again = service.get_market_cap_snapshot("AAPL", refresh=True)

    assert fresh["market_cap"] == 4_977_636_972_600
    assert fresh["data_as_of_date"] is None
    assert fresh["refreshed_for_session"] == "2026-09-25"
    assert fresh["cache_hit"] is False
    assert again["cache_hit"] is True
    assert calls == ["/v3/reference/tickers/AAPL"]


def test_market_cap_page_read_never_refreshes_or_claims_stale_is_fresh(market_cap_cache) -> None:
    state, calls, service = market_cap_cache
    state["cached"] = {"market_cap": 4_300_000_000_000, "refreshed_for_session": "2026-09-24"}

    with service.external_data_scope(False):
        result = service.get_market_cap_snapshot("AAPL", refresh=True)

    assert result["stale"] is True
    assert result["market_cap"] == 4_300_000_000_000
    assert calls == []


def test_market_cap_uses_only_same_session_gildata_shadow(market_cap_cache) -> None:
    state, calls, service = market_cap_cache
    state["shadow"] = {"symbol": "AAPL", "as_of": "2026-09-24", "market_cap_usd": 4_000_000_000_000}
    assert service.get_market_cap_snapshot("AAPL")["available"] is False

    state["shadow"]["as_of"] = "2026-09-25"
    result = service.get_market_cap_snapshot("AAPL")
    assert result["market_cap"] == 4_000_000_000_000
    assert result["source"] == "gildata:FinQuery:cached"
    assert result["data_as_of_date"] == "2026-09-25"
    assert calls == []


def test_market_cap_rejects_unsafe_symbol_without_io(market_cap_cache) -> None:
    _, calls, service = market_cap_cache
    assert service.get_market_cap_snapshot("../AAPL", refresh=True)["available"] is False
    assert calls == []


def test_profile_overlay_keeps_original_analyst_and_adds_source_dates(monkeypatch) -> None:
    api_server = pytest.importorskip("api_server")
    import gildata_shadow_service
    import market_calendar
    import market_data_service

    monkeypatch.setattr(market_calendar, "most_recent_session", lambda: date(2026, 9, 25))
    monkeypatch.setattr(api_server, "external_data_allowed", lambda: False)
    monkeypatch.setattr(market_data_service, "get_market_cap_snapshot", lambda symbol, refresh: {
        "available": True, "market_cap": 4_977_636_972_600,
        "source": "gildata:FinQuery:cached", "data_as_of_date": "2026-09-25", "stale": False,
    })
    monkeypatch.setattr(gildata_shadow_service, "cached_equity", lambda symbol: {
        "source": "gildata:FinQuery", "as_of": "2026-09-25",
        "ratings": {"buy": 20, "overweight": 8, "neutral": 13, "underweight": 2, "sell": 3},
        "target_avg_usd": 337.68, "target_window_days": 100,
    })
    original = {"available": True, "facts": {"market_cap": 4_300_000_000_000},
                "analyst": {"as_of": "2026-06-01", "target_avg_quarter": 310.0,
                            "target_upside": 0.35, "target_signal": {"signal": "high_upside"}}}

    result = api_server._single_profile_research_overlay("AAPL", original)

    assert original["facts"]["market_cap"] == 4_300_000_000_000
    assert result["facts"]["market_cap"] == 4_977_636_972_600
    assert result["market_cap_meta"]["data_as_of_date"] == "2026-09-25"
    assert result["analyst"]["target_avg_quarter"] == 310.0
    assert "target_upside" not in result["analyst"]
    assert "target_signal" not in result["analyst"]
    assert original["analyst"]["target_upside"] == 0.35
    assert result["gildata_research"]["ratings"]["buy"] == 20
    assert result["gildata_research"]["is_latest_session"] is True
