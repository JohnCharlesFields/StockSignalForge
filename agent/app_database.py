"""Small SQLite persistence layer for the local research cockpit.

SQLite runs inside the same application container.  The database file lives
under /app/agent/data_cache by default, which is already a Docker named volume
in the compose file, so no separate database service is required.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit


AGENT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = AGENT_DIR / "data_cache" / "easymoneysniper.sqlite3"
DB_PATH = Path(os.environ.get("EASYMONEYSNIPER_DB_PATH", DEFAULT_DB_PATH))
_DB_LOCK = threading.RLock()
_INITIALIZED = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_load(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def ensure_database() -> Path:
    global _INITIALIZED
    if _INITIALIZED and DB_PATH.exists():
        return DB_PATH
    with _DB_LOCK:
        if _INITIALIZED and DB_PATH.exists():
            return DB_PATH
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at REAL
                );
                CREATE TABLE IF NOT EXISTS reference_evidence_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reference_evidence_symbol_date
                ON reference_evidence_snapshots(symbol, kind, as_of DESC, fetched_at DESC);

                CREATE TABLE IF NOT EXISTS overnight_alpha_cache (
                    cache_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    benchmark TEXT NOT NULL,
                    universe_key TEXT NOT NULL,
                    signal_profile TEXT NOT NULL,
                    period TEXT NOT NULL,
                    min_edge REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    sample_count INTEGER,
                    alpha_win_rate REAL,
                    mean_beta_adjusted_alpha REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_overnight_alpha_symbol
                ON overnight_alpha_cache(symbol, benchmark, universe_key);

                CREATE INDEX IF NOT EXISTS idx_overnight_alpha_expires
                ON overnight_alpha_cache(expires_at);

                CREATE TABLE IF NOT EXISTS home_dashboard_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    generated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_home_dashboard_generated
                ON home_dashboard_snapshots(generated_at DESC);

                CREATE TABLE IF NOT EXISTS symbol_directory (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    name_upper TEXT NOT NULL DEFAULT '',
                    exchange TEXT NOT NULL DEFAULT '',
                    exchange_display TEXT NOT NULL DEFAULT '',
                    quote_type TEXT NOT NULL DEFAULT 'EQUITY',
                    source TEXT NOT NULL DEFAULT 'seed',
                    has_history INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_symbol_directory_name
                ON symbol_directory(name_upper);

                CREATE TABLE IF NOT EXISTS symbol_directory_aliases (
                    symbol TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    alias_upper TEXT NOT NULL,
                    alias_kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, alias_upper, source),
                    FOREIGN KEY(symbol) REFERENCES symbol_directory(symbol) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_symbol_directory_alias
                ON symbol_directory_aliases(alias_upper, symbol);

                CREATE TABLE IF NOT EXISTS signal_calibration (
                    calibration_id TEXT PRIMARY KEY,
                    signal_type TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    universe_set TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'replay',
                    generated_at TEXT NOT NULL,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    cluster_count INTEGER NOT NULL DEFAULT 0,
                    cost_bps REAL NOT NULL DEFAULT 0,
                    curve_json TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_signal_calibration_active
                ON signal_calibration(signal_type, horizon_days, is_active);

                CREATE TABLE IF NOT EXISTS signal_events (
                    event_id TEXT PRIMARY KEY,
                    signal_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    as_of_date TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    score REAL NOT NULL,
                    ref_price REAL,
                    run_id TEXT,
                    created_at TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    forward_return REAL,
                    excess_return REAL,
                    baseline_return REAL,
                    resolved_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_signal_events_pending
                ON signal_events(resolved, as_of_date);

                CREATE INDEX IF NOT EXISTS idx_signal_events_symbol
                ON signal_events(symbol);

                CREATE TABLE IF NOT EXISTS iv_history (
                    symbol TEXT NOT NULL,
                    as_of_date TEXT NOT NULL,
                    atm_iv REAL,
                    iv_hv REAL,
                    hv REAL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, as_of_date)
                );

                CREATE INDEX IF NOT EXISTS idx_iv_history_symbol
                ON iv_history(symbol, as_of_date DESC);

                CREATE TABLE IF NOT EXISTS portfolio_holdings (
                    symbol TEXT PRIMARY KEY,
                    shares REAL NOT NULL DEFAULT 0,
                    avg_cost REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    opened_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS portfolio_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    available_cash REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_watchlist (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_user_watchlist_enabled
                ON user_watchlist(enabled, symbol);

                -- Forward-verification ledger: every recommendation the system
                -- makes is logged BEFORE the outcome (walk-forward / no look-ahead),
                -- then resolved at horizon and scored vs the predicted win-rate.
                CREATE TABLE IF NOT EXISTS predictions (
                    prediction_id TEXT PRIMARY KEY,
                    as_of_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    rank INTEGER,
                    signal_type TEXT NOT NULL DEFAULT 'pullback_hv',
                    calibrated_prob REAL,
                    entry_ref_price REAL,
                    horizon_days INTEGER NOT NULL,
                    curve_source TEXT,
                    is_board_pick INTEGER NOT NULL DEFAULT 1,
                    market_liquid_rs_top40 INTEGER NOT NULL DEFAULT 0,
                    stock_stronger_than_industry INTEGER NOT NULL DEFAULT 0,
                    playbook_score REAL,
                    market_rs_score REAL,
                    playbook_tags TEXT,
                    -- 'live' = real walk-forward (logged at the daily batch, the
                    -- honest forward record). 'backfill' = as-of historical
                    -- replay to seed the ledger; in-sample vs the active curve,
                    -- kept separate so it never contaminates the live track.
                    mode TEXT NOT NULL DEFAULT 'live',
                    created_at TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    exit_price REAL,
                    forward_return REAL,
                    baseline_return REAL,
                    excess_return REAL,
                    net_excess REAL,
                    win INTEGER,
                    resolved_at TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_predictions_unique
                ON predictions(as_of_date, symbol, horizon_days, signal_type);

                CREATE INDEX IF NOT EXISTS idx_predictions_pending
                ON predictions(resolved, as_of_date);

                -- Immutable stock-direction shadow predictions for the long-option
                -- board. They never feed the existing pullback_hv calibration.
                CREATE TABLE IF NOT EXISTS long_option_direction_predictions (
                    prediction_id TEXT PRIMARY KEY,
                    signal_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    model_version TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    source TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    snapshot_generated_at TEXT NOT NULL,
                    stock_price REAL,
                    net_consensus REAL,
                    features_json TEXT NOT NULL,
                    resolution_state TEXT NOT NULL DEFAULT 'pending',
                    resolved_at TEXT,
                    entry_date TEXT,
                    exit_date TEXT,
                    entry_open REAL,
                    exit_close REAL,
                    underlying_return REAL,
                    signed_net REAL,
                    signed_spy_excess REAL,
                    resolution_note TEXT,
                    UNIQUE(signal_date, symbol, horizon_days, model_version)
                );

                CREATE INDEX IF NOT EXISTS idx_long_option_direction_pending
                ON long_option_direction_predictions(resolution_state, signal_date);

                CREATE INDEX IF NOT EXISTS idx_long_option_direction_scorecard
                ON long_option_direction_predictions(horizon_days, mode, resolution_state);

                -- Daily candidate slices for the pullback-buying board. This
                -- stores every scanned candidate, while top-N remains only a
                -- display/board-pick flag. It lets the UI monitor multi-day
                -- signal lifecycles without recomputing a scan on page open.
                CREATE TABLE IF NOT EXISTS priority_candidate_slices (
                    slice_id TEXT PRIMARY KEY,
                    as_of_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    candidate_rank INTEGER,
                    is_board_pick INTEGER NOT NULL DEFAULT 0,
                    horizon_days INTEGER NOT NULL DEFAULT 0,
                    signal_type TEXT NOT NULL DEFAULT 'pullback_hv',
                    current_price REAL,
                    calibrated_prob REAL,
                    priority_score REAL,
                    relative_strength_20d REAL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(as_of_date, symbol, signal_type)
                );

                CREATE INDEX IF NOT EXISTS idx_priority_slices_date_rank
                ON priority_candidate_slices(as_of_date DESC, candidate_rank);

                CREATE INDEX IF NOT EXISTS idx_priority_slices_symbol_date
                ON priority_candidate_slices(symbol, as_of_date DESC);
                """
            )
            # Backward-compatible column migrations (CREATE TABLE IF NOT EXISTS
            # never adds columns to a pre-existing table).
            _ensure_column(conn, "predictions", "mode", "TEXT NOT NULL DEFAULT 'live'")
            _ensure_column(conn, "predictions", "beta_adjusted_alpha", "REAL")
            _ensure_column(conn, "predictions", "market_liquid_rs_top40", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "predictions", "stock_stronger_than_industry", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "predictions", "playbook_score", "REAL")
            _ensure_column(conn, "predictions", "market_rs_score", "REAL")
            _ensure_column(conn, "predictions", "playbook_tags", "TEXT")
            _ensure_column(conn, "predictions", "evaluation_version", "TEXT NOT NULL DEFAULT 'legacy'")
            _ensure_column(conn, "predictions", "forecast_json", "TEXT")
            _ensure_column(conn, "predictions", "entry_date", "TEXT")
            _ensure_column(conn, "predictions", "exit_date", "TEXT")
            _ensure_column(conn, "predictions", "net_return", "REAL")
            conn.commit()
        _INITIALIZED = True
    return DB_PATH


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add ``column`` to ``table`` if an older DB predates it. No-op otherwise."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    ensure_database()
    with _connect() as conn:
        yield conn


def cache_get(cache_key: str) -> Optional[Any]:
    ensure_database()
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json, expires_at FROM kv_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        expires_at = row["expires_at"]
        if expires_at is not None and float(expires_at) <= now:
            conn.execute("DELETE FROM kv_cache WHERE cache_key = ?", (cache_key,))
            conn.commit()
            return None
        return _json_load(row["payload_json"])


def cache_set(cache_key: str, payload: Any, ttl_seconds: int | None = None) -> None:
    ensure_database()
    now = time.time()
    stamp = _utc_now()
    expires_at = now + ttl_seconds if ttl_seconds and ttl_seconds > 0 else None
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO kv_cache(cache_key, payload_json, created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at
            """,
            (cache_key, _json_dump(payload), stamp, stamp, expires_at),
        )
        conn.commit()


def reference_evidence_append(symbol: str, kind: str, as_of: str, payload: dict[str, Any]) -> str:
    """Keep the first observation of each content version; never revise history."""
    content = {k: v for k, v in payload.items() if k not in {"fetched_at", "snapshot_id"}}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    ident = f"{symbol}:{kind}:{as_of}:{digest}"
    with _DB_LOCK, connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO reference_evidence_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ident, symbol, kind, as_of, payload.get("fetched_at") or _utc_now(),
             payload.get("source") or "gildata", _json_dump(payload)),
        )
        conn.commit()
    return ident


def reference_evidence_previous(symbol: str, kind: str, before_date: str, known_at: str) -> Optional[dict[str, Any]]:
    with _DB_LOCK, connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM reference_evidence_snapshots "
            "WHERE symbol=? AND kind=? AND as_of<? AND fetched_at<=? "
            "ORDER BY as_of DESC, fetched_at DESC LIMIT 1", (symbol, kind, before_date, known_at),
        ).fetchone()
    return _json_load(row["payload_json"]) if row else None


def overnight_alpha_get(cache_key: str) -> Optional[dict[str, Any]]:
    ensure_database()
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json, expires_at FROM overnight_alpha_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        expires_at = row["expires_at"]
        if expires_at is not None and float(expires_at) <= now:
            conn.execute("DELETE FROM overnight_alpha_cache WHERE cache_key = ?", (cache_key,))
            conn.commit()
            return None
        payload = _json_load(row["payload_json"], {})
        return payload if isinstance(payload, dict) else None


def overnight_alpha_set(
    cache_key: str,
    *,
    symbol: str,
    benchmark: str,
    universe_key: str,
    signal_profile: str,
    period: str,
    min_edge: float,
    payload: dict[str, Any],
    ttl_seconds: int,
) -> None:
    ensure_database()
    stats = payload.get("stats") or {}
    stamp = _utc_now()
    expires_at = time.time() + ttl_seconds if ttl_seconds > 0 else None
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO overnight_alpha_cache(
                cache_key, symbol, benchmark, universe_key, signal_profile,
                period, min_edge, payload_json, sample_count, alpha_win_rate,
                mean_beta_adjusted_alpha, created_at, updated_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                sample_count = excluded.sample_count,
                alpha_win_rate = excluded.alpha_win_rate,
                mean_beta_adjusted_alpha = excluded.mean_beta_adjusted_alpha,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at
            """,
            (
                cache_key,
                symbol,
                benchmark,
                universe_key,
                signal_profile,
                period,
                min_edge,
                _json_dump(payload),
                stats.get("sample_count"),
                stats.get("alpha_win_rate"),
                stats.get("mean_beta_adjusted_alpha"),
                stamp,
                stamp,
                expires_at,
            ),
        )
        conn.commit()


def save_home_dashboard_snapshot(payload: dict[str, Any], *, source: str = "home") -> str:
    ensure_database()
    snapshot_id = f"home_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    stamp = _utc_now()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO home_dashboard_snapshots(snapshot_id, generated_at, status, payload_json, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (snapshot_id, stamp, "completed", _json_dump(payload), source, stamp),
        )
        conn.commit()
    return snapshot_id


def latest_home_dashboard_snapshot() -> Optional[dict[str, Any]]:
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """
            SELECT snapshot_id, generated_at, status, payload_json, source
            FROM home_dashboard_snapshots
            ORDER BY generated_at DESC
            LIMIT 20
            """
        ).fetchall()
        if not rows:
            return None
        # Parse all non-empty snapshots first so "healthy" can be judged
        # relative to the best available (universes loaded, rows produced).
        parsed: list[tuple[sqlite3.Row, dict[str, Any], int, int, int]] = []
        for row in rows:
            payload = _json_load(row["payload_json"], {})
            if isinstance(payload, dict) and isinstance(payload.get("rows"), list) and payload["rows"]:
                row_count = len(payload.get("rows") or [])
                loaded_count = int(payload.get("loaded_universe_count") or 0)
                failed_count = int(payload.get("failed_universe_count") or 0)
                parsed.append((row, payload, row_count, loaded_count, failed_count))
        if parsed:
            best_loaded = max(item[3] for item in parsed)
            best_rows = max(item[2] for item in parsed)
            candidates: list[tuple[int, int, str, sqlite3.Row, dict[str, Any]]] = []
            for row, payload, row_count, loaded_count, failed_count in parsed:
                source_priority = 2 if str(row["source"] or "") == "daily_auto" else 1
                # A snapshot is "healthy" when it loaded all (or nearly all) of
                # its universes with no failures and did not collapse to a tiny
                # row count. Among healthy snapshots RECENCY wins -- raw row
                # count varies day to day and must NOT pin the board to an older
                # day that happened to screen a few more names.
                healthy = (
                    failed_count == 0
                    and loaded_count >= best_loaded
                    and row_count >= 0.5 * best_rows
                )
                candidates.append((source_priority, 1 if healthy else 0, str(row["generated_at"]), row, payload))
            # A larger old batch must not override a smaller fresh batch.
            candidates.sort(key=lambda item: (
                str(item[4].get("data_as_of") or item[4].get("required_price_session") or "")[:10],
                item[0], item[1], item[2],
            ), reverse=True)
            selected_row = candidates[0][3]
            selected_payload = candidates[0][4]
        else:
            selected_row = rows[0]
            selected_payload = _json_load(selected_row["payload_json"], {})
        return {
            "snapshot_id": selected_row["snapshot_id"],
            "generated_at": selected_row["generated_at"],
            "status": selected_row["status"],
            "source": selected_row["source"],
            "payload": selected_payload if isinstance(selected_payload, dict) else {},
        }


def symbol_directory_count() -> int:
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM symbol_directory").fetchone()["n"])


def symbol_directory_upsert_many(items: list[dict[str, Any]]) -> int:
    """Insert or refresh directory rows. Returns number of rows written."""
    if not items:
        return 0
    ensure_database()
    stamp = _utc_now()
    written = 0
    with _DB_LOCK, _connect() as conn:
        _remember_symbol_directory_names(conn, stamp)
        for item in items:
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            name = str(item.get("name") or symbol)
            exchange = str(item.get("exchange") or "")
            conn.execute(
                """
                INSERT INTO symbol_directory(
                    symbol, name, name_upper, exchange, exchange_display,
                    quote_type, source, has_history, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name = CASE WHEN excluded.name != '' AND excluded.name != symbol_directory.symbol
                                THEN excluded.name ELSE symbol_directory.name END,
                    name_upper = CASE WHEN excluded.name != '' AND excluded.name != symbol_directory.symbol
                                THEN excluded.name_upper ELSE symbol_directory.name_upper END,
                    exchange = CASE WHEN excluded.exchange != '' THEN excluded.exchange ELSE symbol_directory.exchange END,
                    exchange_display = CASE WHEN excluded.exchange_display != '' THEN excluded.exchange_display ELSE symbol_directory.exchange_display END,
                    has_history = MAX(symbol_directory.has_history, excluded.has_history),
                    updated_at = excluded.updated_at
                """,
                (
                    symbol,
                    name,
                    name.upper(),
                    exchange,
                    str(item.get("exchange_display") or ""),
                    str(item.get("quote_type") or "EQUITY"),
                    str(item.get("source") or "seed"),
                    1 if item.get("has_history") else 0,
                    stamp,
                ),
            )
            written += 1
        _remember_symbol_directory_names(conn, stamp)
        conn.commit()
    return written


def _remember_symbol_directory_names(conn: sqlite3.Connection, stamp: str) -> None:
    conn.execute("""
        INSERT OR IGNORE INTO symbol_directory_aliases
        (symbol, alias, alias_upper, alias_kind, source, updated_at)
        SELECT symbol, name, name_upper,
               CASE WHEN name GLOB '*[一-龥]*' THEN 'directory_cn' ELSE 'directory_en' END,
               'directory:known_name', ?
        FROM symbol_directory WHERE name != '' AND name != symbol AND LENGTH(name) <= 160
    """, (stamp,))


def _upsert_symbol_aliases(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> int:
    known = {row[0] for row in conn.execute("SELECT symbol FROM symbol_directory")}
    stamp = _utc_now()
    values = []
    kinds = {"official_cn", "vendor_cn", "common_cn", "directory_cn", "seed_en", "vendor_en", "directory_en"}
    for item in items:
        symbol = str(item.get("symbol") or "").strip().upper()
        alias = str(item.get("alias") or "").strip()
        kind = str(item.get("alias_kind") or "")
        source = str(item.get("source") or "").strip()
        url = str(item.get("source_url") or "")
        if (symbol not in known or not source or len(source) > 100 or kind not in kinds
                or not alias or alias.upper() == symbol or len(alias) > 160
                or alias in {"暂无", "未知", "None", "null", "-", "暂无数据"}
                or any(ord(c) < 32 for c in alias)):
            continue
        if kind == "official_cn" and not url:
            continue
        if url:
            try:
                parts = urlsplit(url)
                if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                        or parts.query or parts.fragment or len(url) > 1000):
                    continue
            except ValueError:
                continue
        values.append((symbol, alias, alias.upper(), kind, source, url, stamp))
    before = conn.total_changes
    conn.executemany("""
        INSERT INTO symbol_directory_aliases
        (symbol, alias, alias_upper, alias_kind, source, source_url, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, alias_upper, source) DO UPDATE SET
            alias=excluded.alias, alias_kind=excluded.alias_kind, source_url=excluded.source_url,
            updated_at=excluded.updated_at
        WHERE alias != excluded.alias OR alias_kind != excluded.alias_kind OR source_url != excluded.source_url
    """, values)
    return conn.total_changes - before


def symbol_directory_alias_upsert_many(items: list[dict[str, Any]]) -> int:
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        written = _upsert_symbol_aliases(conn, items)
        conn.commit()
    return written


def symbol_directory_refresh_aliases(seed: list[dict[str, Any]], verified: list[dict[str, Any]]) -> dict[str, int]:
    """Background import from bundled identities and already-cached vendor facts."""
    ensure_database()
    items = list(verified)
    items.extend({"symbol": r["symbol"], "alias": r["name"], "alias_kind": "seed_en", "source": "bundled:english"}
                 for r in seed)
    with _DB_LOCK, _connect() as conn:
        _remember_symbol_directory_names(conn, _utc_now())
        cached = conn.execute("SELECT payload_json FROM kv_cache WHERE cache_key LIKE 'gildata:research:company:%' "
                              "AND (expires_at IS NULL OR expires_at > ?) LIMIT 5000", (time.time(),)).fetchall()
        for row in cached:
            payload = _json_load(row[0], {})
            if not isinstance(payload, dict) or payload.get("source") != "gildata:company":
                continue
            for field in ("name", "name_cn", "short_name"):
                name = str(payload.get(field) or "").strip()
                if name:
                    items.append({"symbol": payload.get("symbol"), "alias": name,
                                  "alias_kind": "vendor_cn" if re.search(r"[\u4e00-\u9fff]", name) else "vendor_en",
                                  "source": "gildata:company"})
        written = _upsert_symbol_aliases(conn, items)
        conn.commit()
        count = conn.execute("SELECT COUNT(*), COUNT(DISTINCT CASE WHEN alias_kind LIKE '%_cn' THEN symbol END) "
                             "FROM symbol_directory_aliases").fetchone()
    return {"written": written, "alias_count": count[0], "chinese_symbol_count": count[1]}


def symbol_directory_blank_pool_exchanges() -> int:
    """Blank research-pool labels that were wrongly stored in the exchange field.

    Older builds wrote universe/pool names (e.g. '财报前预期修正池', 'S&P 100 / KOMP')
    into ``exchange``.  The search dropdown should show the real listing exchange,
    so we clear those so the seed's real exchange (or '未知板块') shows instead.
    """
    ensure_database()
    patterns = ["%池%", "%接力%", "%修正%", "%观察%", "%成交量%", "%S&P%", "%KOMP%", "%SOXX%", "%Top%"]
    where = " OR ".join("exchange LIKE ?" for _ in patterns)
    with _DB_LOCK, _connect() as conn:
        cursor = conn.execute(
            f"UPDATE symbol_directory SET exchange = '', exchange_display = '' WHERE {where}",
            tuple(patterns),
        )
        conn.commit()
        return cursor.rowcount or 0


def symbol_directory_search(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Read the local catalog without migrations or the shared writer lock."""
    q = str(query or "").strip().upper()
    size = max(0, min(12, int(limit)))
    if not q or not size or not DB_PATH.exists():
        return []
    columns = ", ".join("d." + c for c in ("symbol", "name", "exchange", "exchange_display", "quote_type", "source", "has_history"))
    rows: list[sqlite3.Row] = []
    seen: set[str] = set()
    aliases: dict[str, list[dict[str, Any]]] = {}
    conn = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")

        has_alias_table = conn.execute("SELECT 1 FROM sqlite_master WHERE name='symbol_directory_aliases' AND type='table'").fetchone()

        def append_matches(where: str, args: tuple[Any, ...], from_sql: str = "symbol_directory d") -> None:
            remaining = size - len(rows)
            if remaining <= 0:
                return
            matches = conn.execute(
                f"SELECT DISTINCT {columns} FROM {from_sql} WHERE {where} "
                "ORDER BY LENGTH(d.symbol), d.symbol LIMIT ?",
                (*args, remaining + len(seen)),
            ).fetchall()
            for row in matches:
                if row["symbol"] not in seen:
                    seen.add(row["symbol"])
                    rows.append(row)
                    if len(rows) == size:
                        break

        append_matches("symbol = ?", (q,))
        append_matches("d.symbol >= ? AND d.symbol < ?", (q, q + chr(0x10FFFF)))
        alias_join = "symbol_directory_aliases a JOIN symbol_directory d ON d.symbol=a.symbol"
        if has_alias_table:
            append_matches("a.alias_upper = ?", (q,), alias_join)
        # Binary ranges use the PK/name/alias indexes; contains is only a fallback.
        append_matches("d.name_upper >= ? AND d.name_upper < ?", (q, q + chr(0x10FFFF)))
        if has_alias_table:
            append_matches("a.alias_upper >= ? AND a.alias_upper < ?", (q, q + chr(0x10FFFF)), alias_join)
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        append_matches("symbol LIKE ? ESCAPE '\\' OR name_upper LIKE ? ESCAPE '\\'", (like, like))
        if has_alias_table:
            append_matches("a.alias_upper LIKE ? ESCAPE '\\'", (like,), alias_join)
            if rows:
                placeholders = ",".join("?" for _ in rows)
                details = conn.execute(
                    f"SELECT symbol,alias,alias_upper,alias_kind,source,source_url FROM symbol_directory_aliases "
                    f"WHERE symbol IN ({placeholders})", tuple(r["symbol"] for r in rows),
                ).fetchall()
                for detail in details:
                    aliases.setdefault(detail["symbol"], []).append(dict(detail))
    finally:
        conn.close()
    output = []
    priority = {"official_cn": 0, "vendor_cn": 1, "directory_cn": 2, "common_cn": 3,
                "vendor_en": 0, "directory_en": 1, "seed_en": 2}
    for row in rows:
        names = sorted(aliases.get(row["symbol"], []),
                       key=lambda a: (priority.get(a["alias_kind"], 9), len(a["alias"]), a["alias"], a["source"]))
        cn = next((a for a in names if a["alias_kind"].endswith("_cn")), None)
        english = next((a for a in names if a["alias_kind"].endswith("_en")), None)
        matches = sorted((a for a in names if q in a["alias_upper"]),
                         key=lambda a: (a["alias_upper"] != q, not a["alias_upper"].startswith(q),
                                        priority.get(a["alias_kind"], 9), len(a["alias"]), a["source"]))
        match = matches[0] if matches else None
        output.append({
            "symbol": row["symbol"],
            "name": row["name"],
            "name_cn": cn["alias"] if cn else (row["name"] if re.search(r"[\u4e00-\u9fff]", row["name"]) else None),
            "name_en": english["alias"] if english else (row["name"] if not re.search(r"[\u4e00-\u9fff]", row["name"]) else None),
            "name_cn_kind": cn["alias_kind"] if cn else None,
            "name_cn_source": cn["source"] if cn else None,
            "name_cn_source_url": cn["source_url"] if cn else None,
            "matched_alias": match["alias"] if match else None,
            "alias_kind": match["alias_kind"] if match else None,
            "alias_source": match["source"] if match else None,
            "alias_source_url": match["source_url"] if match else None,
            "exchange": row["exchange"],
            "exchange_display": row["exchange_display"],
            "quote_type": row["quote_type"],
            "source": row["source"],
            "has_history": bool(row["has_history"]),
        })
    return output


def user_watchlist_list(include_disabled: bool = False) -> list[dict[str, Any]]:
    ensure_database()
    where = "" if include_disabled else "WHERE enabled = 1"
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT symbol, name, note, enabled, source, created_at, updated_at
            FROM user_watchlist
            {where}
            ORDER BY enabled DESC, symbol
            """
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        out.append(item)
    return out


def user_watchlist_symbols(include_disabled: bool = False) -> list[str]:
    return [
        str(row.get("symbol") or "").upper()
        for row in user_watchlist_list(include_disabled=include_disabled)
        if row.get("symbol")
    ]


def user_watchlist_get(symbol: str) -> Optional[dict[str, Any]]:
    ensure_database()
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            """
            SELECT symbol, name, note, enabled, source, created_at, updated_at
            FROM user_watchlist
            WHERE symbol = ?
            """,
            (sym,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["enabled"] = bool(item.get("enabled"))
    return item


def user_watchlist_upsert(
    symbol: str,
    *,
    name: str = "",
    note: str = "",
    enabled: bool = True,
    source: str = "manual",
) -> dict[str, Any]:
    ensure_database()
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol is required")
    stamp = _utc_now()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_watchlist(symbol, name, note, enabled, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                name = CASE WHEN excluded.name != '' THEN excluded.name ELSE user_watchlist.name END,
                note = excluded.note,
                enabled = excluded.enabled,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (sym, str(name or ""), str(note or ""), 1 if enabled else 0, str(source or "manual"), stamp, stamp),
        )
        conn.commit()
    symbol_directory_upsert_many([{
        "symbol": sym,
        "name": name or sym,
        "source": "watchlist",
        "quote_type": "EQUITY",
        "has_history": True,
    }])
    return user_watchlist_get(sym) or {
        "symbol": sym,
        "name": name,
        "note": note,
        "enabled": bool(enabled),
        "source": source,
        "created_at": stamp,
        "updated_at": stamp,
    }


def user_watchlist_delete(symbol: str) -> bool:
    ensure_database()
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute("DELETE FROM user_watchlist WHERE symbol = ?", (sym,))
        conn.commit()
    return bool(cur.rowcount)


def user_watchlist_set_enabled(symbol: str, enabled: bool) -> Optional[dict[str, Any]]:
    ensure_database()
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    stamp = _utc_now()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            "UPDATE user_watchlist SET enabled = ?, updated_at = ? WHERE symbol = ?",
            (1 if enabled else 0, stamp, sym),
        )
        conn.commit()
    if not cur.rowcount:
        return None
    return user_watchlist_get(sym)


def database_status() -> dict[str, Any]:
    path = ensure_database()
    with _DB_LOCK, _connect() as conn:
        tables = {}
        for name in (
            "kv_cache",
            "overnight_alpha_cache",
            "home_dashboard_snapshots",
            "symbol_directory",
            "signal_calibration",
            "signal_events",
            "portfolio_holdings",
            "user_watchlist",
            "predictions",
            "priority_candidate_slices",
        ):
            tables[name] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
    return {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "tables": tables,
    }


# ---------------------------------------------------------------------------
# Signal calibration curves (R1: signal -> outcome -> calibration loop)
# ---------------------------------------------------------------------------

def signal_calibration_upsert(
    *,
    signal_type: str,
    horizon_days: int,
    curve: dict[str, Any],
    universe_set: str = "",
    source: str = "replay",
    event_count: int = 0,
    cluster_count: int = 0,
    cost_bps: float = 0.0,
    make_active: bool = True,
) -> str:
    """Persist a calibration curve and (by default) flip it active.

    Activation is exclusive per ``(signal_type, horizon_days)``: writing a new
    active curve clears the active flag on prior curves for the same pair.
    Returns the generated ``calibration_id``.
    """
    ensure_database()
    stamp = _utc_now()
    calibration_id = f"{signal_type}_{int(horizon_days)}d_{source}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    with _DB_LOCK, _connect() as conn:
        if make_active:
            conn.execute(
                "UPDATE signal_calibration SET is_active = 0 WHERE signal_type = ? AND horizon_days = ?",
                (signal_type, int(horizon_days)),
            )
        conn.execute(
            """
            INSERT INTO signal_calibration(
                calibration_id, signal_type, horizon_days, universe_set, source,
                generated_at, event_count, cluster_count, cost_bps, curve_json, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                calibration_id,
                signal_type,
                int(horizon_days),
                universe_set,
                source,
                stamp,
                int(event_count),
                int(cluster_count),
                float(cost_bps),
                _json_dump(curve),
                1 if make_active else 0,
            ),
        )
        conn.commit()
    return calibration_id


def _calibration_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "calibration_id": row["calibration_id"],
        "signal_type": row["signal_type"],
        "horizon_days": int(row["horizon_days"]),
        "universe_set": row["universe_set"],
        "source": row["source"],
        "generated_at": row["generated_at"],
        "event_count": int(row["event_count"]),
        "cluster_count": int(row["cluster_count"]),
        "cost_bps": float(row["cost_bps"]),
        "is_active": bool(row["is_active"]),
        "curve": _json_load(row["curve_json"], {}),
    }


def signal_calibration_active(signal_type: str, horizon_days: int) -> Optional[dict[str, Any]]:
    """Return the active calibration curve for a signal type / horizon."""
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM signal_calibration
            WHERE signal_type = ? AND horizon_days = ? AND is_active = 1
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (signal_type, int(horizon_days)),
        ).fetchone()
    return _calibration_row(row) if row else None


def signal_calibration_list(include_curve: bool = False) -> list[dict[str, Any]]:
    """List all stored calibration curves, newest first."""
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signal_calibration ORDER BY generated_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        item = _calibration_row(row)
        if not include_curve:
            item.pop("curve", None)
        result.append(item)
    return result


def _signal_event_id(signal_type: str, symbol: str, as_of_date: str, horizon_days: int) -> str:
    raw = f"{signal_type}|{symbol}|{as_of_date}|{int(horizon_days)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def signal_event_log_many(events: list[dict[str, Any]]) -> int:
    """Insert live signal events for later forward-return resolution.

    Each event needs ``signal_type, symbol, as_of_date, horizon_days, score``
    and optionally ``ref_price`` and ``run_id``.  Idempotent per
    ``(signal_type, symbol, as_of_date, horizon_days)`` so re-running a scan on
    the same day does not double-count; the existing (possibly resolved) row is
    left untouched.
    """
    if not events:
        return 0
    ensure_database()
    stamp = _utc_now()
    written = 0
    with _DB_LOCK, _connect() as conn:
        for event in events:
            signal_type = str(event.get("signal_type") or "").strip()
            symbol = str(event.get("symbol") or "").strip().upper()
            as_of_date = str(event.get("as_of_date") or "").strip()
            if not signal_type or not symbol or not as_of_date:
                continue
            horizon_days = int(event.get("horizon_days") or 0)
            event_id = _signal_event_id(signal_type, symbol, as_of_date, horizon_days)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO signal_events(
                    event_id, signal_type, symbol, as_of_date, horizon_days,
                    score, ref_price, run_id, created_at, resolved
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    event_id,
                    signal_type,
                    symbol,
                    as_of_date,
                    horizon_days,
                    float(event.get("score") or 0.0),
                    float(event["ref_price"]) if event.get("ref_price") is not None else None,
                    str(event.get("run_id") or ""),
                    stamp,
                ),
            )
            written += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        conn.commit()
    return written


def signal_events_pending(limit: int = 500) -> list[dict[str, Any]]:
    """Return unresolved signal events ordered by oldest as-of date first."""
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """
            SELECT event_id, signal_type, symbol, as_of_date, horizon_days, score, ref_price
            FROM signal_events
            WHERE resolved = 0
            ORDER BY as_of_date ASC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [
        {
            "event_id": row["event_id"],
            "signal_type": row["signal_type"],
            "symbol": row["symbol"],
            "as_of_date": row["as_of_date"],
            "horizon_days": int(row["horizon_days"]),
            "score": float(row["score"]),
            "ref_price": float(row["ref_price"]) if row["ref_price"] is not None else None,
        }
        for row in rows
    ]


def signal_event_resolve(
    event_id: str,
    *,
    forward_return: float,
    excess_return: float,
    baseline_return: float,
) -> None:
    """Mark a signal event resolved with its realized forward statistics."""
    ensure_database()
    stamp = _utc_now()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """
            UPDATE signal_events
            SET resolved = 1, forward_return = ?, excess_return = ?,
                baseline_return = ?, resolved_at = ?
            WHERE event_id = ?
            """,
            (float(forward_return), float(excess_return), float(baseline_return), stamp, event_id),
        )
        conn.commit()


def signal_events_resolved(signal_type: str | None = None, horizon_days: int | None = None) -> list[dict[str, Any]]:
    """Return resolved events (optionally filtered) for live recalibration."""
    ensure_database()
    clauses = ["resolved = 1"]
    params: list[Any] = []
    if signal_type:
        clauses.append("signal_type = ?")
        params.append(signal_type)
    if horizon_days is not None:
        clauses.append("horizon_days = ?")
        params.append(int(horizon_days))
    where = " AND ".join(clauses)
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT signal_type, symbol, as_of_date, horizon_days, score,
                   forward_return, excess_return, baseline_return
            FROM signal_events WHERE {where}
            ORDER BY as_of_date ASC
            """,
            tuple(params),
        ).fetchall()
    return [
        {
            "signal_type": row["signal_type"],
            "ticker": row["symbol"],
            "as_of_date": row["as_of_date"],
            "horizon_days": int(row["horizon_days"]),
            "score": float(row["score"]),
            "forward_return": float(row["forward_return"]) if row["forward_return"] is not None else 0.0,
            "excess_return": float(row["excess_return"]) if row["excess_return"] is not None else 0.0,
            "baseline_return": float(row["baseline_return"]) if row["baseline_return"] is not None else 0.0,
        }
        for row in rows
    ]


def signal_events_count() -> dict[str, int]:
    """Return resolved / pending signal-event counts."""
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        total = int(conn.execute("SELECT COUNT(*) AS n FROM signal_events").fetchone()["n"])
        resolved = int(conn.execute("SELECT COUNT(*) AS n FROM signal_events WHERE resolved = 1").fetchone()["n"])
    return {"total": total, "resolved": resolved, "pending": total - resolved}


# ---------------------------------------------------------------------------
# ATM implied-volatility history (IV-rise launch signal)
# ---------------------------------------------------------------------------

def iv_history_log_many(items: list[dict[str, Any]]) -> int:
    """Insert daily ATM IV snapshots. Idempotent per (symbol, as_of_date)."""
    if not items:
        return 0
    ensure_database()
    stamp = _utc_now()
    written = 0
    with _DB_LOCK, _connect() as conn:
        for item in items:
            symbol = str(item.get("symbol") or "").strip().upper()
            as_of = str(item.get("as_of_date") or "").strip()
            if not symbol or not as_of or item.get("atm_iv") is None:
                continue
            cursor = conn.execute(
                """
                INSERT INTO iv_history(symbol, as_of_date, atm_iv, iv_hv, hv, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, as_of_date) DO UPDATE SET
                    atm_iv = excluded.atm_iv, iv_hv = excluded.iv_hv, hv = excluded.hv
                """,
                (symbol, as_of, float(item["atm_iv"]),
                 float(item["iv_hv"]) if item.get("iv_hv") is not None else None,
                 float(item["hv"]) if item.get("hv") is not None else None, stamp),
            )
            written += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        conn.commit()
    return written


def iv_history_recent(symbol: str, limit: int = 12) -> list[dict[str, Any]]:
    """Return a symbol's most recent IV snapshots, newest first."""
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT as_of_date, atm_iv, iv_hv, hv FROM iv_history WHERE symbol = ? ORDER BY as_of_date DESC LIMIT ?",
            (str(symbol).strip().upper(), max(1, int(limit))),
        ).fetchall()
    return [{"as_of_date": r["as_of_date"], "atm_iv": r["atm_iv"], "iv_hv": r["iv_hv"], "hv": r["hv"]} for r in rows]


def iv_history_count() -> int:
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM iv_history").fetchone()["n"])


# ---------------------------------------------------------------------------
# Portfolio holdings + account cash (manual position tracker -> decisions)
# ---------------------------------------------------------------------------

def portfolio_holdings_list() -> list[dict[str, Any]]:
    """Return all manually-recorded holdings, ordered by symbol."""
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT symbol, shares, avg_cost, note, opened_at, updated_at "
            "FROM portfolio_holdings ORDER BY symbol"
        ).fetchall()
    return [
        {
            "symbol": r["symbol"],
            "shares": float(r["shares"] or 0),
            "avg_cost": float(r["avg_cost"] or 0),
            "note": r["note"] or "",
            "opened_at": r["opened_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def portfolio_holding_upsert(
    *,
    symbol: str,
    shares: float,
    avg_cost: float,
    note: str = "",
    opened_at: str | None = None,
) -> None:
    """Insert or update a single holding (keyed by symbol)."""
    ensure_database()
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol required")
    stamp = _utc_now()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO portfolio_holdings(symbol, shares, avg_cost, note, opened_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                shares = excluded.shares,
                avg_cost = excluded.avg_cost,
                note = excluded.note,
                opened_at = COALESCE(excluded.opened_at, portfolio_holdings.opened_at),
                updated_at = excluded.updated_at
            """,
            (sym, float(shares or 0), float(avg_cost or 0), str(note or ""),
             opened_at or stamp, stamp),
        )
        conn.commit()


def portfolio_holding_delete(symbol: str) -> int:
    ensure_database()
    sym = str(symbol or "").strip().upper()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute("DELETE FROM portfolio_holdings WHERE symbol = ?", (sym,))
        conn.commit()
        return int(cur.rowcount or 0)


def portfolio_account_get() -> dict[str, Any]:
    """Return the single account row (available cash); defaults if unset."""
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT available_cash, currency, updated_at FROM portfolio_account WHERE id = 1"
        ).fetchone()
    if not row:
        return {"available_cash": 0.0, "currency": "USD", "updated_at": None}
    return {
        "available_cash": float(row["available_cash"] or 0),
        "currency": row["currency"] or "USD",
        "updated_at": row["updated_at"],
    }


def predictions_log_many(rows: list[dict[str, Any]]) -> int:
    """Idempotently log predictions.

    The first forecast is immutable. Later daily slices can update for the
    monitor, but cannot change the probability, rank or features being tested.
    """
    ensure_database()
    stamp = _utc_now()
    written = 0
    with _DB_LOCK, _connect() as conn:
        for r in rows:
            sym = str(r.get("symbol") or "").strip().upper()
            if not sym:
                continue
            pid = f"{r.get('as_of_date')}_{sym}_{int(r.get('horizon_days') or 0)}_{r.get('signal_type') or 'pullback_hv'}"
            cur = conn.execute(
                """
                INSERT INTO predictions(
                    prediction_id, as_of_date, symbol, rank, signal_type, calibrated_prob,
                    entry_ref_price, horizon_days, curve_source, is_board_pick,
                    market_liquid_rs_top40, stock_stronger_than_industry,
                    playbook_score, market_rs_score, playbook_tags,
                    mode, created_at, evaluation_version, forecast_json, resolved
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(as_of_date, symbol, horizon_days, signal_type) DO NOTHING
                """,
                (pid, str(r.get("as_of_date")), sym,
                 int(r["rank"]) if r.get("rank") is not None else None,
                 str(r.get("signal_type") or "pullback_hv"),
                 float(r["calibrated_prob"]) if r.get("calibrated_prob") is not None else None,
                 float(r["entry_ref_price"]) if r.get("entry_ref_price") is not None else None,
                 int(r.get("horizon_days") or 0), r.get("curve_source"),
                 1 if r.get("is_board_pick", True) else 0,
                 1 if r.get("market_liquid_rs_top40") else 0,
                 1 if r.get("stock_stronger_than_industry") else 0,
                 float(r["playbook_score"]) if r.get("playbook_score") is not None else None,
                 float(r["market_rs_score"]) if r.get("market_rs_score") is not None else None,
                 _json_dump(r.get("playbook_tags") or []),
                 str(r.get("mode") or "live"), stamp,
                 str(r.get("evaluation_version") or "legacy"), _json_dump(r.get("forecast") or {})),
            )
            written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    return written


def priority_candidate_slices_log(
    *,
    as_of_date: str,
    rows: list[dict[str, Any]],
    horizon_days: int,
    signal_type: str = "pullback_hv",
    board_top_n: int = 30,
) -> int:
    """Persist the full daily candidate slice for lifecycle monitoring."""
    ensure_database()
    stamp = _utc_now()
    written = 0
    with _DB_LOCK, _connect() as conn:
        for i, r in enumerate(rows):
            sym = str(r.get("symbol") or "").strip().upper()
            if not sym:
                continue
            rank = int(r.get("candidate_rank") or r.get("rank") or i + 1)
            sid = f"{as_of_date}_{sym}_{signal_type}"
            is_pick = 1 if rank <= int(board_top_n or 0) else 0
            payload = dict(r)
            payload["candidate_rank"] = rank
            payload["is_board_pick"] = bool(is_pick)
            cur = conn.execute(
                """
                INSERT INTO priority_candidate_slices(
                    slice_id, as_of_date, symbol, candidate_rank, is_board_pick,
                    horizon_days, signal_type, current_price, calibrated_prob,
                    priority_score, relative_strength_20d, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(as_of_date, symbol, signal_type) DO UPDATE SET
                    candidate_rank = excluded.candidate_rank,
                    is_board_pick = excluded.is_board_pick,
                    horizon_days = excluded.horizon_days,
                    current_price = excluded.current_price,
                    calibrated_prob = excluded.calibrated_prob,
                    priority_score = excluded.priority_score,
                    relative_strength_20d = excluded.relative_strength_20d,
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at
                """,
                (
                    sid,
                    as_of_date,
                    sym,
                    rank,
                    is_pick,
                    int(horizon_days or 0),
                    signal_type,
                    float(r["current_price"]) if r.get("current_price") is not None else None,
                    float(r["calibrated_probability"]) if r.get("calibrated_probability") is not None else None,
                    float(r["priority_score"]) if r.get("priority_score") is not None else None,
                    float(r["relative_strength_20d"]) if r.get("relative_strength_20d") is not None else None,
                    _json_dump(payload),
                    stamp,
                ),
            )
            written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    return written


def priority_candidate_latest_date() -> str | None:
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute("SELECT MAX(as_of_date) as d FROM priority_candidate_slices").fetchone()
    return row["d"] if row and row["d"] else None


def priority_candidate_slices_for_date(as_of_date: str | None = None, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
    ensure_database()
    day = as_of_date or priority_candidate_latest_date()
    if not day:
        return []
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM priority_candidate_slices
            WHERE as_of_date = ?
            ORDER BY candidate_rank
            LIMIT ? OFFSET ?
            """,
            (day, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["payload"] = _json_load(d.pop("payload_json", None), {})
        out.append(d)
    return out


def priority_candidate_recent(days: int = 30) -> list[dict[str, Any]]:
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM priority_candidate_slices
            WHERE as_of_date >= date('now', ?)
            ORDER BY symbol, as_of_date DESC
            """,
            (f"-{max(1, int(days))} days",),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["payload"] = _json_load(d.pop("payload_json", None), {})
        out.append(d)
    return out


def predictions_pending(limit: int = 2000) -> list[dict[str, Any]]:
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT prediction_id, as_of_date, symbol, horizon_days, entry_ref_price, calibrated_prob, created_at, evaluation_version, forecast_json "
            "FROM predictions WHERE resolved = 0 ORDER BY as_of_date LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(r) for r in rows]


def prediction_resolve(prediction_id: str, *, exit_price: float, forward_return: float,
                       baseline_return: float, excess_return: float, net_excess: float, win: int,
                       beta_adjusted_alpha: float | None = None, entry_date: str | None = None,
                       exit_date: str | None = None, net_return: float | None = None) -> None:
    ensure_database()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "UPDATE predictions SET resolved=1, exit_price=?, forward_return=?, baseline_return=?, "
            "excess_return=?, net_excess=?, win=?, beta_adjusted_alpha=?, resolved_at=?, entry_date=?, exit_date=?, net_return=? WHERE prediction_id=? AND resolved=0",
            (float(exit_price), float(forward_return), float(baseline_return), float(excess_return),
             float(net_excess), int(win),
             float(beta_adjusted_alpha) if beta_adjusted_alpha is not None else None,
             _utc_now(), entry_date, exit_date, net_return, prediction_id),
        )
        conn.commit()


def predictions_resolved(since_date: str | None = None, board_only: bool = True,
                         mode: str | None = None) -> list[dict[str, Any]]:
    ensure_database()
    q = ("SELECT as_of_date, symbol, rank, calibrated_prob, horizon_days, forward_return, "
         "baseline_return, excess_return, net_excess, win, beta_adjusted_alpha, "
         "market_liquid_rs_top40, stock_stronger_than_industry, playbook_score, "
         "market_rs_score, playbook_tags, evaluation_version, net_return FROM predictions WHERE resolved=1")
    params: list[Any] = []
    if board_only:
        q += " AND is_board_pick=1"
    if mode:
        q += " AND mode=?"
        params.append(mode)
    if since_date:
        q += " AND as_of_date >= ?"
        params.append(since_date)
    q += " ORDER BY as_of_date"
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def predictions_open(board_only: bool = True, mode: str | None = None, evaluation_version: str | None = None) -> list[dict[str, Any]]:
    ensure_database()
    q = ("SELECT as_of_date, symbol, rank, calibrated_prob, horizon_days, entry_ref_price "
         "FROM predictions WHERE resolved=0")
    params: list[Any] = []
    if board_only:
        q += " AND is_board_pick=1"
    if mode:
        q += " AND mode=?"
        params.append(mode)
    if evaluation_version:
        q += " AND evaluation_version=?"
        params.append(evaluation_version)
    q += " ORDER BY as_of_date DESC, rank"
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def predictions_count(mode: str | None = None, evaluation_version: str | None = None) -> dict[str, int]:
    ensure_database()
    clauses, values = [], []
    if mode:
        clauses.append("mode=?")
        values.append(mode)
    if evaluation_version:
        clauses.append("evaluation_version=?")
        values.append(evaluation_version)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params = tuple(values)
    with _DB_LOCK, _connect() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) n FROM predictions{where}", params).fetchone()["n"])
        resolved = int(conn.execute(
            f"SELECT COUNT(*) n FROM predictions WHERE resolved=1{(' AND ' + ' AND '.join(clauses)) if clauses else ''}",
            params).fetchone()["n"])
        board = int(conn.execute(
            f"SELECT COUNT(*) n FROM predictions WHERE is_board_pick=1{(' AND ' + ' AND '.join(clauses)) if clauses else ''}",
            params).fetchone()["n"])
        by_mode = {row["mode"]: int(row["n"]) for row in conn.execute(
            "SELECT mode, COUNT(*) n FROM predictions GROUP BY mode")}
    return {"total": total, "resolved": resolved, "pending": total - resolved,
            "board_decisions": board, "by_mode": by_mode}


def portfolio_account_set(available_cash: float, currency: str = "USD") -> None:
    ensure_database()
    stamp = _utc_now()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO portfolio_account(id, available_cash, currency, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                available_cash = excluded.available_cash,
                currency = excluded.currency,
                updated_at = excluded.updated_at
            """,
            (float(available_cash or 0), str(currency or "USD"), stamp),
        )
        conn.commit()
