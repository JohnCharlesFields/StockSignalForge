"""Point-in-time snapshot persistence for pre-earnings signals.

This module stores daily snapshots of pre-earnings signal data to enable
proper backtesting without time-travel bias.  Each snapshot captures the
data that was visible at the time of signal generation.

Key design principles:
1. Snapshots are append-only (never overwrite history).
2. Raw API responses are preserved in raw_payload for audit.
3. Missing fields are stored as null (never fabricated).
4. Data quality warnings are persisted alongside the signal.
5. Backtesting must only read historical snapshots, never call current APIs.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB_LOCK = threading.Lock()
_DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "pre_earnings_snapshots.db"


def _ensure_db(db_path: Path) -> None:
    """Create the database and table if they don't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pre_earnings_snapshots (
                id TEXT PRIMARY KEY,
                snapshot_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_version TEXT,
                target_report_date TEXT,
                report_session TEXT,
                trading_days_to_report INTEGER,
                date_confidence REAL,
                final_score REAL,
                raw_score REAL,
                analyst_revision_score REAL,
                revenue_revision_score REAL,
                peer_inference_score REAL,
                relative_momentum_score REAL,
                data_quality_factor REAL,
                valuation_factor REAL,
                volatility_factor REAL,
                concentration_factor REAL,
                eps_revision_up_30d INTEGER,
                eps_revision_down_30d INTEGER,
                eps_change_30d REAL,
                revenue_change_30d REAL,
                peer_events TEXT,
                volume_ratio_20d REAL,
                stock_return_10d REAL,
                benchmark_return_10d REAL,
                relative_return_10d REAL,
                entry_rule TEXT,
                entry_zone_low REAL,
                entry_zone_high REAL,
                entry_zone_current REAL,
                scheduled_exit TEXT,
                exit_rule TEXT,
                signal_stop REAL,
                stop_trigger TEXT,
                execution_rule TEXT,
                atr_14 REAL,
                max_holding_days INTEGER,
                max_position_weight REAL,
                data_quality_score REAL,
                missing_fields TEXT,
                warnings TEXT,
                raw_payload TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(snapshot_date, symbol, signal_type)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_symbol
            ON pre_earnings_snapshots(symbol)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_date
            ON pre_earnings_snapshots(snapshot_date)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_target_date
            ON pre_earnings_snapshots(target_report_date)
        """)
        conn.commit()


class PreEarningsSnapshotRepository:
    """Repository for persisting pre-earnings signal snapshots.

    Usage:
        repo = PreEarningsSnapshotRepository()
        repo.save_snapshot(signal_dict)
        snapshots = repo.list_snapshots(symbol="NVDA")
        latest = repo.get_latest_snapshot("NVDA")
    """

    def __init__(self, db_path: str | Path | None = None):
        """Initialize the repository.

        Args:
            db_path: Path to SQLite database file.  Defaults to
                     agent/data/pre_earnings_snapshots.db
        """
        self._db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        _ensure_db(self._db_path)

    def save_snapshot(self, signal: dict[str, Any]) -> str:
        """Save a signal snapshot.  Never overwrites existing history.

        Args:
            signal: Complete signal dict from calculate_pre_earnings_expectation_revision_signal().

        Returns:
            The snapshot ID (UUID).
        """
        snapshot_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Extract fields from signal
        symbol = signal.get("symbol", "")
        snapshot_date = signal.get("as_of", now)[:10]  # YYYY-MM-DD
        report = signal.get("report", {})
        score = signal.get("score", {})
        evidence = signal.get("evidence", {})
        execution = signal.get("execution", {})
        risk = signal.get("risk_control", {})
        dq = signal.get("data_quality", {})

        conn = None
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=10)
            conn.execute("""
                INSERT OR IGNORE INTO pre_earnings_snapshots (
                    id, snapshot_date, symbol, signal_type, signal_version,
                    target_report_date, report_session, trading_days_to_report,
                    date_confidence, final_score, raw_score,
                    analyst_revision_score, revenue_revision_score,
                    peer_inference_score, relative_momentum_score,
                    data_quality_factor, valuation_factor,
                    volatility_factor, concentration_factor,
                    eps_revision_up_30d, eps_revision_down_30d,
                    eps_change_30d, revenue_change_30d,
                    peer_events, volume_ratio_20d,
                    stock_return_10d, benchmark_return_10d, relative_return_10d,
                    entry_rule, entry_zone_low, entry_zone_high, entry_zone_current,
                    scheduled_exit, exit_rule,
                    signal_stop, stop_trigger, execution_rule,
                    atr_14, max_holding_days, max_position_weight,
                    data_quality_score, missing_fields, warnings,
                    raw_payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot_id,
                snapshot_date,
                symbol,
                signal.get("signal_type", "pre_earnings_expectation_revision"),
                signal.get("signal_version", "v0.2.0"),
                report.get("target_date"),
                report.get("session", "UNKNOWN"),
                report.get("trading_days_to_report"),
                report.get("date_confidence"),
                score.get("final"),
                score.get("raw"),
                score.get("components", {}).get("analyst_revision"),
                score.get("components", {}).get("revenue_revision"),
                score.get("components", {}).get("peer_inference"),
                score.get("components", {}).get("relative_momentum"),
                score.get("risk_adjustments", {}).get("data_quality_factor"),
                score.get("risk_adjustments", {}).get("valuation_factor"),
                score.get("risk_adjustments", {}).get("volatility_factor"),
                score.get("risk_adjustments", {}).get("concentration_factor"),
                evidence.get("eps_revision_up_30d"),
                evidence.get("eps_revision_down_30d"),
                evidence.get("eps_change_30d"),
                evidence.get("revenue_change_30d"),
                json.dumps(evidence.get("peer_events", []), ensure_ascii=False),
                evidence.get("volume_ratio_20d"),
                evidence.get("stock_return_10d"),
                evidence.get("benchmark_return_10d"),
                evidence.get("relative_return_10d"),
                execution.get("entry_rule"),
                execution.get("entry_zone", {}).get("low"),
                execution.get("entry_zone", {}).get("high"),
                execution.get("entry_zone", {}).get("current"),
                execution.get("scheduled_exit"),
                execution.get("exit_rule"),
                risk.get("signal_stop"),
                risk.get("stop_trigger"),
                risk.get("execution_rule"),
                risk.get("atr_14"),
                risk.get("max_holding_days"),
                risk.get("max_position_weight"),
                dq.get("score"),
                json.dumps(dq.get("missing_fields", []), ensure_ascii=False),
                json.dumps(dq.get("warnings", []), ensure_ascii=False),
                json.dumps(signal, ensure_ascii=False),
                now,
            ))
            conn.commit()
        except sqlite3.IntegrityError:
            # Duplicate snapshot (same date + symbol + type) - skip silently
            pass
        finally:
            if conn:
                conn.close()

        return snapshot_id

    def get_snapshot(self, symbol: str, snapshot_date: str) -> dict[str, Any] | None:
        """Get a specific snapshot by symbol and date.

        Args:
            symbol: Stock ticker symbol.
            snapshot_date: Snapshot date (YYYY-MM-DD).

        Returns:
            Snapshot dict or None if not found.
        """
        with _DB_LOCK:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM pre_earnings_snapshots WHERE symbol = ? AND snapshot_date = ?",
                    (symbol.upper(), snapshot_date)
                ).fetchone()

        if row is None:
            return None

        return self._row_to_dict(row)

    def get_latest_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """Get the most recent snapshot for a symbol.

        Args:
            symbol: Stock ticker symbol.

        Returns:
            Latest snapshot dict or None if not found.
        """
        with _DB_LOCK:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM pre_earnings_snapshots WHERE symbol = ? ORDER BY snapshot_date DESC LIMIT 1",
                    (symbol.upper(),)
                ).fetchone()

        if row is None:
            return None

        return self._row_to_dict(row)

    def list_snapshots(
        self,
        symbol: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        signal_type: str | None = None,
        min_score: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List snapshots with optional filters.

        Args:
            symbol: Filter by symbol (optional).
            start_date: Filter by snapshot_date >= start_date (optional).
            end_date: Filter by snapshot_date <= end_date (optional).
            signal_type: Filter by signal_type (optional).
            min_score: Filter by final_score >= min_score (optional).
            limit: Maximum number of results (default: 100).

        Returns:
            List of snapshot dicts.
        """
        query = "SELECT * FROM pre_earnings_snapshots WHERE 1=1"
        params: list[Any] = []

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol.upper())
        if start_date:
            query += " AND snapshot_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND snapshot_date <= ?"
            params.append(end_date)
        if signal_type:
            query += " AND signal_type = ?"
            params.append(signal_type)
        if min_score is not None:
            query += " AND final_score >= ?"
            params.append(min_score)

        query += " ORDER BY snapshot_date DESC LIMIT ?"
        params.append(limit)

        with _DB_LOCK:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query, params).fetchall()

        return [self._row_to_dict(row) for row in rows]

    def get_snapshot_count(self, symbol: str | None = None) -> int:
        """Get the count of snapshots, optionally filtered by symbol.

        Args:
            symbol: Filter by symbol (optional).

        Returns:
            Count of snapshots.
        """
        if symbol:
            query = "SELECT COUNT(*) FROM pre_earnings_snapshots WHERE symbol = ?"
            params = (symbol.upper(),)
        else:
            query = "SELECT COUNT(*) FROM pre_earnings_snapshots"
            params = ()

        with _DB_LOCK:
            with sqlite3.connect(str(self._db_path)) as conn:
                return conn.execute(query, params).fetchone()[0]

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a SQLite row to a dict with proper type handling."""
        d = dict(row)

        # Parse JSON fields
        for field in ("peer_events", "missing_fields", "warnings"):
            if d.get(field) and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except json.JSONDecodeError:
                    d[field] = []

        # Parse raw_payload
        if d.get("raw_payload") and isinstance(d["raw_payload"], str):
            try:
                d["raw_payload"] = json.loads(d["raw_payload"])
            except json.JSONDecodeError:
                d["raw_payload"] = None

        return d
