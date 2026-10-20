"""Calendar persistence in the application's existing SQLite database."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

import app_database as db
from trading_calendar_history import full_snapshot, is_full_snapshot, semantic_payload

_LOCK = threading.RLock()


class RunLock:
    """OS advisory lock; a surviving worker retains ownership after parent exit."""
    def __init__(self, name: str):
        if name not in {"dispatch", "worker"}:
            raise ValueError("invalid calendar lock")
        self.path = db.DB_PATH.parent / f"trading_calendar_{name}.lock"
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(raw: Any, default: Any = None) -> Any:
    try:
        return json.loads(raw) if isinstance(raw, str) else default
    except (ValueError, TypeError):
        return default


def ensure_tables() -> None:
    db.ensure_database()
    with _LOCK, db.connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trading_calendar_events (
                event_id TEXT PRIMARY KEY, category TEXT NOT NULL, source TEXT NOT NULL,
                event_date TEXT NOT NULL, end_date TEXT, start_utc TEXT,
                title TEXT NOT NULL, status TEXT NOT NULL, importance REAL NOT NULL,
                payload_json TEXT NOT NULL, first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_calendar_date ON trading_calendar_events(event_date, end_date);
            CREATE INDEX IF NOT EXISTS idx_calendar_source ON trading_calendar_events(source, category);
            CREATE TABLE IF NOT EXISTS trading_calendar_symbols (
                event_id TEXT NOT NULL, symbol TEXT NOT NULL, relation_type TEXT NOT NULL,
                PRIMARY KEY(event_id, symbol, relation_type)
            );
            CREATE INDEX IF NOT EXISTS idx_calendar_symbol ON trading_calendar_symbols(symbol, event_id);
            CREATE TABLE IF NOT EXISTS trading_calendar_changes (
                change_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
                changed_at TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_calendar_changes ON trading_calendar_changes(event_id, change_id);
            CREATE TABLE IF NOT EXISTS trading_calendar_sources (
                source TEXT PRIMARY KEY, payload_json TEXT NOT NULL
            );
        """)
        conn.commit()


def upsert_events(events: list[dict[str, Any]]) -> int:
    ensure_tables()
    written = 0
    now = stamp()
    with _LOCK, db.connection() as conn:
        for item in events:
            event = dict(item)
            ident = event["event_id"]
            if event.pop("cancellation_only", False):
                old = conn.execute("SELECT payload_json FROM trading_calendar_events WHERE event_id=?", (ident,)).fetchone()
                previous = load_json(old[0], {}) if old else None
                if previous is None:
                    for candidate in conn.execute("SELECT payload_json FROM trading_calendar_events WHERE source=?", (event["source"],)):
                        value = load_json(candidate[0], {})
                        if value.get("detail", {}).get("source_uid") == event.get("source_uid"):
                            previous = value
                            break
                if previous is None:
                    continue
                event = {**previous, "status": "cancelled", "fetched_at": event["fetched_at"]}
                ident = event["event_id"]
            event.setdefault("status", "scheduled")
            event.setdefault("confidence", "unverified")
            event.setdefault("symbols", [])
            event.setdefault("detail", {})
            if event.get("category") == "earnings":
                symbol = next((s["symbol"] for s in event.get("symbols", []) if s.get("relation_type") == "subject"), "")
                period = event.get("detail", {}).get("reference_period")
                candidates = conn.execute("SELECT e.event_id,e.payload_json FROM trading_calendar_events e JOIN trading_calendar_symbols s ON e.event_id=s.event_id WHERE e.category='earnings' AND s.symbol=? AND s.relation_type='subject'", (symbol,)).fetchall()
                compatible = []
                for candidate in candidates:
                    prior = load_json(candidate[1], {})
                    prior_period = prior.get("detail", {}).get("reference_period")
                    same_period = bool(period and prior_period and period == prior_period)
                    same_date = event["date"] == prior.get("date") and not (period and prior_period and period != prior_period)
                    if same_period or same_date:
                        compatible.append((0 if same_period else 1, candidate[0], prior_period))
                if compatible:
                    _, ident, previous_period = sorted(compatible)[0]
                    if previous_period and not period:
                        event["detail"] = {**event.get("detail", {}), "reference_period": previous_period}
                elif candidates and conn.execute("SELECT 1 FROM trading_calendar_events WHERE event_id=?", (ident,)).fetchone():
                    import hashlib
                    ident += ":" + hashlib.sha256(str(period).encode()).hexdigest()[:8]
                event["event_id"] = ident
            old_row = conn.execute("SELECT payload_json FROM trading_calendar_events WHERE event_id=?", (ident,)).fetchone()
            old = load_json(old_row[0], {}) if old_row else {}
            if old.get("fetched_at") and not event.get("fetched_at"):
                continue
            # Never let an old import override evidence acquired later.
            if old.get("fetched_at") and event.get("fetched_at") and event["fetched_at"] < old["fetched_at"]:
                continue
            event["first_seen_at"] = old.get("first_seen_at") or event.get("first_seen_at") or now
            changed = semantic_payload(old) != semantic_payload(event)
            if old and not changed:
                event["updated_at"] = old.get("updated_at") or now
            else:
                event["updated_at"] = now
            latest = conn.execute("SELECT after_json FROM trading_calendar_changes WHERE event_id=? ORDER BY change_id DESC LIMIT 1", (ident,)).fetchone()
            anchored = bool(latest and is_full_snapshot(load_json(latest[0], {})))
            if changed or not anchored:
                # Legacy rows get an anchor now, never a fabricated first-seen version.
                before = full_snapshot(old) if old and anchored else {}
                conn.execute("INSERT INTO trading_calendar_changes(event_id,changed_at,before_json,after_json) VALUES(?,?,?,?)",
                             (ident, now, json.dumps(before, ensure_ascii=False, allow_nan=False),
                              json.dumps(full_snapshot(event), ensure_ascii=False, allow_nan=False)))
            conn.execute("""INSERT INTO trading_calendar_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET category=excluded.category,source=excluded.source,
                event_date=excluded.event_date,end_date=excluded.end_date,start_utc=excluded.start_utc,
                title=excluded.title,status=excluded.status,importance=excluded.importance,
                payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (ident, event["category"], event["source"], event["date"], event.get("end_date"),
                 event.get("start_utc"), event["title"], event.get("status", "scheduled"), event.get("importance", 50),
                 json.dumps(event, ensure_ascii=False, allow_nan=False), event["first_seen_at"], event["updated_at"]))
            conn.execute("DELETE FROM trading_calendar_symbols WHERE event_id=?", (ident,))
            conn.executemany("INSERT OR IGNORE INTO trading_calendar_symbols VALUES(?,?,?)",
                             [(ident, s["symbol"], s.get("relation_type", "subject")) for s in event.get("symbols", [])])
            written += 1
        conn.commit()
    return written


def source_set(source: str, payload: dict[str, Any]) -> None:
    ensure_tables()
    with _LOCK, db.connection() as conn:
        conn.execute("INSERT INTO trading_calendar_sources VALUES(?,?) ON CONFLICT(source) DO UPDATE SET payload_json=excluded.payload_json",
                     (source, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        conn.commit()


def sources_get() -> list[dict[str, Any]]:
    ensure_tables()
    with db.connection() as conn:
        return [load_json(row[0], {}) for row in conn.execute("SELECT payload_json FROM trading_calendar_sources ORDER BY source")]


def event_get(event_id: str) -> dict[str, Any] | None:
    ensure_tables()
    with db.connection() as conn:
        row = conn.execute("SELECT payload_json FROM trading_calendar_events WHERE event_id=?", (event_id,)).fetchone()
        return load_json(row[0], {}) if row else None


def changes_get(event_id: str) -> list[dict[str, Any]]:
    with db.connection() as conn:
        return [{"changed_at": r[0], "before": load_json(r[1], {}), "after": load_json(r[2], {})}
                for r in conn.execute("SELECT changed_at,before_json,after_json FROM trading_calendar_changes WHERE event_id=? AND before_json!='{}' ORDER BY change_id DESC LIMIT 20", (event_id,))]


def withdraw_news(event_id: str, *, reason: str, fetched_at: str) -> None:
    existing = event_get(event_id)
    if existing and existing.get("status") != "withdrawn":
        existing.update(status="withdrawn", fetched_at=fetched_at)
        existing["detail"] = {**existing.get("detail", {}), "correction_note": reason}
        upsert_events([existing])


def events_between(start: str, end: str, categories: list[str], q: str, high_only: bool) -> list[dict[str, Any]]:
    ensure_tables()
    where = ["event_date <= ?", "COALESCE(end_date,event_date) >= ?", "status!='withdrawn'"]
    args: list[Any] = [end, start]
    if categories:
        where.append("category IN (" + ",".join("?" for _ in categories) + ")")
        args.extend(categories)
    if high_only:
        where.append("importance >= 75")
    if q:
        term = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM trading_calendar_symbols s LEFT JOIN symbol_directory d ON d.symbol=s.symbol WHERE s.event_id=trading_calendar_events.event_id AND (s.symbol LIKE ? ESCAPE '\\' OR d.name LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM symbol_directory_aliases a WHERE a.symbol=s.symbol AND a.alias LIKE ? ESCAPE '\\'))))")
        args.extend([f"%{term}%"] * 4)
    with db.connection() as conn:
        return [load_json(r[0], {}) for r in conn.execute("SELECT payload_json FROM trading_calendar_events WHERE " + " AND ".join(where), args)]
