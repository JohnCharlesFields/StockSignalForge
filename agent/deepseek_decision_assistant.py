"""DeepSeek-backed decision assistance for three-layer equity research.

The assistant is intentionally a review layer: it explains, challenges, and
flags conflicts in rule-based signals. It does not replace deterministic
ranking, sizing, invalidation, or risk controls.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


AGENT_DIR = Path(__file__).resolve().parent
_CACHE_PATH = Path(os.environ.get("DEEPSEEK_DECISION_ASSIST_CACHE", AGENT_DIR / "data_cache" / "deepseek_decision_assist.json"))
_CACHE_TTL_SECONDS = int(os.environ.get("DEEPSEEK_DECISION_ASSIST_TTL_SECONDS", str(12 * 3600)))
_MAX_CONTEXT_CHARS = int(os.environ.get("DEEPSEEK_DECISION_ASSIST_MAX_CONTEXT_CHARS", "9000"))


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(v) for v in value]
        return str(value)


def _load_cache() -> Dict[str, Any]:
    try:
        if _CACHE_PATH.exists():
            return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_CACHE_PATH)


def _context_digest(context: Dict[str, Any]) -> str:
    compact = {
        "llm_provider": os.getenv("LANGCHAIN_PROVIDER", ""),
        "llm_model": os.getenv("LANGCHAIN_MODEL_NAME", ""),
        "symbol": context.get("symbol"),
        "scan_universe": context.get("scan_universe"),
        "decision_grade": context.get("decision_grade"),
        "decision": context.get("decision"),
        "opportunity": {
            key: (context.get("opportunity") or {}).get(key)
            for key in (
                "spot",
                "opportunity_score",
                "risk_level",
                "liquidity_score",
                "pullback_rejection",
                "pullback_confirmation",
                "gamma_exposure",
                "market_cap",
                "trailing_pe",
                "forward_pe",
            )
        },
        "timing": {
            key: (context.get("timing") or {}).get(key)
            for key in ("signal", "signal_cn", "launch_score", "daily_tunnel", "peer_earnings")
        },
        "event": context.get("event"),
        "macro_regime": context.get("macro_regime"),
        "stock_panic": context.get("stock_panic"),
        "hypothesis_test": context.get("hypothesis_test"),
        "research_evidence": context.get("research_evidence"),
        "conflicts": context.get("conflicts"),
        "source_pools": context.get("source_pools") or (context.get("opportunity") or {}).get("source_pools"),
        "review_focus": _review_focus(context),
    }
    raw = json.dumps(_json_safe(compact), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _symbol_cache_key(context: Dict[str, Any]) -> str:
    universe = str(context.get("scan_universe") or "global").strip().lower()
    symbol = str(context.get("symbol") or "").strip().upper()
    provider = os.getenv("LANGCHAIN_PROVIDER", "")
    model = os.getenv("LANGCHAIN_MODEL_NAME", "")
    return f"symbol:{provider}:{model}:{universe}:{symbol}" if symbol else ""


def _text_blob(value: Any) -> str:
    try:
        return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True).lower()
    except Exception:
        return str(value or "").lower()


def _review_focus(context: Dict[str, Any]) -> str:
    universe = str(context.get("scan_universe") or "").strip().lower()
    peer_payload = context.get("peer_earnings") or (context.get("timing") or {}).get("peer_earnings")
    if isinstance(peer_payload, dict) and peer_payload:
        return "peer_relay"
    if universe in {"speculative_peer_earnings", "pre_earnings_revision"}:
        return "peer_relay"
    blob = _text_blob({
        "scan_universe": context.get("scan_universe"),
        "source_pools": context.get("source_pools") or (context.get("opportunity") or {}).get("source_pools"),
    })
    if any(token in blob for token in ("ai supply", "ai_supply", "ai关键", "ai 供应链", "small cap", "serenity")):
        return "ai_supply_chain"
    return "three_layer_research"


def _focus_instruction(focus: str) -> str:
    if focus == "peer_relay":
        return (
            "专项重点：这是同行财报接力复核。必须检查子赛道/客群/产品价格带是否相近，"
            "财报先发公司是否有足够市值与行业地位带动标的，避免小市值公司上涨反推大市值公司的伪传导，"
            "并明确相似度、滞涨、披露窗口和失效条件。"
        )
    if focus == "ai_supply_chain":
        return (
            "专项重点：这是 AI 关键供应链/小盘候选复核。必须检查公司产品是否位于 AI 算力、网络、存储、光通信、"
            "电力/散热、数据中心或半导体设备链条的关键环节，区分真实订单弹性与概念炒作，强调流动性、估值和事件风险。"
        )
    return (
        "专项重点：这是三层量化信号总览复核。重点检查机会质量、启动择时、事件风险、GEX/VIX、"
        "回调承接/启动确认和次日可退出性是否一致。"
    )


def get_cached_review(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a fresh cached review for a context, if available."""
    cache = _load_cache()
    digest_key = _context_digest(context)
    symbol_key = _symbol_cache_key(context)
    expected_focus = _review_focus(context)
    for key in (digest_key, symbol_key):
        if not key:
            continue
        item = cache.get(key)
        if not isinstance(item, dict):
            continue
        if time.time() - float(item.get("created_ts", 0) or 0) > _CACHE_TTL_SECONDS:
            continue
        if isinstance(item.get("review"), dict):
            review = item.get("review") or {}
            if review.get("provider") != os.getenv("LANGCHAIN_PROVIDER", "") or review.get("model") != os.getenv("LANGCHAIN_MODEL_NAME", ""):
                continue
            cached_focus = review.get("review_focus")
            if cached_focus and cached_focus != expected_focus:
                continue
            if not cached_focus:
                review["review_focus"] = expected_focus
                review.setdefault("specialist_notes", [])
            if key == digest_key and symbol_key and symbol_key not in cache:
                cache[symbol_key] = {"created_ts": item.get("created_ts", time.time()), "review": review}
                _save_cache(cache)
            return review
    return None


def _fallback_review(status: str, reason: str, context: Dict[str, Any]) -> Dict[str, Any]:
    focus = _review_focus(context)
    return {
        "available": False,
        "status": status,
        "provider": os.getenv("LANGCHAIN_PROVIDER", ""),
        "model": os.getenv("LANGCHAIN_MODEL_NAME", ""),
        "symbol": context.get("symbol"),
        "review_focus": focus,
        "verdict": "未复核",
        "confidence": 0.0,
        "risk_level": "unknown",
        "overnight_action": "人工复核",
        "summary": reason,
        "specialist_notes": [],
        "supporting_points": [],
        "objections": [reason],
        "risk_flags": ["llm_review_unavailable"],
        "watch_items": [],
        "invalid_if": [],
        "does_not_override_rules": True,
    }


def _build_prompt(context: Dict[str, Any]) -> List[Dict[str, str]]:
    payload = json.dumps(_json_safe(context), ensure_ascii=False)
    if len(payload) > _MAX_CONTEXT_CHARS:
        payload = payload[:_MAX_CONTEXT_CHARS] + "\n...TRUNCATED..."
    focus = _review_focus(context)
    focus_instruction = _focus_instruction(focus)
    system = (
        "你是美股短线隔夜研究复核员。你的职责是复核规则模型输出，"
        "指出支持证据、反对证据、隔夜风险和人工复核清单。"
        "不要给确定性买入建议，不要编造不存在的数据，不要覆盖规则分数。"
        "必须区分期权状态：no_listed_options 是没有上市期权，data_unavailable 是数据源失败，"
        "chain_available_but_no_tradable_legs 是有期权链但无法构建真实可交易组合腿；不要把三者统称为期权证据缺失。"
        "只输出严格 JSON，不要 markdown。"
    )
    user = (
        "请复核下面的三维量化信号。用户偏好：盘中到收盘阶段买入，次日开盘卖出；"
        "因此重点检查尾盘承接、隔夜事件风险、GEX/VIX 风险、次日开盘可退出性。\n"
        f"{focus_instruction}\n\n"
        "JSON schema:\n"
        "{"
        "\"available\":true,"
        f"\"review_focus\":\"{focus}\","
        "\"verdict\":\"优先复核|观察等待|风险回避|证据不足\","
        "\"confidence\":0.0,"
        "\"risk_level\":\"low|medium|high\","
        "\"overnight_action\":\"尾盘复核|等待突破|等待回踩|回避隔夜|人工复核\","
        "\"summary\":\"一句中文摘要\","
        "\"specialist_notes\":[\"专项复核要点\"],"
        "\"supporting_points\":[\"...\"],"
        "\"objections\":[\"...\"],"
        "\"risk_flags\":[\"...\"],"
        "\"watch_items\":[\"...\"],"
        "\"invalid_if\":[\"...\"],"
        "\"does_not_override_rules\":true"
        "}\n\n"
        f"三维信号上下文：\n{payload}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _extract_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _llm_review_only(context: Dict[str, Any]) -> Dict[str, Any]:
    """Pure LLM call + parse for one context. No cache I/O (so it is safe to run
    in a thread pool); callers persist the result."""
    provider = os.getenv("LANGCHAIN_PROVIDER", "")
    model = os.getenv("LANGCHAIN_MODEL_NAME", "")
    try:
        from src.providers.chat import ChatLLM

        response = ChatLLM().chat(_build_prompt(context), timeout=int(os.getenv("DEEPSEEK_DECISION_ASSIST_TIMEOUT", "45")))
        data = _extract_json(response.content or "")
        return {
            "available": True,
            "status": "llm_reviewed",
            "provider": provider,
            "model": model,
            "symbol": context.get("symbol"),
            "review_focus": str(data.get("review_focus") or _review_focus(context)),
            "verdict": str(data.get("verdict") or "人工复核"),
            "confidence": max(0.0, min(1.0, float(data.get("confidence", 0.0) or 0.0))),
            "risk_level": str(data.get("risk_level") or "unknown"),
            "overnight_action": str(data.get("overnight_action") or "人工复核"),
            "summary": str(data.get("summary") or ""),
            "specialist_notes": [str(x) for x in (data.get("specialist_notes") or [])][:4],
            "supporting_points": [str(x) for x in (data.get("supporting_points") or [])][:5],
            "objections": [str(x) for x in (data.get("objections") or [])][:5],
            "risk_flags": [str(x) for x in (data.get("risk_flags") or [])][:6],
            "watch_items": [str(x) for x in (data.get("watch_items") or [])][:6],
            "invalid_if": [str(x) for x in (data.get("invalid_if") or [])][:5],
            "does_not_override_rules": True,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    except Exception as exc:
        return _fallback_review("error", f"DeepSeek 复核失败：{type(exc).__name__}", context)


def _persist_reviews(items: List[tuple]) -> None:
    """Single load->update->save for a batch of (context, review) pairs.

    Doing one cache write avoids the lost-update race when reviews are produced
    concurrently."""
    if not items:
        return
    cache = _load_cache()
    now = time.time()
    for context, review in items:
        cache[_context_digest(context)] = {"created_ts": now, "review": review}
        symbol_key = _symbol_cache_key(context)
        if symbol_key:
            cache[symbol_key] = {"created_ts": now, "review": review}
    _save_cache(cache)


def review_context(context: Dict[str, Any], *, force: bool = False) -> Dict[str, Any]:
    """Run or load a DeepSeek decision-assistance review for one context."""
    if os.getenv("ENABLE_DEEPSEEK_DECISION_ASSIST", "1").lower() in {"0", "false", "no"}:
        return _fallback_review("disabled", "DeepSeek 决策辅助已关闭。", context)
    if not force:
        cached = get_cached_review(context)
        if cached:
            return cached
    review = _llm_review_only(context)
    _persist_reviews([(context, review)])
    return review


def review_top_contexts(contexts: Iterable[Dict[str, Any]], *, limit: int = 10, force: bool = False, cache_only: bool = False) -> Dict[str, Dict[str, Any]]:
    """Review top contexts and return symbol -> review.

    The per-context LLM calls (the slow part, ~3s each) run CONCURRENTLY in a
    small thread pool instead of serially -- e.g. Top-12 drops from ~34s to ~6s.
    Cache hits are served first (no LLM), and all new reviews are persisted in a
    single cache write to avoid concurrent lost updates.
    """
    items = [c for c in list(contexts)[: max(0, limit)] if str(c.get("symbol") or "")]
    reviews: Dict[str, Dict[str, Any]] = {}
    if not items:
        return reviews
    if os.getenv("ENABLE_DEEPSEEK_DECISION_ASSIST", "1").lower() in {"0", "false", "no"}:
        for c in items:
            reviews[str(c["symbol"])] = _fallback_review("disabled", "DeepSeek 决策辅助已关闭。", c)
        return reviews

    to_run: List[Dict[str, Any]] = []
    for c in items:
        symbol = str(c["symbol"])
        if not force:
            cached = get_cached_review(c)
            if cached:
                reviews[symbol] = cached
                continue
        if not cache_only:
            to_run.append(c)

    if to_run:
        workers = max(1, min(int(os.getenv("DEEPSEEK_REVIEW_WORKERS", "12")), len(to_run)))
        produced: List[tuple] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_llm_review_only, c): c for c in to_run}
            for future in as_completed(futures):
                c = futures[future]
                try:
                    review = future.result()
                except Exception as exc:
                    review = _fallback_review("error", f"DeepSeek 复核失败：{type(exc).__name__}", c)
                reviews[str(c["symbol"])] = review
                produced.append((c, review))
        _persist_reviews(produced)
    return reviews
