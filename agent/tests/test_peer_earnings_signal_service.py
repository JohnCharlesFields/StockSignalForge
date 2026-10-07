from datetime import date
from pathlib import Path

import pandas as pd

from peer_earnings_signal_service import (
    PEER_MATCH_VERSION,
    _peer_mapping_for_symbols,
    _apply_calendar_override,
    build_peer_earnings_signals_for_test,
    list_peer_groups,
    load_peer_earnings_history,
    peer_catalog_summary,
    summarize_peer_earnings_history,
)


def test_valuation_peers_follow_sublane_without_changing_relay_peers() -> None:
    group = next(item for item in list_peer_groups() if item["id"] == "ai_accelerators")
    mapping = _peer_mapping_for_symbols(["NVDA", "AMD", "AVGO"], [group])["NVDA"]
    assert mapping["direct_peers"] == ["AMD", "AVGO"]
    assert mapping["valuation_peers"] == ["AMD"]
    assert mapping["peer_sublanes"] == {"AMD": "ai_accelerator"}


def _history(prices: list[float], volumes: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {"Close": prices, "Volume": volumes},
        index=pd.date_range("2026-04-20", periods=len(prices), freq="B"),
    )


def test_peer_earnings_lag_ranks_unreported_peer_after_leader_beat() -> None:
    prices_leader = [100.0] * 30 + [106.0, 110.0, 112.0, 113.0, 114.0]
    prices_target = [50.0] * 30 + [50.5, 50.8, 51.0, 51.2, 51.4]
    volumes_leader = [100_000] * 30 + [260_000, 180_000, 150_000, 130_000, 120_000]
    volumes_target = [90_000] * 30 + [100_000, 105_000, 110_000, 115_000, 130_000]
    groups = [{"id": "enterprise_servers", "label": "AI servers", "similarity": 0.96, "symbols": ["DELL", "HPE"]}]
    earnings = {
        "DELL": {
            "symbol": "DELL",
            "latest_report_date": "2026-06-01",
            "next_report_date": None,
            "eps_surprise_pct": 0.18,
            "revenue_yoy": 0.22,
        },
        "HPE": {
            "symbol": "HPE",
            "latest_report_date": "2026-03-01",
            "next_report_date": "2026-06-15",
            "eps_surprise_pct": 0.01,
            "revenue_yoy": 0.05,
        },
    }
    result = build_peer_earnings_signals_for_test(
        groups,
        earnings,
        {"DELL": _history(prices_leader, volumes_leader), "HPE": _history(prices_target, volumes_target)},
        as_of=date(2026, 6, 5),
    )

    assert len(result) == 1
    signal = result[0]
    assert signal["symbol"] == "HPE"
    assert signal["leader_symbol"] == "DELL"
    assert signal["relative_lag_gap"] > 0.05
    assert signal["research_probability"] > 0.5
    assert signal["target_latest_price"] == 51.4
    assert signal["target_report_date"] == "2026-06-15"
    assert signal["leader_pre_report_price"] == 100.0
    assert signal["leader_latest_price"] == 114.0
    assert signal["expected_catch_up_return"] > 0
    assert signal["expected_target_price"] > signal["target_latest_price"]
    assert signal["sublane_relation"] == "直接同行"
    assert signal["daily_tunnel"]["sample_days"] == 16
    assert signal["daily_tunnel"]["score"] > 0


def test_peer_earnings_lag_requires_target_to_report_later() -> None:
    history = _history([100.0] * 30 + [106.0, 108.0, 109.0], [100_000] * 30 + [250_000, 150_000, 120_000])
    groups = [{"id": "enterprise_servers", "label": "AI servers", "similarity": 0.96, "symbols": ["DELL", "HPE"]}]
    earnings = {
        "DELL": {"symbol": "DELL", "latest_report_date": "2026-06-01", "eps_surprise_pct": 0.18, "revenue_yoy": 0.22},
        "HPE": {"symbol": "HPE", "latest_report_date": "2026-05-20", "next_report_date": None},
    }
    assert build_peer_earnings_signals_for_test(
        groups, earnings, {"DELL": history, "HPE": history}, as_of=date(2026, 6, 5)
    ) == []


def test_peer_earnings_lag_keeps_unknown_target_date_for_manual_check() -> None:
    prices_leader = [100.0] * 30 + [106.0, 110.0, 112.0, 114.0]
    prices_target = [50.0] * 30 + [50.2, 50.6, 51.0, 51.3]
    volumes_leader = [100_000] * 30 + [280_000, 180_000, 150_000, 120_000]
    volumes_target = [90_000] * 30 + [100_000, 105_000, 110_000, 115_000]
    groups = [{"id": "enterprise_servers", "label": "AI servers", "similarity": 0.96, "symbols": ["DELL", "HPE"]}]
    earnings = {
        "DELL": {
            "symbol": "DELL",
            "latest_report_date": "2026-06-01",
            "eps_surprise_pct": 0.18,
            "revenue_yoy": 0.22,
        },
        "HPE": {
            "symbol": "HPE",
            "latest_report_date": "2026-03-01",
            "next_report_date": None,
        },
    }
    result = build_peer_earnings_signals_for_test(
        groups,
        earnings,
        {"DELL": _history(prices_leader, volumes_leader), "HPE": _history(prices_target, volumes_target)},
        as_of=date(2026, 6, 5),
    )

    assert len(result) == 1
    assert result[0]["symbol"] == "HPE"
    assert result[0]["target_report_date"] is None
    assert result[0]["days_to_target_report"] is None
    assert result[0]["target_report_date_requires_manual_check"] is True
    assert result[0]["target_report_date_note"] == "披露日待人工确认"


def test_calendar_override_and_history_archive_are_auditable() -> None:
    corrected = _apply_calendar_override(
        {"symbol": "HPE", "latest_report_date": "2026-03-09", "next_report_date": None},
        {"next_report_date": "2026-06-02", "note": "checked IR page"},
    )
    assert corrected["next_report_date"] == "2026-06-02"
    assert corrected["calendar_override"] is True
    assert corrected["calendar_override_note"] == "checked IR page"

    history_path = Path(__file__).with_name("peer_earnings_history_fixture.jsonl")
    assert load_peer_earnings_history(history_path, limit=1) == [{"scan_time": "b"}]


def test_peer_earnings_history_summary_tracks_realized_followups() -> None:
    path = Path(__file__).with_name("peer_earnings_history_summary_fixture.jsonl")
    summary = summarize_peer_earnings_history(path, limit=10)

    assert summary["resolved_count"] == 1
    assert summary["win_rate"] == 1.0
    assert summary["examples"][0]["target_hit"] is True
    assert summary["examples"][0]["realized_return"] == 0.12


def test_weak_apparel_audience_match_is_rejected() -> None:
    prices_leader = [35.0] * 30 + [39.0, 41.0, 42.0, 43.0]
    prices_target = [70.0] * 30 + [70.2, 70.5, 70.8, 71.0]
    volumes_leader = [100_000] * 30 + [280_000, 180_000, 150_000, 120_000]
    volumes_target = [90_000] * 30 + [100_000, 105_000, 110_000, 115_000]
    groups = [{"id": "apparel_sportswear", "label": "sportswear", "similarity": 0.88, "symbols": ["ONON", "NKE"]}]
    earnings = {
        "ONON": {
            "symbol": "ONON",
            "latest_report_date": "2026-06-01",
            "eps_surprise_pct": 0.18,
            "revenue_yoy": 0.22,
            "market_cap": 18_000_000_000,
        },
        "NKE": {
            "symbol": "NKE",
            "latest_report_date": "2026-03-01",
            "next_report_date": "2026-06-20",
            "market_cap": 105_000_000_000,
        },
    }
    assert build_peer_earnings_signals_for_test(
        groups,
        earnings,
        {"ONON": _history(prices_leader, volumes_leader), "NKE": _history(prices_target, volumes_target)},
        as_of=date(2026, 6, 5),
    ) == []


def test_large_cap_leader_can_transmit_to_smaller_same_sublane_peer() -> None:
    prices_leader = [20.0] * 30 + [22.0, 23.0, 24.0, 25.0]
    prices_target = [10.0] * 30 + [10.1, 10.2, 10.3, 10.4]
    volumes_leader = [100_000] * 30 + [280_000, 180_000, 150_000, 120_000]
    volumes_target = [90_000] * 30 + [100_000, 105_000, 110_000, 115_000]
    groups = [{"id": "crypto_miners", "label": "miners", "similarity": 0.92, "symbols": ["MARA", "CLSK"]}]
    earnings = {
        "MARA": {"symbol": "MARA", "latest_report_date": "2026-06-01", "eps_surprise_pct": 0.18, "revenue_yoy": 0.22, "market_cap": 30_000_000_000},
        "CLSK": {"symbol": "CLSK", "latest_report_date": "2026-03-01", "next_report_date": "2026-06-20", "market_cap": 3_000_000_000},
    }
    result = build_peer_earnings_signals_for_test(
        groups,
        earnings,
        {"MARA": _history(prices_leader, volumes_leader), "CLSK": _history(prices_target, volumes_target)},
        as_of=date(2026, 6, 5),
    )
    assert len(result) == 1
    assert result[0]["symbol"] == "CLSK"
    assert result[0]["leader_to_target_market_cap_ratio"] == 10.0
    assert result[0]["transmission_direction"] == "龙头向较小同行传导"


def test_small_cap_leader_does_not_reverse_read_much_larger_same_sublane_peer() -> None:
    prices_leader = [10.0] * 30 + [11.0, 12.0, 12.5, 13.0]
    prices_target = [20.0] * 30 + [20.1, 20.2, 20.3, 20.4]
    volumes_leader = [100_000] * 30 + [280_000, 180_000, 150_000, 120_000]
    volumes_target = [90_000] * 30 + [100_000, 105_000, 110_000, 115_000]
    groups = [{"id": "crypto_miners", "label": "miners", "similarity": 0.92, "symbols": ["CLSK", "MARA"]}]
    earnings = {
        "CLSK": {"symbol": "CLSK", "latest_report_date": "2026-06-01", "eps_surprise_pct": 0.18, "revenue_yoy": 0.22, "market_cap": 3_000_000_000},
        "MARA": {"symbol": "MARA", "latest_report_date": "2026-03-01", "next_report_date": "2026-06-20", "market_cap": 30_000_000_000},
    }
    assert build_peer_earnings_signals_for_test(
        groups,
        earnings,
        {"CLSK": _history(prices_leader, volumes_leader), "MARA": _history(prices_target, volumes_target)},
        as_of=date(2026, 6, 5),
    ) == []


def test_explicit_adjacent_sublane_can_transmit_with_lower_confidence() -> None:
    prices_leader = [100.0] * 30 + [108.0, 112.0, 114.0, 116.0]
    prices_target = [50.0] * 30 + [50.2, 50.5, 50.8, 51.0]
    volumes_leader = [100_000] * 30 + [280_000, 180_000, 150_000, 120_000]
    volumes_target = [90_000] * 30 + [100_000, 105_000, 110_000, 115_000]
    groups = [{"id": "semi_equipment", "label": "semi equipment", "similarity": 0.91, "symbols": ["AMAT", "KLAC"]}]
    earnings = {
        "AMAT": {"symbol": "AMAT", "latest_report_date": "2026-06-01", "eps_surprise_pct": 0.18, "revenue_yoy": 0.22, "market_cap": 150_000_000_000},
        "KLAC": {"symbol": "KLAC", "latest_report_date": "2026-03-01", "next_report_date": "2026-06-20", "market_cap": 90_000_000_000},
    }
    result = build_peer_earnings_signals_for_test(
        groups,
        earnings,
        {"AMAT": _history(prices_leader, volumes_leader), "KLAC": _history(prices_target, volumes_target)},
        as_of=date(2026, 6, 5),
    )
    assert len(result) == 1
    assert result[0]["sublane_relation"] == "邻近赛道"
    assert result[0]["peer_similarity_breakdown"]["business"] == 0.72


def test_unlisted_cross_sublane_pair_is_rejected() -> None:
    prices_leader = [100.0] * 30 + [108.0, 112.0, 114.0, 116.0]
    prices_target = [50.0] * 30 + [50.2, 50.5, 50.8, 51.0]
    volumes_leader = [100_000] * 30 + [280_000, 180_000, 150_000, 120_000]
    volumes_target = [90_000] * 30 + [100_000, 105_000, 110_000, 115_000]
    groups = [{"id": "payments", "label": "payments", "similarity": 0.89, "symbols": ["V", "PYPL"]}]
    earnings = {
        "V": {"symbol": "V", "latest_report_date": "2026-06-01", "eps_surprise_pct": 0.18, "revenue_yoy": 0.22, "market_cap": 600_000_000_000},
        "PYPL": {"symbol": "PYPL", "latest_report_date": "2026-03-01", "next_report_date": "2026-06-20", "market_cap": 60_000_000_000},
    }
    assert build_peer_earnings_signals_for_test(
        groups,
        earnings,
        {"V": _history(prices_leader, volumes_leader), "PYPL": _history(prices_target, volumes_target)},
        as_of=date(2026, 6, 5),
    ) == []


def test_extended_peer_catalog_is_complete_and_auditable() -> None:
    groups = list_peer_groups()
    summary = peer_catalog_summary()
    assert summary["version"] == PEER_MATCH_VERSION
    assert summary["group_count"] >= 80
    assert summary["symbol_count"] >= 350
    assert summary["sublane_count"] >= 100
    assert summary["adjacent_rule_count"] >= 50
    assert len({group["id"] for group in groups}) == len(groups)
    assert all(group["sublanes"] for group in groups)
    payments = next(group for group in groups if group["id"] == "payments")
    assert "FISV" in payments["symbols"]
    assert "FI" not in payments["symbols"]
