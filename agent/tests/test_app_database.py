from __future__ import annotations

import app_database


def test_database_initializes_and_caches_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_database, "DB_PATH", tmp_path / "easymoneysniper.sqlite3")
    monkeypatch.setattr(app_database, "_INITIALIZED", False)

    path = app_database.ensure_database()
    assert path.exists()

    app_database.cache_set("hello", {"value": 1}, ttl_seconds=60)
    assert app_database.cache_get("hello") == {"value": 1}

    snapshot_id = app_database.save_home_dashboard_snapshot({"rows": [{"symbol": "NVDA"}]})
    latest = app_database.latest_home_dashboard_snapshot()
    assert latest["snapshot_id"] == snapshot_id
    assert latest["payload"]["rows"][0]["symbol"] == "NVDA"

    status = app_database.database_status()
    assert status["exists"] is True
    assert status["tables"]["home_dashboard_snapshots"] == 1
