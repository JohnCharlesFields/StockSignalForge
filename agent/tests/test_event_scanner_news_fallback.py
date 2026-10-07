from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_scanner():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "event_driven_sp500" / "sp500_event_scanner.py"
    spec = importlib.util.spec_from_file_location("sp500_event_scanner_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_news_items_fill_yahoo_shortfall_with_google(monkeypatch):
    scanner = _load_scanner()
    monkeypatch.setattr(
        scanner,
        "yahoo_rss_items",
        lambda symbol, max_items, cache_dir=None, cache_ttl_minutes=30: [
            {"title": "Yahoo item", "url": "https://news.example/1", "source": "yahoo_rss"}
        ],
    )
    monkeypatch.setattr(
        scanner,
        "get_google_news_rss",
        lambda query, max_items, period, ttl_seconds: {
            "available": True,
            "items": [
                {"title": "Google item", "url": "https://news.example/2", "source": "google_news_rss"},
                {"title": "Duplicate", "url": "https://news.example/1", "source": "google_news_rss"},
            ],
        },
    )

    items = scanner.news_items_with_fallback("AAPL", "Apple Inc.", 3)

    assert [item["source"] for item in items] == ["yahoo_rss", "google_news_rss"]
    assert [item["url"] for item in items] == ["https://news.example/1", "https://news.example/2"]
