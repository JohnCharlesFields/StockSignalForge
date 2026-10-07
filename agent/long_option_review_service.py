"""On-demand DeepSeek review of an already-computed equity direction signal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from long_option_screen_service import SNAPSHOT_PATH, read_snapshot

_CACHE_DIR = SNAPSHOT_PATH.parent / "reviews"
_LOCK = threading.Lock()


def _fallback(status: str, reason: str, symbol: str) -> dict[str, Any]:
    return {"available": False, "status": status, "symbol": symbol, "reason": reason,
            "model_view": "WAIT", "final_view": "WAIT"}


def _parse_json(content: str) -> dict[str, Any]:
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("model output must be JSON object")
    return parsed


def _list(value: Any, count: int = 4) -> list[str]:
    return [str(item)[:220] for item in value[:count] if isinstance(item, str)] if isinstance(value, list) else []


def review_symbol(symbol: str) -> dict[str, Any]:
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
        return _fallback("invalid_symbol", "标的代码格式不正确。", symbol)
    result = read_snapshot()
    if not result.get("available") or result.get("stale"):
        return _fallback("stale_snapshot", "请先运行当日正股筛选，再进行模型复核。", symbol)
    snapshot = result["snapshot"]
    row = next((item for item in snapshot.get("rows", []) if item.get("symbol") == symbol), None)
    if not row:
        return _fallback("not_in_snapshot", "当前快照中没有此标的。", symbol)
    if row.get("status") != "signal" or row.get("side") not in {"C", "P"}:
        return _fallback("no_direction", "当前正股规则没有明确方向，不调用模型强行给出 Call/Put。", symbol)

    provider = os.getenv("LANGCHAIN_PROVIDER", "")
    model = os.getenv("LANGCHAIN_MODEL_NAME", "")
    context = {
        key: row.get(key) for key in (
            "symbol", "spot", "stock_data_as_of", "side", "net_consensus", "bull_consensus",
            "bear_consensus", "relative_strength_20d", "hv20", "technical_context",
        )
    }
    digest = hashlib.sha256(json.dumps({"context": context, "provider": provider, "model": model},
                                      sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]
    path = _CACHE_DIR / f"{symbol}_{digest}.json"
    with _LOCK:
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if datetime.now(timezone.utc) - datetime.fromisoformat(cached["created_at"]) < timedelta(hours=12):
                    return cached
            except (OSError, ValueError, KeyError, TypeError):
                pass

    system = (
        "你是美股正股方向与单腿期权研究复核员，不是交易执行员。"
        "根据提供的正股事实复核接下来1-10个交易日方向，用户可能人工选择约60天到期的买入Call或Put。"
        "必须给出支持和反对证据；规则方向不可靠、行情不足或震荡时选择WAIT。"
        "形态与斐波那契仅供描述，不是独立验证的买点。只能复述wave_structure中程序已检出的候选编号；"
        "no_pattern、unverified或stale_pattern不得断言当前第几浪或ABC，impulse_forming不得说第五浪已完成。"
        "不得编造期权合约、报价、IV、期权盈利概率、事件、财报或未提供的价格。"
        "正股看对仍可因权利金和IV变化而亏损。只返回JSON对象。"
    )
    user = (
        "请以中文复核以下正股方向。输出JSON字段："
        "model_view（CALL/PUT/WAIT三选一）、summary（一句话）、supporting_points（最多3条）、"
        "objections（最多3条）、watch_condition（等待何种正股价格行为确认）、"
        "invalidation_condition（什么正股价格行为推翻判断）、wave_note（仅解释提供的候选结构；没有当前候选就写未识别）。"
        "不要产生任何新价格数值，关键价位由程序单独展示。\n"
        + json.dumps(context, ensure_ascii=False, allow_nan=False)
    )
    try:
        from src.providers.chat import ChatLLM

        response = ChatLLM().chat([{"role": "system", "content": system}, {"role": "user", "content": user}], timeout=45)
        raw = _parse_json(response.content or "")
        view = str(raw.get("model_view", "")).upper()
        if view not in {"CALL", "PUT", "WAIT"}:
            raise ValueError("invalid model_view")
        rule_view = "CALL" if row["side"] == "C" else "PUT"
        review = {
            "available": True, "status": "reviewed", "symbol": symbol,
            "provider": provider, "model": model, "rule_view": rule_view,
            "model_view": view, "final_view": view if view == rule_view else "WAIT",
            "conflict": view not in {"WAIT", rule_view},
            "summary": str(raw.get("summary") or "")[:350],
            "supporting_points": _list(raw.get("supporting_points"), 3),
            "objections": _list(raw.get("objections"), 3),
            "watch_condition": str(raw.get("watch_condition") or "")[:250],
            "invalidation_condition": str(raw.get("invalidation_condition") or "")[:250],
            "wave_note": _wave_note(context.get("technical_context")),
            "technical_context": row.get("technical_context") or {},
            "stock_data_as_of": row.get("stock_data_as_of"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "does_not_override_rules": True,
        }
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(review, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temp.replace(path)
        return review
    except Exception as exc:
        return _fallback("model_unavailable", f"DeepSeek 复核暂不可用：{type(exc).__name__}", symbol)


def _wave_note(technical: Any) -> str:
    wave = (technical or {}).get("wave_structure") or {}
    status = wave.get("status")
    if status == "abc_candidate":
        return "程序识别到已确认的 A-B-C 调整候选；不代表调整已经结束。"
    if status == "impulse_candidate":
        return "程序识别到已完成的 1-5 推动浪候选；不代表趋势将延续。"
    if status == "impulse_forming":
        return "程序识别到 0-4 摆动候选；第 5 浪尚未确认。"
    if status == "stale_pattern":
        return "仅有较早的历史浪形候选，不代表当前仍在该浪中。"
    return "程序未识别当前可复核的标准五浪或 A-B-C 候选。"
