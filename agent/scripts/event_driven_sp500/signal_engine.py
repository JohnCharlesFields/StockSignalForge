"""Event-driven signal engine for Vibe-Trading backtests.

The engine intentionally reads event data from CSV files instead of fetching
news during a backtest. This keeps the data layer auditable and avoids
look-ahead bias caused by querying today's web during historical runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd


EVENT_TYPE_WEIGHTS = {
    "earnings": 1.15,
    "guidance": 1.25,
    "analyst_rating": 0.75,
    "product": 0.85,
    "regulatory": 1.05,
    "legal": 1.10,
    "mna": 1.20,
    "buyback": 0.95,
    "macro": 0.55,
    "supply_chain": 0.75,
    "sentiment": 0.45,
    "technical_break": 0.65,
    "other": 0.50,
}


class SignalEngine:
    """Combine event impact, macro diffusion, and price confirmation.

    Output convention follows the built-in backtest engine:
    1 = long, -1 = short / reduce, 0 = flat.
    """

    def __init__(
        self,
        event_file: str = "data/events.csv",
        fallback_event_file: str = "data/event_data.csv",
        half_life_days: float = 3.0,
        event_lookback_days: int = 15,
        min_event_score: float = 0.15,
        entry_threshold: float = 0.35,
        exit_threshold: float = 0.08,
        negative_threshold_abs: float = 0.30,
        max_holding_days: int = 7,
        event_weight: float = 0.62,
        technical_weight: float = 0.28,
        macro_weight: float = 0.10,
    ):
        self.event_file = event_file
        self.fallback_event_file = fallback_event_file
        self.half_life_days = half_life_days
        self.event_lookback_days = event_lookback_days
        self.min_event_score = min_event_score
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.negative_threshold = -abs(negative_threshold_abs)
        self.max_holding_days = max_holding_days
        self.event_weight = event_weight
        self.technical_weight = technical_weight
        self.macro_weight = macro_weight

    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        events = self._load_events()
        result = {}
        macro_events = self._macro_events(events)

        for code, df in data_map.items():
            symbol = self._normalize_symbol(code)
            events_one = events[events["symbol"].map(self._normalize_symbol) == symbol]
            signal_score = self._score_series(df, events_one, macro_events)
            raw = pd.Series(0, index=df.index, dtype=int)
            raw[signal_score >= self.entry_threshold] = 1
            raw[signal_score <= self.negative_threshold] = -1
            result[code] = self._apply_exit_rules(raw, signal_score)

        return result

    def _load_events(self) -> pd.DataFrame:
        run_dir = Path(__file__).resolve().parents[1]
        path = Path(self.event_file)
        if not path.is_absolute():
            path = run_dir / path
        if not path.exists():
            path = Path(self.fallback_event_file)
            if not path.is_absolute():
                path = run_dir / path
        if not path.exists():
            return self._empty_events()

        events = pd.read_csv(path)
        if "symbol" not in events.columns:
            events["symbol"] = ""
        if "event_type" not in events.columns:
            events["event_type"] = "other"
        if "score" not in events.columns and "sentiment_score" in events.columns:
            events["score"] = events["sentiment_score"]
        if "score" not in events.columns:
            events["score"] = 0.0
        if "impact_score" not in events.columns:
            events["impact_score"] = events["score"].abs()
        if "date" not in events.columns:
            events["date"] = events.get("effective_at", events.get("published_at", ""))

        events["date"] = pd.to_datetime(events["date"], errors="coerce").dt.normalize()
        if "effective_at" in events.columns:
            effective = pd.to_datetime(events["effective_at"], errors="coerce").dt.normalize()
            events["date"] = effective.fillna(events["date"])
        events["score"] = pd.to_numeric(events["score"], errors="coerce").fillna(0.0)
        events["impact_score"] = pd.to_numeric(
            events["impact_score"], errors="coerce"
        ).fillna(events["score"].abs())
        events["event_type"] = events["event_type"].fillna("other").astype(str)
        events = events.dropna(subset=["date"])
        return events

    def _empty_events(self) -> pd.DataFrame:
        return pd.DataFrame(
            columns=["date", "symbol", "event_type", "score", "impact_score"]
        )

    def _macro_events(self, events: pd.DataFrame) -> pd.DataFrame:
        if events.empty:
            return events
        is_macro = events["event_type"].astype(str).str.lower().eq("macro")
        return events[is_macro | events["symbol"].astype(str).str.upper().eq("MACRO")]

    def _score_series(
        self,
        df: pd.DataFrame,
        events_one: pd.DataFrame,
        macro_events: pd.DataFrame,
    ) -> pd.Series:
        close = df["close"].astype(float)
        tech = self._technical_confirmation(df)
        event_signal = pd.Series(0.0, index=df.index)
        macro_signal = pd.Series(0.0, index=df.index)

        if not events_one.empty:
            event_signal = self._event_signal(events_one, df.index)
        if not macro_events.empty:
            macro_signal = self._event_signal(macro_events, df.index)

        score = (
            self.event_weight * event_signal
            + self.technical_weight * tech
            + self.macro_weight * macro_signal
        )
        score = score.replace([np.inf, -np.inf], np.nan).fillna(0.0)
        return score.clip(-1.0, 1.0).reindex(close.index).fillna(0.0)

    def _event_signal(self, events: pd.DataFrame, index: pd.Index) -> pd.Series:
        signal = pd.Series(0.0, index=index)
        if events.empty:
            return signal

        dates = pd.to_datetime(index).normalize()
        for event in events.itertuples(index=False):
            event_date = getattr(event, "date")
            event_score = float(getattr(event, "score", 0.0))
            impact = float(getattr(event, "impact_score", abs(event_score)))
            if abs(event_score) < self.min_event_score:
                continue
            event_type = str(getattr(event, "event_type", "other")).lower()
            weight = EVENT_TYPE_WEIGHTS.get(event_type, EVENT_TYPE_WEIGHTS["other"])
            age_days = (dates - event_date).days
            mask = (age_days >= 0) & (age_days <= self.event_lookback_days)
            if not mask.any():
                continue
            decay = np.exp(-np.log(2.0) * age_days[mask] / self.half_life_days)
            contribution = np.sign(event_score) * min(abs(event_score) * impact * weight, 1.5)
            signal.loc[mask] = signal.loc[mask] + contribution * decay

        return signal.clip(-1.0, 1.0)

    def _technical_confirmation(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"].astype(float)
        volume = df.get("volume", pd.Series(0.0, index=df.index)).astype(float)

        ret_5 = close.pct_change(5)
        ret_20 = close.pct_change(20)
        vol_ratio = volume / volume.rolling(20, min_periods=5).mean()
        vol_score = ((vol_ratio - 1.0) / 2.0).clip(-0.5, 0.8)
        trend_score = (ret_5 * 4.0 + ret_20 * 1.5).clip(-0.8, 0.8)

        score = 0.65 * trend_score + 0.35 * vol_score
        return score.replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(-1.0, 1.0)

    def _apply_exit_rules(self, raw: pd.Series, score: pd.Series) -> pd.Series:
        final = pd.Series(0, index=raw.index, dtype=int)
        in_position = False
        entry_i = 0

        for i, dt in enumerate(raw.index):
            current_signal = int(raw.iloc[i])
            current_score = float(score.iloc[i])
            if not in_position and current_signal == 1:
                in_position = True
                entry_i = i
                final.loc[dt] = 1
                continue
            if in_position:
                held_days = i - entry_i
                should_exit = (
                    current_signal == -1
                    or abs(current_score) <= self.exit_threshold
                    or held_days >= self.max_holding_days
                )
                final.loc[dt] = 0 if should_exit else 1
                if should_exit:
                    in_position = False
            elif current_signal == -1:
                final.loc[dt] = -1

        return final

    def _normalize_symbol(self, code: str) -> str:
        return str(code).upper().replace(".US", "").replace("-", ".")
