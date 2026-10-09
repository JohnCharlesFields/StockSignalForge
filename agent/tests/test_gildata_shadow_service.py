from __future__ import annotations

import json

import gildata_shadow_service as service
import macro_panic_service
import pytest
import requests


def _table(name: str, header: str, *rows: str) -> dict:
    divider = "|" + "|".join("---" for _ in header.strip("|").split("|")) + "|"
    return {"api_name": name, "table_markdown": "\n".join((header, divider, *rows))}


def _fixtures() -> tuple[list[dict], list[dict]]:
    facts = [
        _table(
            "美股日行情",
            "|证券代码|交易日期|总市值(万元)|币种|",
            "|AAPL|2026-09-24|497763697.26|USD|",
            "|AAPL|2026-09-25|497763697.26|USD|",
            "|MSFT|2026-09-25|400000000|USD|",
        ),
        _table("美股价值分析", "|证券代码|交易日|总市值(亿元)|市盈率PE|市净率PB(MRQ)|市销率PS|",
               "|AAPL|2026-09-25|49776.37|39.10|46.34|10.75|"),
        _table("美股盈利预测", "|股票代码|截止日期|预测指标|预测数据币种|统计周期(天)|预测值平均数|预测次数(次)|",
               "|AAPL|2026-09-25|目标价|美元|100|337.68|36|"),
    ]
    ratings = [
        _table("美股机构评级", "|股票代码|截止日期|统计周期(天)|买入评级机构数(个)|增持评级机构数(个)|中性评级机构数(个)|减持评级机构数(个)|卖出评级机构数(个)|评级机构总数(个)|",
               "|AAPL|2026-09-24|100|23|10|16|1|2|52|",
               "|AAPL|2026-09-25|100|20|8|13|2|3|46|")
    ]
    return facts, ratings


def test_equity_normalization_requires_exact_symbol_date_and_usd() -> None:
    facts, ratings = _fixtures()
    sample = service.normalize_equity("aapl", "2026-09-25", facts, ratings)

    assert sample["market_cap_usd"] == 4_977_636_972_600
    assert sample["pe"] == 39.10
    assert sample["target_avg_usd"] == 337.68
    assert sample["target_window_days"] == 100
    assert sample["ratings"]["buy"] == 20
    assert sample["ratings"]["total"] == 46
    assert sample["shadow_only"] is True


def test_missing_or_mismatched_observations_do_not_become_current_facts() -> None:
    facts, ratings = _fixtures()
    assert service.normalize_equity("AAPL", "2026-09-26", facts, ratings)["available_fields"] == []
    facts[0] = _table("美股日行情", "|证券代码|交易日期|总市值(万元)|币种|",
                      "|AAPL|2026-09-25|497763697.26|CNY|")
    facts[1] = _table("美股价值分析", "|证券代码|交易日|总市值(亿元)|市盈率PE|",
                      "|AAPL|2026-09-25|100|39.10|")
    sample = service.normalize_equity("AAPL", "2026-09-25", facts, ratings)
    assert "market_cap_usd" not in sample
    assert sample["market_cap_warning"] == "daily_currency_not_usd"


def test_ratings_are_not_accepted_if_category_total_does_not_match() -> None:
    facts, ratings = _fixtures()
    ratings[0]["table_markdown"] = ratings[0]["table_markdown"].replace("|2|3|46|", "|2|3|99|")
    sample = service.normalize_equity("AAPL", "2026-09-25", facts, ratings)
    assert "ratings" not in sample
    assert sample["rating_warning"] == "category_total_mismatch"


def test_vix_requires_exact_daily_observation() -> None:
    results = [_table("指数日行情", "|指数代码|交易日|收盘价(点)|", "|VIX|2026-09-25|14.87|")]
    assert service.normalize_vix("2026-09-25", results)["value"] == 14.87
    assert service.normalize_vix("2026-09-26", results)["value"] is None


def test_cached_vix_is_opt_in_exact_session_and_offline(monkeypatch) -> None:
    class CachedPath:
        def __truediv__(self, part):
            return self

        def read_text(self, encoding):
            return json.dumps({"as_of": "2026-09-25", "value": 14.87})

    monkeypatch.setattr(service, "_root", CachedPath)
    monkeypatch.delenv("GILDATA_VIX_FALLBACK", raising=False)
    monkeypatch.delenv("GILDATA_REFERENCE_PRIMARY", raising=False)
    assert service.cached_vix("2026-09-25") is None
    monkeypatch.setenv("GILDATA_VIX_FALLBACK", "1")
    assert service.cached_vix("2026-09-25")["value"] == 14.87
    assert service.cached_vix("2026-09-24") is None


def test_mcp_payload_reads_json_rpc_text_without_guessing_from_prose() -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [
        {"type": "text", "text": json.dumps({"code": 0, "results": [_table("指数日行情", "|指数代码|交易日|收盘价(点)|", "|VIX|2026-09-25|14.87|")]})}
    ]}})
    assert service._mcp_payload(body)["code"] == 0


def test_finquery_retries_one_connection_error_without_logging_token(monkeypatch) -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [
        {"type": "text", "text": json.dumps({"code": 0, "results": []})}
    ]}}).encode()

    class Response:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield body

        def close(self):
            pass

    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise requests.exceptions.SSLError("transient handshake")
        return Response()

    monkeypatch.setenv("GILDATA_MCP_TOKEN", "private-test-token")
    monkeypatch.setenv("GILDATA_MCP_URL", "https://mcp.example.invalid/finance")
    monkeypatch.setattr(service.requests, "post", post)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)

    results, stats = service._finquery("test")

    assert results == []
    assert stats["attempts"] == 2
    assert len(calls) == 2


def test_finquery_rejects_oversized_response(monkeypatch) -> None:
    class Response:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"too-large"

        def close(self):
            pass

    monkeypatch.setenv("GILDATA_MCP_TOKEN", "private-test-token")
    monkeypatch.setenv("GILDATA_MCP_URL", "https://mcp.example.invalid/finance")
    monkeypatch.setattr(service, "_MAX_RESPONSE_BYTES", 4)
    monkeypatch.setattr(service.requests, "post", lambda *args, **kwargs: Response())

    with pytest.raises(ValueError, match="mcp_response_too_large"):
        service._finquery("test")


def test_macro_vix_uses_shadow_only_after_primary_sources_fail(monkeypatch) -> None:
    import pandas as pd

    monkeypatch.setenv("GILDATA_VIX_FALLBACK", "1")
    monkeypatch.delenv("GILDATA_REFERENCE_PRIMARY", raising=False)
    monkeypatch.setattr(macro_panic_service, "_write_cache", lambda payload: None)
    monkeypatch.setattr(macro_panic_service, "get_cboe_vix_latest", lambda index: {"available": False})
    monkeypatch.setattr(macro_panic_service, "get_daily_history", lambda *args, **kwargs: (pd.DataFrame(), "empty"))
    monkeypatch.setattr(service, "cached_vix", lambda required_as_of: {"as_of": required_as_of, "value": 14.87})

    result = macro_panic_service.get_vix_regime(force_refresh=True)

    assert result["value"] == 14.87
    assert result["source"] == "gildata:VIX_daily:cached"
    assert result["source_type"] == "real_vix"
    assert result["data_as_of_date"]


@pytest.mark.parametrize("url", ["", "http://mcp.example.invalid/finance",
    "https://mcp.example.invalid/finance?token=private-test-token",
    "https://user:private-test-token@mcp.example.invalid/finance",
    "https://mcp.example.invalid/finance#private-config"])
def test_finquery_requires_private_https_endpoint_before_network(monkeypatch, url) -> None:
    monkeypatch.setenv("GILDATA_MCP_TOKEN", "private-test-token")
    monkeypatch.setenv("GILDATA_MCP_URL", url)

    def unexpected_request(*args, **kwargs):
        pytest.fail("MCP must not call the network without a configured HTTPS endpoint")

    monkeypatch.setattr(service.requests, "post", unexpected_request)
    with pytest.raises(ValueError, match="gildata_mcp_url_missing_or_invalid"):
        service._finquery("test")
