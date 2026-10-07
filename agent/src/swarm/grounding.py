"""Pre-fetch market data for symbols mentioned in a swarm's user_vars.

Why this exists
---------------
Swarm workers are LLMs. Without explicit grounding they cheerfully quote
prices from their training data — which is wrong by definition for any
asset that has traded since the model's cutoff. The fix can only be
structural: feed the worker the real recent prices before it starts
reasoning, and tell it those are the only prices it may cite.

What this module does
---------------------
* Scans every value in ``user_vars`` for tokens that match one of the
  data-source-suffixed symbol shapes the loaders already understand
  (``NVDA.US``, ``700.HK``, ``600519.SH``, ``BTC-USDT``, etc.).
* Pulls the last ``DEFAULT_WINDOW_DAYS`` of OHLCV for each detected
  symbol via ``backtest.loaders.registry.resolve_loader`` with
  ``source="auto"``. Failures (delisted ticker, network blip) are
  swallowed per-symbol so they do not poison the whole run.
* Renders a compact markdown block the worker prompt can splice in.

What this module **does not** do
--------------------------------
* It does not match bare tickers (``NVDA`` without ``.US``) — too easy
  to false-positive on common English words. Users supply suffixed
  symbols already in every shipped preset.
* It does not refresh data mid-run. The block is a snapshot taken once
  when the background run starts; long-running swarms will see stale data after
  many minutes, but that is still strictly better than training-data
  prices from a year ago.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta
from typing import Iterable

logger = logging.getLogger(__name__)


# Window of OHLCV bars to fetch per symbol. 30 calendar days yields
# roughly 21 US trading days — enough for a "recent" view without
# bloating the worker prompt.
DEFAULT_WINDOW_DAYS = 30
DEFAULT_MAX_SYMBOLS = 8
MAX_SYMBOLS_ENV = "SWARM_GROUNDING_MAX_SYMBOLS"

# How many of the most-recent rows to render in the worker prompt.
# The full window is still used to compute the min/max line; the table
# is truncated for readability.
PROMPT_TABLE_TAIL = 5

# Symbol patterns understood by the bundled loaders. Anchored on word
# boundaries so substrings of longer text don't trigger.
_SYMBOL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Z]{1,5}\.US\b"),
    re.compile(r"\b\d{3,5}\.HK\b"),
    re.compile(r"\b\d{6}\.(?:SZ|SH|BJ)\b"),
    re.compile(r"\b[A-Z]{2,6}-USDT\b"),
)


def extract_symbols_from_user_vars(user_vars: dict[str, str]) -> list[str]:
    """Return the deduplicated list of symbols mentioned anywhere in *user_vars*.

    Order is preserved from first occurrence so the worker prompt is
    deterministic when nothing else changes.
    """
    seen: dict[str, None] = {}  # ordered set
    for value in user_vars.values():
        if not isinstance(value, str):
            continue
        for pattern in _SYMBOL_PATTERNS:
            for match in pattern.findall(value):
                seen.setdefault(match, None)
    return list(seen.keys())


def max_grounding_symbols() -> int:
    """Return the configured cap for symbols fetched into worker prompts."""
    raw = os.getenv(MAX_SYMBOLS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_SYMBOLS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "grounding: invalid %s=%r, using default %d",
            MAX_SYMBOLS_ENV, raw, DEFAULT_MAX_SYMBOLS,
        )
        return DEFAULT_MAX_SYMBOLS
    return max(1, value)


def fetch_grounding_data(
    symbols: Iterable[str],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    today: date | None = None,
) -> dict[str, list[dict]]:
    """Fetch OHLCV for *symbols* and return a code -> list-of-bars mapping.

    Each bar is a plain dict with ``trade_date`` (ISO string), ``open``,
    ``high``, ``low``, ``close``, ``volume``. Symbols that fail to
    resolve are simply omitted from the result with a logged warning.

    Args:
        symbols: Iterable of suffixed symbols (``NVDA.US`` etc.).
        window_days: Calendar-day lookback. Defaults to
            :data:`DEFAULT_WINDOW_DAYS`.
        today: Override the upper bound (mainly for tests). Defaults to
            ``date.today()``.

    Returns:
        Dict keyed by the *original* symbol string with the bars list as
        value. Empty if no symbols resolve.
    """
    symbols_list = list(symbols)
    if not symbols_list:
        return {}

    end = today or date.today()
    start = end - timedelta(days=window_days)
    start_str = start.isoformat()
    end_str = end.isoformat()

    # Imported lazily so unit tests of the extraction / formatting layer
    # don't have to drag in pandas + the loader graph just to import.
    # ``resolve_loader`` expects a *market* key (``"us_equity"`` etc.), not a
    # raw code; ``_detect_market`` is the function ``runner.py`` already uses
    # to dispatch the same shapes we extract here, so reusing it keeps the
    # routing identical to the rest of the codebase.
    from backtest.loaders.registry import resolve_loader
    from backtest.runner import _detect_market

    out: dict[str, list[dict]] = {}
    for code in symbols_list:
        try:
            market = _detect_market(code)
            loader = resolve_loader(market)  # already a ready-to-use instance
            df_map = loader.fetch([code], start_str, end_str, interval="1D")
        except Exception as exc:  # pragma: no cover — depends on network
            logger.warning(
                "grounding: failed to fetch %s — %s", code, exc, exc_info=False
            )
            continue
        df = df_map.get(code)
        if df is None or df.empty:
            logger.info("grounding: no data returned for %s", code)
            continue
        rows: list[dict] = []
        for ts, row in df.iterrows():
            rows.append({
                "trade_date": getattr(ts, "isoformat", lambda: str(ts))(),
                "open": float(row.get("open", 0.0)),
                "high": float(row.get("high", 0.0)),
                "low": float(row.get("low", 0.0)),
                "close": float(row.get("close", 0.0)),
                "volume": float(row.get("volume", 0.0)),
            })
        if rows:
            out[code] = rows
    return out


def format_grounding_block(grounding: dict[str, list[dict]]) -> str:
    """Render *grounding* as a markdown block ready to splice into a prompt.

    Returns the empty string if no symbol has any data — callers can use
    that as a falsy guard so the section is omitted entirely instead of
    rendering an empty heading.
    """
    if not grounding:
        return ""

    sections: list[str] = []
    for code, rows in grounding.items():
        if not rows:
            continue
        first_date = rows[0]["trade_date"][:10]
        last_date = rows[-1]["trade_date"][:10]
        closes = [row["close"] for row in rows]
        window_low = min(closes)
        window_high = max(closes)
        last_close = closes[-1]

        lines = [
            f"### {code}  (window {first_date} → {last_date})",
            "",
            "| Date | Close | Volume |",
            "| --- | ---: | ---: |",
        ]
        for row in rows[-PROMPT_TABLE_TAIL:]:
            lines.append(
                f"| {row['trade_date'][:10]} | {row['close']:.2f} "
                f"| {int(row['volume']):,} |"
            )
        lines.append("")
        lines.append(
            f"**Latest close:** {last_close:.2f} ({last_date})  "
            f"**Window range:** {window_low:.2f} – {window_high:.2f}"
        )
        sections.append("\n".join(lines))

    if not sections:
        return ""

    header = (
        "## Ground Truth — Recent Market Data\n\n"
        "**These are the authoritative current prices for this run.** Do NOT "
        "cite prices, valuations, multiples, or returns from your training "
        "data — markets have moved. If you need a price outside this window, "
        "call `get_market_data` for the relevant range. When you state a "
        "price, cite the date from this table."
    )
    return header + "\n\n" + "\n\n".join(sections)
