from __future__ import annotations

import priority_board_service as svc


def test_sector_diffusion_attaches_review_only_label(monkeypatch):
    picks = [
        {
            "symbol": "AAA",
            "calibrated_probability": 0.56,
            "playbook_enhancements": {
                "industry_etf": "XLK",
                "playbook_score": 0.7,
                "stock_stronger_than_industry": True,
                "labels": [],
            },
        },
        {
            "symbol": "BBB",
            "calibrated_probability": 0.52,
            "playbook_enhancements": {
                "industry_etf": "XLK",
                "playbook_score": 0.6,
                "stock_stronger_than_industry": False,
                "labels": [],
            },
        },
        {
            "symbol": "CCC",
            "calibrated_probability": 0.51,
            "playbook_enhancements": {
                "industry_etf": "XLK",
                "playbook_score": 0.55,
                "stock_stronger_than_industry": False,
                "labels": [],
            },
        },
    ]

    monkeypatch.setattr(
        svc,
        "priority_candidate_recent",
        lambda days=12: [
            {"as_of_date": "2026-06-20", "symbol": "AAA", "payload": {"playbook_enhancements": {"industry_etf": "XLK"}}},
            {"as_of_date": "2026-06-21", "symbol": "AAA", "payload": {"playbook_enhancements": {"industry_etf": "XLK"}}},
        ],
    )

    before = [p["calibrated_probability"] for p in picks]
    svc._attach_sector_diffusion_labels(picks)
    after = [p["calibrated_probability"] for p in picks]

    assert before == after
    for pick in picks:
        enhancement = pick["playbook_enhancements"]
        assert enhancement["sector_diffusion"]["evidence_level"] == "soft_review_only"
        assert any(label["id"] == "sector_diffusion" for label in enhancement["labels"])
