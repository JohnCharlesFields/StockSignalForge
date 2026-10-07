"""Pin that the research/calibration CLI default universes are resolvable.

The walk-forward backtest (research_signal_framework_backtest.py) and the
calibration builder (build_signal_calibration.py) both default to
``--universes spx,ndx,sox``. Those must all resolve under the platform's
index-pool reset (see test_research_universe_service.py, which deliberately keeps
sp100/komp/soxx/spmo/aiq/us_volume NON-resolvable). This test runs offline by
forcing the bundled registry fallback and stubbing the snapshot writer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research_universe_service import _source_audit
from scripts import screening_framework_v2_optimized as sf

DEFAULT_UNIVERSES = ["spx", "ndx", "sox"]


@pytest.fixture(autouse=True)
def _offline_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the bundled fallback path and avoid snapshot disk writes."""

    def _fallback(key: str) -> tuple[list[str], str]:
        spec = sf.UNIVERSE_REGISTRY[key]
        tickers = sorted({sf.normalize_yfinance_symbol(x) for x in spec["fallback"]})
        return tickers, f"fallback:bundled_{key}"

    monkeypatch.setattr(sf, "load_registered_universe", _fallback)
    monkeypatch.setattr(
        sf, "archive_universe_snapshot",
        lambda key, tickers, source, effective_date=None: Path(f"<stub:{key}>"),
    )


@pytest.mark.parametrize("name", DEFAULT_UNIVERSES)
def test_default_universe_resolves(name: str) -> None:
    tickers, source, memberships = sf.resolve_universe(name)
    assert tickers, f"expected non-empty ticker list for universe {name!r}"
    assert all(isinstance(t, str) and t for t in tickers)
    assert source
    assert memberships


def test_source_audit_flags_fallback_and_snapshot() -> None:
    fallback = _source_audit("fallback:bundled_spx", 100)
    assert fallback["source_kind"] == "fallback"
    assert fallback["is_fallback"] is True
    assert "fallback" in fallback["bias_warning"]

    live_snapshot = _source_audit("live:https://example.test;snapshot:/tmp/spx/2026-06-17.json", 500)
    assert live_snapshot["source_kind"] == "live"
    assert live_snapshot["is_archive"] is True
    assert live_snapshot["snapshot_path"] == "/tmp/spx/2026-06-17.json"
