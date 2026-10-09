"""Read-only US-market capability sampling. No production cache writes."""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from gildata_shadow_service import _mcp_url, _parse_markdown_table, _query
from market_calendar import most_recent_session


def catalog():
    token = os.environ.get("GILDATA_MCP_TOKEN", "").strip()
    if not token:
        raise ValueError("missing_token")
    response = requests.post(
        _mcp_url(), params={"token": token},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream"}, timeout=(8, 30),
    )
    response.raise_for_status()
    body = response.text
    if len(body.encode("utf-8")) > 2_000_000:
        raise ValueError("catalog_too_large")
    if body.lstrip().startswith(("data:", "event:")):
        body = next(line[5:].strip() for line in reversed(body.splitlines())
                    if line.startswith("data:") and line[5:].strip().startswith("{"))
    envelope = json.loads(body)
    if envelope.get("error"):
        raise ValueError("catalog_rpc_error")
    return (envelope.get("result") or {}).get("tools") or []


def definitions(as_of):
    start = (datetime.fromisoformat(as_of) - timedelta(days=7)).date().isoformat()
    return [
        ("daily", "日行情", "FinQuery", f"仅查询美股AAPL、NVDA在{as_of}的日行情，含开高低收、昨收、成交量、成交额、换手率、总市值和币种。", ["AAPL", "NVDA"]),
        ("minute", "分钟/盘中行情", "FinQuery", f"查询美股AAPL在{as_of}纽约时间09:30至09:35的1分钟OHLCV，注明时区及延迟；没有该类数据请明确。", ["AAPL"]),
        ("adjustment", "复权及拆股", "FinQuery", "仅查询美国苹果AAPL在2020-08-28至2020-09-02的原始未复权和复权收盘价、复权因子、拆股比例及复权口径，不要A股。", ["AAPL"]),
        ("company", "公司与行业资料", "FinQuery", "查询美国上市公司AAPL、NVDA的证券代码、公司名称、上市交易所、上市日期、行业分类、总股本和流通股本，只要两只美股。", ["AAPL", "NVDA"]),
        ("valuation", "最新估值", "FinQuery", f"查询美股AAPL、NVDA在{as_of}的PE(TTM)、forward PE、PB、PS、EV/EBITDA及总市值，列明币种和日期。", ["AAPL", "NVDA"]),
        ("valuation_history", "历史估值", "FinQuery", "查询美股AAPL、NVDA在2026-09-25的PE、PB、PS和总市值，仅该日期，缺值请保留为空。", ["AAPL", "NVDA"]),
        ("income", "利润表", "FinQuery", "查询美股NVDA最近两个已披露季度的利润表，收入、营业利润、净利润、EPS、报告期、披露日、币种；不要盈利预测。", ["NVDA"]),
        ("balance", "资产负债表", "FinQuery", "查询美股NVDA最近两个已披露季度的现金及等价物、总资产、总负债、有息债务、股东权益、报告期和披露日，注明单位币种。", ["NVDA"]),
        ("cashflow", "现金流", "FinQuery", "查询美股NVDA最近两个已披露季度的经营现金流、资本支出、自由现金流、报告期和披露日，注明单位币种。", ["NVDA"]),
        ("forecast", "盈利预测", "FinQuery", f"查询美股AAPL、NVDA截止{as_of}的分析师EPS、营收、净利润一致预期，含预测报告期、均值、样本数、调高/调低次数、币种和单位。", ["AAPL", "NVDA"]),
        ("ratings", "评级目标价", "FinQuery", f"查询美股AAPL、NVDA截止{as_of}的五档机构评级、机构总数、美元目标价均值、中位数、最大最小值、标准差、统计周期和样本数。", ["AAPL", "NVDA"]),
        ("earnings_calendar", "下一财报日期", "FinQuery", f"查询美股AAPL、NVDA在{as_of}已知的下一次财报实际计划发布日期、盘前盘后、当季EPS及营收预期、来源与更新时间。不要用预测报告期代替发布日期。", ["AAPL", "NVDA"]),
        ("holders", "机构股东持仓", "FinQuery", "查询美股AAPL、NVDA最近一期机构股东持仓，含机构名、持股数、持股比例、报告期及披露日期；只需各股票10条记录。", ["AAPL", "NVDA"]),
        ("insiders", "内部人士交易", "FinQuery", f"查询美股AAPL在{start}至{as_of}的公司内部人士股票买卖，交易日期、披露日期、姓名、买卖方向、股数、价格。", ["AAPL"]),
        ("short_interest", "卖空与融券", "FinQuery", f"查询美股AAPL、NVDA在{as_of}或之前最近一期short interest卖空未平仓、占流通股比例、days to cover、借券费率，列明统计日和披露日。不要A股融资融券。", ["AAPL", "NVDA"]),
        ("options", "期权链/IV/OI", "FinQuery", f"查询美股AAPL在{as_of}、到期2026-12-18、行权价340的Call和Put真实期权合约、bid/ask、成交量、OI、IV、Delta、Gamma、报价时间；不返回正股代替期权。", ["AAPL"]),
        ("index", "美股指数行情", "FinQuery", f"查询美国SPX标普500、NDX纳斯达克100、DJI道琼斯、RUT罗素2000在{as_of}的日收盘和涨跌幅；明确指数代码。", ["SPX", "NDX", "DJI", "RUT"]),
        ("vix_curve", "VIX及期限结构", "FinQuery", f"查询美国CBOE VIX、VIX9D、VIX3M、VVIX在{as_of}的日收盘点位，明确指数代码日期，不返回国内指数。", ["VIX", "VIX9D", "VIX3M", "VVIX"]),
        ("constituents", "指数成分及历史快照", "FinQuery", "查询美国纳斯达克100指数NDX在2026-09-01和2026-10-01各自的历史成分股清单、股票代码、权重、生效日、发布日；没有历史快照请明确，不用今天名单代替。", []),
        ("etf_holdings", "美国ETF持仓", "FinQuery", f"查询美国上市ETF SOXX、SPY在{as_of}或之前最近一期的持仓股票代码、权重、持仓日期和来源，只需前10条记录。不要中国QDII基金。", []),
        ("macro_inflation", "美国宏观通胀就业", "MacroIndustryData", "查询美国CPI同比、核心PCE同比、失业率各最近3期实际公布值，列明观测期、公布日期、单位、原始数据来源。", []),
        ("macro_rates", "美国利率与国债", "MacroIndustryData", f"查询美国联邦基金目标利率上下限、2年和10年美债收益率在{as_of}或之前最近3期的值、日期及来源。", []),
        ("breadth_flows", "市场宽度/ETF资金流", "FinQuery", f"查询美国NYSE/Nasdaq在{as_of}上涨下跌家数、创新高新低家数，以及美股ETF SPY/QQQ净资金流，明确市场、日期和单位。", ["SPY", "QQQ"]),
        ("news_stock", "美股个股新闻", "NewsDataQuery", f"仅检索美国上市公司英伟达NVIDIA（NVDA）在{start}至{as_of}的真实新闻，原文必须涉及该公司，最多5条，含日期、时区、媒体和链接。", ["NVDA"]),
        ("news_macro", "美国宏观新闻", "NewsDataQuery", f"检索{start}至{as_of}美国美联储Fed货币政策相关新闻，最多5条，含真实标题、发布日期、时区、原始媒体和链接，不要中国公司公告。", []),
        ("announcement", "美股SEC公告", "AnnouncementData", "查询美国英伟达NVDA最新一份向美国SEC提交的10-Q中的营收和风险披露，给出真实提交日期和SEC原文链接。没有美国公告覆盖请明确。", ["NVDA"]),
        ("research", "美股券商研究", "FinancialResearchReport", f"查询{start}至{as_of}直接研究美国英伟达NVIDIA/NVDA的机构研报，列明研报日期、机构、原文出处及观点，不要仅提及NVIDIA的A股公司研报。最多3条。", ["NVDA"]),
        ("balance_simple", "资产负债表简化复核", "FinQuery", "美股NVDA最近一期资产负债表的总资产、总负债、现金、股东权益。", ["NVDA"]),
        ("forecast_revenue", "营收一致预期复核", "FinQuery", f"美股NVDA截止{as_of}的销售收入一致预期，预测报告期、均值、标准差、调高调低次数。", ["NVDA"]),
        ("macro_components", "宏观指标分别复核", "MacroIndustryData", "美国核心PCE同比和失业率，分别给出最近3个月数值和指标代码。", []),
        ("macro_yields", "国债收益率分别复核", "MacroIndustryData", "美国2年期和10年期国债收益率，分别给出最近3个观测日及指标代码。", []),
        ("split_nvda", "拆股记录复核", "FinQuery", "美股NVDA在2024年6月10日的拆股比例、调整因子和事件日期。", ["NVDA"]),
        ("members_current", "当前指数成分复核", "FinQuery", "美国标普500指数SPX当前成分股代码和纳入日期，不要日行情。", []),
        ("news_nvidia_keyword", "个股新闻简化复核", "NewsDataQuery", "英伟达 NVIDIA 最新新闻", []),
        ("news_fed_keyword", "宏观新闻简化复核", "NewsDataQuery", "美联储 最新新闻", []),
        ("us_screen", "美股智能选股", "SmartStockSelection", f"仅美国上市普通股，{as_of}总市值超过1000亿美元，列出股票代码、名称、市值、币种，最多5只。", []),
        ("history_daily", "多日日线覆盖", "FinQuery", "美股AAPL、NVDA从2026-10-01至2026-10-07逐交易日日行情的开高低收和成交量。", ["AAPL", "NVDA"]),
        ("small_caps", "中小市值美股覆盖", "FinQuery", f"美股ONDS、ALNT、CEVA在{as_of}的日收盘价、总市值、币种和市盈率。", ["ONDS", "ALNT", "CEVA"]),
        ("options_fallback", "美股期权兜底复核", "FinDataFallbackQuery", f"FinQuery未取得期权数据，请兜底查询美股AAPL在{as_of}真实期权合约bid/ask、IV、OI，不能用正股或中国期权替代。", ["AAPL"]),
        ("news_evidence_nvidia", "个股新闻来源字段复核", "NewsDataQuery", "英伟达最新新闻", []),
        ("news_evidence_fed", "宏观新闻来源字段复核", "NewsDataQuery", "美联储最新新闻", []),
    ]


def summarize(result, requested):
    tables = []
    matching = set()
    returned = set()
    for item in result[:20]:
        raw = str(item.get("table_markdown") or "")
        is_table = raw.lstrip().startswith("|")
        rows = _parse_markdown_table(raw) if is_table else []
        codes = {str(row[key]).upper() for row in rows for key in ("证券代码", "股票代码", "指数代码", "基金代码") if row.get(key)}
        returned.update(codes)
        matching.update(codes & set(requested))
        # Keep small structured samples, not the raw vendor payload or full news.
        sample = [row for row in rows if any(str(row.get(k, "")).upper() in requested
                  for k in ("证券代码", "股票代码", "指数代码"))][:3] if requested else rows[:3]
        groups = {}
        for row in rows:
            group = tuple(row.get(k, "") for k in
                          ("指标代码", "指标名称", "财务科目名称", "预测指标", "指数代码"))
            if any(group) and group not in groups and len(groups) < 24:
                groups[group] = row
        dates = sorted({row[k] for row in rows for k in
                        ("交易日", "交易日期", "截止日期", "日期", "结束日期") if row.get(k)})
        news_fields = {}
        if not is_table:
            for key in ("撰写时间", "发布时间", "新闻舆情来源", "撰写机构"):
                match = re.search(re.escape(key) + r"[：:]([^\n；]+)", raw)
                if match:
                    news_fields[key] = match.group(1).strip()
            news_fields["has_http_url_in_text"] = bool(re.search(r"https?://", raw))
            news_fields["has_explicit_timezone"] = bool(re.search(r"北京时间|美东|纽约时间|UTC|[+-]\d{2}:\d{2}", raw))
        tables.append({"api_name": item.get("api_name"), "structured_table": is_table,
                       "parsed_rows_capped": len(rows), "columns": list(rows[0]) if rows else [],
                       "returned_codes": sorted(codes)[:30], "requested_code_sample": sample,
                       "group_samples": list(groups.values()),
                       "observed_date_count": len(dates), "observed_date_min": dates[0] if dates else None,
                       "observed_date_max": dates[-1] if dates else None,
                       "text_source_fields": news_fields,
                       "metadata_keys": sorted(item),
                       "title": item.get("title"), "excerpt": raw[:180] if not is_table else None})
    return {"result_count": len(result), "tables": tables, "returned_codes": sorted(returned)[:60],
            "matched_requested_codes": sorted(matching),
            "screen_status": "matched_code_requires_topic_date_check" if matching else
                             "structured_requires_manual_check" if any(t["structured_table"] for t in tables) else
                             "unstructured_or_no_matching_us_code" if tables else "empty"}


def save(report, output):
    output.mkdir(parents=True, exist_ok=True)
    (output / "catalog.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--as-of", default=most_recent_session().isoformat())
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    output = Path(args.output)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "as_of": args.as_of,
              "scope": "bounded_samples_not_exhaustive_database_dictionary", "production_modified": False,
              "tools": [], "probes": []}
    try:
        report["tools"] = catalog()
    except Exception as exc:
        report["catalog_error"] = type(exc).__name__
    available = {t["name"] for t in report["tools"]}
    save(report, output)
    print(json.dumps({"tools": sorted(available)}, ensure_ascii=False), flush=True)
    for ident, category, tool, query, requested in definitions(args.as_of):
        if args.only and ident not in args.only:
            continue
        started = time.monotonic()
        row = {"id": ident, "category": category, "tool": tool, "query": query, "requested_codes": requested}
        if tool not in available:
            row["error_type"] = "tool_not_in_catalog"
        else:
            try:
                results, stats = _query(tool, query)
                row.update(summarize(results, requested), transport_status="ok", **stats)
            except Exception as exc:
                row.update(transport_status="failed", error_type=type(exc).__name__,
                           http_status=getattr(getattr(exc, "response", None), "status_code", None))
        row["elapsed_seconds"] = round(time.monotonic() - started, 2)
        report["probes"].append(row)
        save(report, output)
        print(json.dumps({k: row.get(k) for k in ("id", "category", "transport_status", "screen_status", "error_type", "result_count", "elapsed_seconds")}, ensure_ascii=False), flush=True)
    print(json.dumps({"report": str(output / "catalog.json"), "probes": len(report["probes"])}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
