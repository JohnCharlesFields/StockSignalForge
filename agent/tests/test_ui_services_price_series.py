from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pandas as pd


def _workspace_tmp_dir() -> Path:
    path = Path("agent/runs") / f"ui_services_test_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_load_run_context_reads_config_source_and_interval() -> None:
    from src.ui_services import load_run_context

    run_dir = _workspace_tmp_dir()
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "codes": ["AAPL.US"],
                "start_date": "2026-05-01",
                "end_date": "2026-05-20",
                "source": "stooq",
                "interval": "1D",
            }
        ),
        encoding="utf-8",
    )

    context = load_run_context(run_dir)

    assert context["codes"] == ["AAPL.US"]
    assert context["start_date"] == "2026-05-01"
    assert context["end_date"] == "2026-05-20"
    assert context["source"] == "stooq"
    assert context["interval"] == "1D"


def test_reconstruct_price_series_uses_configured_loader(monkeypatch) -> None:
    from src import ui_services

    run_dir = _workspace_tmp_dir()
    (run_dir / "code").mkdir()
    (run_dir / "code" / "signal_engine.py").write_text("class SignalEngine: pass\n", encoding="utf-8")
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "codes": ["AAPL.US"],
                "start_date": "2026-05-01",
                "end_date": "2026-05-20",
                "source": "stooq",
                "interval": "1D",
            }
        ),
        encoding="utf-8",
    )

    class FakeStooq:
        def fetch(self, codes, start_date, end_date, interval="1D"):
            assert codes == ["AAPL.US"]
            assert start_date <= "2026-05-01"
            assert end_date == "2026-05-20"
            assert interval == "1D"
            return {
                "AAPL.US": pd.DataFrame(
                    {
                        "open": [100.0],
                        "high": [102.0],
                        "low": [99.0],
                        "close": [101.0],
                        "volume": [1_000_000],
                    },
                    index=pd.DatetimeIndex([pd.Timestamp("2026-05-01")], name="trade_date"),
                )
            }

    monkeypatch.setattr(
        "backtest.loaders.registry.get_loader_cls_with_fallback",
        lambda source: FakeStooq,
    )

    rows = ui_services.reconstruct_price_series(run_dir)

    assert rows == [
        {
            "time": "2026-05-01",
            "timestamp": "2026-05-01",
            "code": "AAPL.US",
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": 1_000_000,
        }
    ]
