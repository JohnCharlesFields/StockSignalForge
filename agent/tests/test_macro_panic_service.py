import macro_panic_service as service
from macro_panic_service import _classify, panic_evidence_for_opportunity


def test_vix_extreme_panic_enables_reclaim_mode() -> None:
    regime = _classify(38.0, "real_vix")

    assert regime["level"] == "extreme_panic"
    assert regime["mode"] == "panic_reclaim"
    assert regime["systemic_crisis"] is False


def test_vix_crisis_triggers_systemic_filter() -> None:
    regime = _classify(48.0, "real_vix")

    assert regime["level"] == "crisis"
    assert regime["mode"] == "crisis_guard"
    assert regime["systemic_crisis"] is True
    assert "vix_above_45" in regime["systemic_risk_flags"]


def test_proxy_regime_is_marked_as_non_official_vix() -> None:
    regime = _classify(36.0, "proxy")

    assert regime["mode"] == "panic_reclaim"
    assert "proxy_not_official_vix" in regime["systemic_risk_flags"]


def test_vix_regime_prefers_cboe_official_value(monkeypatch) -> None:
    monkeypatch.setattr(service, "_write_cache", lambda payload: None)
    monkeypatch.setattr(
        service,
        "get_cboe_vix_latest",
        lambda index: {
            "available": True,
            "value": 31.7,
            "source": "cboe:VIX_History.csv",
            "source_type": "real_vix",
        },
    )

    regime = service.get_vix_regime(force_refresh=True)

    assert regime["value"] == 31.7
    assert regime["source"] == "cboe:VIX_History.csv"
    assert regime["source_type"] == "real_vix"
    assert "proxy_not_official_vix" not in regime["systemic_risk_flags"]


def test_panic_evidence_penalizes_crisis_guard() -> None:
    component = panic_evidence_for_opportunity(
        {"opportunity_score": 70, "risk_level": "低", "liquidity_score": 0.8},
        regime={
            "available": True,
            "value": 49.0,
            "display_name": "VIX",
            "label_cn": "危机级别",
            "mode": "crisis_guard",
            "source_type": "real_vix",
        },
    )

    assert component["id"] == "macro_vix_regime"
    assert component["label"] == "系统性危机过滤"
    assert component["probability"] < 0.5
