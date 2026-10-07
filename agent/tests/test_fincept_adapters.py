from __future__ import annotations

import pandas as pd
import fincept_adapters as adapters


def test_cboe_vix_latest_parses_csv(monkeypatch):
    monkeypatch.setattr(adapters, "_cache_path", lambda kind, key: "unused-cache.json")
    monkeypatch.setattr(adapters, "_fresh", lambda path, ttl_seconds: False)
    monkeypatch.setattr(adapters, "_write_json", lambda path, payload: None)
    monkeypatch.setattr(
        adapters,
        "_request_text",
        lambda url: "DATE,OPEN,HIGH,LOW,CLOSE\n2026-06-12,18,20,17,19.25\n",
    )

    result = adapters.get_cboe_vix_latest("VIX", ttl_seconds=0)

    assert result["available"] is True
    assert result["value"] == 19.25
    assert result["source_type"] == "real_vix"
    assert result["source"].startswith("cboe:")


def test_cboe_option_chain_maps_to_yfinance_like_frames(monkeypatch):
    monkeypatch.setattr(adapters, "_cache_path", lambda kind, key: "unused-cache.json")
    monkeypatch.setattr(adapters, "_fresh", lambda path, ttl_seconds: False)
    monkeypatch.setattr(adapters, "_write_json", lambda path, payload: None)
    monkeypatch.setattr(
        adapters,
        "_request_json",
        lambda url: {
            "data": {
                "symbol": "AAPL",
                "current_price": 200.0,
                "iv30": 0.25,
                "options": [
                    {
                        "option": "AAPL260626C00200000",
                        "last": 5.2,
                        "bid": 5.1,
                        "ask": 5.4,
                        "volume": 120,
                        "open_interest": 2400,
                        "iv": 31.0,
                        "gamma": 0.0123,
                    },
                    {
                        "option": "AAPL260626P00195000",
                        "last": 4.2,
                        "bid": 4.0,
                        "ask": 4.3,
                        "volume": 90,
                        "open_interest": 1800,
                        "iv": 0.29,
                        "gamma": 0.011,
                    },
                ],
            }
        },
    )

    chain = adapters.get_cboe_option_chain("AAPL", "2026-06-26", ttl_seconds=0)
    calls, puts = adapters.cboe_chain_to_frames(chain)

    assert chain["available"] is True
    assert chain["expiries"] == ["2026-06-26"]
    assert list(calls["strike"]) == [200.0]
    assert list(puts["strike"]) == [195.0]
    assert int(calls.iloc[0]["openInterest"]) == 2400
    assert float(calls.iloc[0]["impliedVolatility"]) == 0.31


def test_cboe_chain_to_frames_handles_empty_payload():
    calls, puts = adapters.cboe_chain_to_frames({"calls": [], "puts": []})

    assert isinstance(calls, pd.DataFrame)
    assert isinstance(puts, pd.DataFrame)
    assert calls.empty
    assert puts.empty


def test_google_news_rss_parses_items(monkeypatch):
    monkeypatch.setattr(adapters, "_cache_path", lambda kind, key: "unused-cache.json")
    monkeypatch.setattr(adapters, "_fresh", lambda path, ttl_seconds: False)
    monkeypatch.setattr(adapters, "_write_json", lambda path, payload: None)
    monkeypatch.setattr(
        adapters,
        "_request_text",
        lambda url: """<?xml version="1.0"?><rss><channel><item>
        <title>Apple wins AI contract</title>
        <link>https://news.example/aapl</link>
        <pubDate>Mon, 15 Jun 2026 12:00:00 GMT</pubDate>
        <description><![CDATA[Apple shares rise after contract.]]></description>
        <source url="https://news.example">Example News</source>
        </item></channel></rss>""",
    )

    result = adapters.get_google_news_rss("AAPL stock", max_items=3, ttl_seconds=0)

    assert result["available"] is True
    assert result["count"] == 1
    assert result["items"][0]["source"] == "google_news_rss"
    assert result["items"][0]["publisher"] == "Example News"


def test_sec_recent_filings_maps_ticker_and_submissions(monkeypatch):
    monkeypatch.setattr(adapters, "_cache_path", lambda kind, key: "unused-cache.json")
    monkeypatch.setattr(adapters, "_fresh", lambda path, ttl_seconds: False)
    monkeypatch.setattr(adapters, "_write_json", lambda path, payload: None)

    def fake_json(url):
        if url.endswith("company_tickers.json"):
            return {"0": {"ticker": "AAPL", "cik_str": 320193, "title": "Apple Inc."}}
        return {
            "name": "Apple Inc.",
            "filings": {
                "recent": {
                    "form": ["10-Q", "8-K"],
                    "filingDate": ["2026-05-01", "2026-04-25"],
                    "reportDate": ["2026-03-31", "2026-04-25"],
                    "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
                    "primaryDocument": ["aapl-20260331.htm", "aapl-8k.htm"],
                    "primaryDocDescription": ["Quarterly report", "Current report"],
                }
            },
        }

    monkeypatch.setattr(adapters, "_request_json", fake_json)

    result = adapters.get_sec_recent_filings("AAPL", limit=1, forms=["10-Q"], ttl_seconds=0)

    assert result["available"] is True
    assert result["cik"] == "0000320193"
    assert result["count"] == 1
    assert result["filings"][0]["form"] == "10-Q"
    assert result["filings"][0]["source"] == "sec:submissions"
