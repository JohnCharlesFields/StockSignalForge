"""News-sentiment digest from the Massive/Polygon news `insights` (already
entitled). One source of truth for the 舆情 shown on the priority board, the
single-stock page, and the portfolio rotation filter.

Honesty notes:
- ``sentiment`` is the publisher/AI-derived label from Polygon's insights, a
  heuristic — not a fact about the company.
- ``estimate_impact`` is a TRANSPARENT heuristic (sentiment direction scaled by
  the stock's own daily volatility / ATR), NOT a trained news->price model. It is
  labelled as such everywhere it surfaces.
- D. Trump (and other high-profile political) mentions are surfaced prominently
  because they historically cause outsized short-term, two-directional moves —
  this is a DISPLAY flag, not a validated tradeable signal.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app_database import cache_get, cache_set

# Politicians / figures whose mentions move single stocks hard. Trump first.
_TRUMP_TOKENS = ("trump",)


def _is_trump(article: Dict[str, Any]) -> bool:
    blob = " ".join([
        str(article.get("title") or ""),
        str(article.get("description") or ""),
        " ".join(str(k) for k in (article.get("keywords") or [])),
    ]).lower()
    return any(tok in blob for tok in _TRUMP_TOKENS)


def news_digest(symbol: str, days: int = 10, limit: int = 30) -> Dict[str, Any]:
    """Recent news sentiment digest for one ticker. Cached 2h (news calls cost
    budget). Returns counts, net score, a Trump section, and the article list."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return {"available": False}
    key = f"news_digest:v3:{sym}:{days}"
    cached = cache_get(key)
    if isinstance(cached, dict):
        return cached

    from market_data_service import external_data_allowed
    if not external_data_allowed():
        return {"available": False, "status": "loading", "reason": "not_cached",
                "symbol": sym, "items": [], "note": "新闻正在后台更新。"}

    out: Dict[str, Any] = {
        "available": False, "symbol": sym, "n": 0, "pos": 0, "neg": 0, "neu": 0,
        "net_score": 0.0, "label": "舆情·无近况", "risk": False,
        "items": [], "source": "massive:news", "reason": "not_loaded",
        "trump": {"mentioned": False, "count": 0, "net_score": 0.0, "items": []},
    }
    try:
        from market_data_service import _massive_get
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        payload = _massive_get("/v2/reference/news", ticker=sym, limit=limit)
        rows = (payload or {}).get("results") or []
        items: List[Dict[str, Any]] = []
        trump_items: List[Dict[str, Any]] = []
        pos = neg = neu = 0
        t_pos = t_neg = 0
        for r in rows:
            if str(r.get("published_utc") or "") < cutoff:
                continue
            sentiment, reasoning = "neutral", None
            for ins in (r.get("insights") or []):
                if str(ins.get("ticker") or "").upper() == sym:
                    sentiment = ins.get("sentiment") or "neutral"
                    reasoning = ins.get("sentiment_reasoning")
                    break
            if sentiment == "positive":
                pos += 1
            elif sentiment == "negative":
                neg += 1
            else:
                neu += 1
            trump = _is_trump(r)
            item = {
                "title": (r.get("title") or "")[:200],
                "description": (r.get("description") or "")[:1600],
                "publisher": ((r.get("publisher") or {}) or {}).get("name"),
                "published_utc": r.get("published_utc"),
                "url": r.get("article_url"),
                "sentiment": sentiment,
                "reasoning": (reasoning or "")[:280] or None,
                "trump": trump,
            }
            items.append(item)
            if trump:
                trump_items.append(item)
                if sentiment == "positive":
                    t_pos += 1
                elif sentiment == "negative":
                    t_neg += 1
        n = pos + neg + neu
        out.update({
            "available": n > 0,
            "n": n, "pos": pos, "neg": neg, "neu": neu,
            "reason": None if n > 0 else "no_recent_news",
            "net_score": round((pos - neg) / n, 4) if n else 0.0,
            "label": _label(n, pos, neg),
            "risk": bool(neg >= 2 and neg > pos),
            "items": items[:12],
            "trump": {
                "mentioned": bool(trump_items),
                "count": len(trump_items),
                "net_score": round((t_pos - t_neg) / len(trump_items), 4) if trump_items else 0.0,
                "items": trump_items[:6],
            },
        })
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 403:
            reason = "forbidden_or_plan_missing"
        elif status == 401:
            reason = "unauthorized"
        elif status:
            reason = f"http_{status}"
        else:
            reason = exc.__class__.__name__
        out["reason"] = reason
        out["status_code"] = status
    cache_set(key, out, ttl_seconds=2 * 3600)
    return out


def _label(n: int, pos: int, neg: int) -> str:
    if n == 0:
        return "舆情·无近况"
    if neg >= 1 and neg >= pos:
        return "舆情·偏负"
    if pos > neg:
        return "舆情·偏正"
    return "舆情·中性"


def estimate_impact(net_score: float, atr_pct: Optional[float], *, trump_net: Optional[float] = None) -> Dict[str, Any]:
    """Transparent heuristic: short-term price perturbation ~ sentiment direction
    scaled by the stock's own daily volatility (ATR%). NOT a trained model.

    A sentiment-laden news day historically moves a stock on the order of its
    daily range; we scale ATR% by the net sentiment in [-1,1]. Trump-tagged news
    skews the magnitude up a notch (mentions are higher-variance, both ways).
    Returns a signed point estimate and a band, all clearly labelled heuristic.
    """
    if atr_pct is None or atr_pct <= 0:
        return {"available": False, "note": "缺少波动率(ATR)，无法估计影响。"}
    score = max(-1.0, min(1.0, float(net_score or 0.0)))
    factor = 1.0
    if trump_net is not None and abs(trump_net) > 0:
        factor = 1.6  # high-profile mention -> wider swing
        score = max(-1.0, min(1.0, 0.5 * score + 0.5 * float(trump_net)))
    point = round(score * atr_pct * factor, 4)               # signed % point estimate
    band = round(abs(atr_pct) * factor, 4)                    # +/- one ATR-ish swing
    return {
        "available": True,
        "point_pct": point,
        "band_pct": band,
        "direction": "偏多" if point > 0.0005 else ("偏空" if point < -0.0005 else "中性"),
        "basis": f"启发式：情绪净值 {score:+.2f} × 日波动(ATR ~{atr_pct*100:.1f}%)" + ("× Trump放大" if factor > 1 else ""),
        "note": "粗略启发式估计，非训练过的新闻→价格模型；仅供参考。",
    }
