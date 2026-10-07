"""Cross-asset correlation matrix computation.

Computes pairwise Pearson or Spearman correlation of daily returns
over a configurable lookback window. Used by the /correlation API endpoint.
"""

from __future__ import annotations

from typing import Dict, Literal

import pandas as pd
import numpy as np
from scipy.stats import spearmanr


def infer_market(code: str) -> str:
    """Infer market key from a ticker symbol."""
    code_upper = code.upper()
    crypto_suffixes = ("USDT", "BTC", "ETH", "BNB", "SOL", "ADA", "DOGE")
    if any(code_upper.endswith(s) for s in crypto_suffixes) or "/" in code:
        return "crypto"
    # Check .HK suffix FIRST so leading-zero tickers like 0700.HK / 0005.HK
    # are correctly classified before the A-share prefix checks
    if code_upper.endswith(".HK"):
        return "hk_equity"
    if code_upper.startswith(("6", "000", "001", "002")):
        return "a_share"
    if code_upper.startswith(("0", "399")):
        return "a_share"
    if code_upper.startswith(("0", "1", "2", "3", "4")):
        return "hk_equity"
    return "us_equity"


def _rolling_correlation_matrix(
    price_series: Dict[str, pd.DataFrame],
    window: int,
    method: Literal["pearson", "spearman"],
) -> tuple[list[str], list[list[float]]]:
    """Compute correlation matrix for multiple price series.

    Args:
        price_series: Mapping of asset code -> DataFrame with a ``close`` column.
        window: Rolling window size in days.
        method: "pearson" or "spearman".

    Returns:
        (labels, matrix) where labels is the sorted list of codes and matrix
        is a symmetric NxN matrix of correlation coefficients.
    """
    if not price_series:
        return [], []

    codes = sorted(price_series.keys())

    # Build a aligned returns DataFrame (row index = date)
    returns_frames = []
    closes = {}
    for code, df in price_series.items():
        if df.empty:
            raise ValueError(f"Price series for '{code}' is empty")
        if "close" not in df.columns and "close" not in df.index.names:
            raise ValueError(f"No 'close' column in price series for '{code}'")
        # Support both column-based and index-based trade_date
        if "trade_date" in df.index.names and "trade_date" not in df.columns:
            ts = df["close"]
        else:
            ts = df.set_index("trade_date")["close"]
        closes[code] = ts.sort_index()

    for code in codes:
        ts = closes[code]
        rets = ts.pct_change().dropna()
        rets.name = code
        returns_frames.append(rets)

    # Align all series to a common index (inner join)
    aligned = pd.concat(returns_frames, axis=1).dropna()
    if aligned.empty:
        raise ValueError("No overlapping return data between assets")

    # Apply the trailing window — only use the last `window` rows of aligned data
    if len(aligned) > window:
        aligned = aligned.iloc[-window:]

    n = len(aligned)
    if n < 2:
        raise ValueError("Not enough data points to compute correlation")

    labels = codes
    n_assets = len(labels)
    matrix = [[1.0] * n_assets for _ in range(n_assets)]

    for i in range(n_assets):
        for j in range(i + 1, n_assets):
            xi = aligned.iloc[:, i].values
            xj = aligned.iloc[:, j].values
            if method == "spearman":
                corr, _ = spearmanr(xi, xj)
            else:
                corr = np.corrcoef(xi, xj)[0, 1]
            if np.isnan(corr):
                corr = 0.0
            value = float(round(corr, 4))
            matrix[i][j] = value
            matrix[j][i] = value

    return labels, matrix


def compute_correlation_matrix(
    codes: list[str],
    days: int = 90,
    method: Literal["pearson", "spearman"] = "pearson",
) -> Dict[str, object]:
    """Fetch price data and compute correlation matrix for a list of assets.

    Args:
        codes: List of asset codes (e.g. ["BTC-USDT", "ETH-USDT", "SPY"]).
        days: Lookback window in days (default 90).
        method: Correlation method.

    Returns:
        Dict with keys: labels, matrix, window, method.
    """
    from datetime import datetime, timedelta

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days + 60)).strftime("%Y-%m-%d")

    # Import here to avoid circular imports.  Correlation must try the whole
    # market chain at fetch time because an available source can still be
    # rate-limited or return no rows for a particular ticker.
    from backtest.loaders.registry import FALLBACK_CHAINS, LOADER_REGISTRY, _ensure_registered

    price_series: Dict[str, pd.DataFrame] = {}
    failed_sources: Dict[str, list[str]] = {}

    _ensure_registered()

    for code in codes:
        market = infer_market(code)
        chain = FALLBACK_CHAINS.get(market, [])
        failures: list[str] = []
        for source_name in chain:
            loader_cls = LOADER_REGISTRY.get(source_name)
            if loader_cls is None:
                continue
            try:
                loader = loader_cls()
                if not loader.is_available():
                    failures.append(f"{source_name}: unavailable")
                    continue
                result = loader.fetch(
                    codes=[code],
                    start_date=start_date,
                    end_date=end_date,
                    interval="1D",
                    fields=["trade_date", "open", "high", "low", "close", "volume"],
                )
                frame = result.get(code)
                if frame is not None and not frame.empty:
                    price_series[code] = frame
                    break
                failures.append(f"{source_name}: no rows")
            except Exception as exc:
                failures.append(f"{source_name}: {exc}")
        if code not in price_series:
            failed_sources[code] = failures

    if len(price_series) < 2:
        detail = "; ".join(
            f"{code} -> {', '.join(errors) if errors else 'no matching loader'}"
            for code, errors in failed_sources.items()
        )
        raise ValueError(
            f"Could not fetch price data for at least 2 assets. "
            f"Fetched: {list(price_series.keys())}. {detail}"
        )

    labels, matrix = _rolling_correlation_matrix(price_series, days, method)
    return {
        "labels": labels,
        "matrix": matrix,
        "window": days,
        "method": method,
    }
