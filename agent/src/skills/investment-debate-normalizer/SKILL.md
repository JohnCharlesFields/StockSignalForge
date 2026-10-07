---
name: investment-debate-normalizer
description: Normalize multi-agent investment committee outputs into a compact, auditable JSON decision card with bull case, bear case, risk review, confidence, invalidation, and position guidance.
---

# Investment Debate Normalizer

Use this skill when multiple analysts, debate agents, or external engines produce long narrative reports and the system needs one comparable decision record.

## Normalization Target

Produce a JSON-compatible decision card:

```json
{
  "symbol": "AAPL",
  "source_engine": "swarm|TradingAgents|ai-hedge-fund",
  "decision": "buy|watch|reject|avoid",
  "confidence": 0.0,
  "bull_case": ["..."],
  "bear_case": ["..."],
  "quality_review": {
    "moat": "...",
    "cash_flow": "...",
    "management": "...",
    "valuation": "...",
    "safety_margin": "..."
  },
  "risk_review": {
    "main_risks": ["..."],
    "tail_risk": "...",
    "liquidity": "...",
    "position_limit": "..."
  },
  "execution_plan": {
    "entry": "...",
    "add_trigger": "...",
    "trim_trigger": "...",
    "stop": "...",
    "review_date": "..."
  },
  "evidence_gaps": ["..."]
}
```

## Rules

- Do not average all agents mechanically. Weight evidence quality.
- Separate "model probability" from "real-world win rate".
- If data freshness is unknown, mark it in `evidence_gaps`.
- If agents disagree, preserve the disagreement rather than hiding it.
- The final `decision` must be conservative when evidence is thin.
- `confidence` is a research confidence score, not a guaranteed probability.

## Decision Mapping

- `buy`: strong evidence, risk is measurable, entry and invalidation are explicit.
- `watch`: thesis exists but timing, valuation, liquidity, or event confirmation is missing.
- `reject`: weak thesis, poor quality, or unfavorable risk/reward.
- `avoid`: event/liquidity/tail risk makes the setup inappropriate even if upside exists.
