from __future__ import annotations

import asyncio
import sqlite3
import threading
from unittest.mock import patch

import pytest

import api_server as api
import app_database as db


@pytest.fixture
def directory(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "directory.sqlite3")
    monkeypatch.setattr(db, "_INITIALIZED", False)
    db.ensure_database()
    db.symbol_directory_upsert_many([
        {"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NMS"},
        {"symbol": "AAP", "name": "Advance Auto Parts"},
        {"symbol": "AA", "name": "Alcoa"},
        {"symbol": "NVDA", "name": "NVIDIA Corporation"},
        {"symbol": "BRK.B", "name": "Berkshire Hathaway"},
        {"symbol": "X", "name": "Pineapple Research"},
        {"symbol": "TEST", "name": "百分之百 100%_ Company"},
    ])
    return db.DB_PATH


def test_exact_prefix_name_and_substring_order(directory):
    assert [r["symbol"] for r in db.symbol_directory_search("aap")] == ["AAP", "AAPL"]
    assert [r["symbol"] for r in db.symbol_directory_search("apple")] == ["AAPL", "X"]
    assert db.symbol_directory_search("BRK.B")[0]["symbol"] == "BRK.B"
    assert db.symbol_directory_search("百分")[0]["symbol"] == "TEST"
    assert len(db.symbol_directory_search("a", 2)) == 2
    assert db.symbol_directory_search("not a company") == []
    assert db.symbol_directory_search(" ") == []


def test_literal_wildcards_and_bounded_limit(directory):
    assert [r["symbol"] for r in db.symbol_directory_search("%_")] == ["TEST"]
    assert len(db.symbol_directory_search("a", 1000)) <= 12
    assert db.symbol_directory_search("a", 0) == []


def test_reads_do_not_initialize_write_or_take_shared_lock(directory):
    entered, release = threading.Event(), threading.Event()

    def writer_lock():
        with db._DB_LOCK:
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=writer_lock)
    thread.start()
    assert entered.wait(2)
    try:
        with patch.object(db, "ensure_database", side_effect=AssertionError("read must not migrate")):
            assert db.symbol_directory_search("NVDA")[0]["symbol"] == "NVDA"
    finally:
        release.set()
        thread.join(2)


def test_wal_uncommitted_writer_does_not_block_reader(directory):
    writer = sqlite3.connect(directory)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE symbol_directory SET name='uncommitted' WHERE symbol='AAPL'")
        assert db.symbol_directory_search("AAPL")[0]["name"] == "Apple Inc."
    finally:
        writer.rollback()
        writer.close()


def test_read_on_missing_directory_does_not_create_database(tmp_path, monkeypatch):
    path = tmp_path / "missing.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", path)
    assert db.symbol_directory_search("NVDA") == []
    assert not path.exists()


def test_search_uses_existing_indexes(directory):
    with sqlite3.connect(directory) as conn:
        for column, expected in [("symbol", "sqlite_autoindex_symbol_directory_1"),
                                 ("name_upper", "idx_symbol_directory_name")]:
            plan = conn.execute(
                f"EXPLAIN QUERY PLAN SELECT symbol FROM symbol_directory WHERE {column} >= ? AND {column} < ?",
                ("A", "A" + chr(0x10FFFF)),
            ).fetchall()
            assert any("SEARCH" in str(row) and expected in str(row) for row in plan)


def test_endpoint_only_reads_directory_and_yields_event_loop(directory):
    entered, release = threading.Event(), threading.Event()

    def slow_read(*args):
        entered.set()
        assert release.wait(2)
        return [{"symbol": "AAPL"}]

    async def verify():
        request = asyncio.create_task(api.single_stock_overnight_search("AAPL", 8))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.005)
            assert entered.is_set()
            assert not request.done()
        finally:
            release.set()
        assert (await request)["items"] == [{"symbol": "AAPL"}]

    with (patch.object(api, "symbol_directory_search", side_effect=slow_read),
          patch.object(api, "_ensure_symbol_directory", side_effect=AssertionError("no hot-path maintenance")),
          patch.object(api, "_upsert_dynamic_symbols", side_effect=AssertionError("no report scanning"))):
        asyncio.run(verify())


def test_empty_endpoint_never_queries_or_maintains_directory():
    with patch.object(api, "symbol_directory_search") as search:
        assert asyncio.run(api.single_stock_overnight_search("  ", 8)) == {"items": []}
        search.assert_not_called()


def test_maintenance_is_single_background_worker_and_stoppable():
    with (patch.object(api, "_SYMBOL_DIRECTORY_THREAD", None),
          patch.object(api, "_SYMBOL_DIRECTORY_STOP", threading.Event()),
          patch.object(api.threading, "Thread") as thread):
        api._start_symbol_directory_maintenance()
        api._start_symbol_directory_maintenance()
        assert thread.call_count == 1
        thread.return_value.start.assert_called_once()
        api._SYMBOL_DIRECTORY_STOP.set()
        with patch.object(api, "_ensure_symbol_directory") as ensure:
            thread.call_args.kwargs["target"]()
            ensure.assert_not_called()


def test_maintenance_failure_keeps_directory_and_retries_later(directory):
    stop = threading.Event()
    sleeps = []

    def wait(seconds):
        sleeps.append(seconds)
        stop.set()
        return True

    with (patch.object(api, "_SYMBOL_DIRECTORY_STOP", stop),
          patch.object(stop, "wait", side_effect=wait),
          patch.object(api, "_ensure_symbol_directory", side_effect=RuntimeError("fixture"))):
        api._symbol_directory_maintenance_loop()
    assert sleeps == [60]
    assert db.symbol_directory_search("NVDA")[0]["symbol"] == "NVDA"
