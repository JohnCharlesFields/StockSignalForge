from copy import deepcopy
import json

import pytest

import app_database as db
import trading_calendar_repository as repo
from event_exit_research import compile_calendar


def event(**updates):
    return {"event_id": "e1", "category": "earnings", "source": "fixture",
            "date": "2026-10-14", "title": "earnings", "status": "scheduled",
            "confidence": "official", "detail": {},
            "symbols": [{"symbol": "AAA", "relation_type": "subject"}],
            "first_seen_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:00:00Z", **updates}


def version(item):
    return {**deepcopy(item), "_calendar_snapshot_version": 2}


def exported(events, changes=()):
    return {"exported_at": "2026-10-16T12:00:00Z", "events": events, "changes": list(changes)}


def change(before, after, when):
    return {"event_id": "e1", "changed_at": when,
            "before": version(before) if before else {}, "after": version(after)}


def test_late_subject_correction_is_not_backdated_without_history():
    corrected = event(updated_at="2026-10-15T12:00:00Z",
                      symbols=[{"symbol": "BBB", "relation_type": "subject"}])
    result = compile_calendar(exported([corrected]), {})
    assert result.exits == []
    assert result.audit["legacy_current_only"] == 1


@pytest.mark.parametrize("updates", [
    {"symbols": [{"symbol": "BBB", "relation_type": "subject"}]},
    {"category": "news"}, {"title": "FOMC minutes", "detail": {"summary": "minutes"}},
    {"confidence": "unverified"}, {"status": "cancelled"}, {"date": "2026-10-16"},
    {"source_url": "https://later.example/updated-evidence"},
])
def test_complete_history_preserves_past_version_after_any_late_correction(updates):
    original = event(category="macro", title="FOMC decision") if "title" in updates else event()
    later = {**original, **updates, "updated_at": "2026-10-20T12:00:00Z"}
    initial = change(None, original, original["first_seen_at"])
    before = compile_calendar(exported([original], [initial]), {})
    after = compile_calendar(exported([later], [initial, change(original, later, later["updated_at"])]), {})
    assert before.exits and after.exits == before.exits


def test_pre_cutoff_subject_correction_uses_new_subject():
    original = event()
    later = event(updated_at="2026-10-12T12:00:00Z",
                  symbols=[{"symbol": "BBB", "relation_type": "subject"}])
    result = compile_calendar(exported([later], [change(None, original, original["first_seen_at"]),
        change(original, later, later["updated_at"])]), {})
    assert [e["subjects"] for e in result.exits] == [["BBB"]]


def test_partial_legacy_revisions_cannot_reconstruct_earlier_metadata():
    later = event(status="cancelled", updated_at="2026-10-15T12:00:00Z",
                  symbols=[{"symbol": "BBB", "relation_type": "subject"}])
    partial = {"event_id": "e1", "changed_at": later["updated_at"],
               "before": {"status": "scheduled"}, "after": {"status": "cancelled"}}
    result = compile_calendar(exported([later], [partial]), {})
    assert result.exits == []
    assert result.audit["legacy_partial_revisions"] == 1


def test_missing_or_naive_current_version_time_is_unknown_not_first_seen():
    for updated in (None, "2026-10-02", "not-a-date"):
        assert not compile_calendar(exported([event(updated_at=updated)]), {}).exits


def test_snapshot_time_cannot_be_backdated_by_imported_first_seen():
    original = event()
    result = compile_calendar(exported([original], [change(None, original, "2026-10-15T12:00:00Z")]), {})
    assert result.exits == []


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "calendar.sqlite3")
    monkeypatch.setattr(db, "_INITIALIZED", False)
    repo.ensure_tables()


def db_export():
    with db.connection() as conn:
        events = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM trading_calendar_events")]
        changes = [{"event_id": r[0], "changed_at": r[1], "before": json.loads(r[2]), "after": json.loads(r[3])}
                   for r in conn.execute("SELECT event_id,changed_at,before_json,after_json FROM trading_calendar_changes ORDER BY change_id")]
    return exported(events, changes)


def test_repository_archives_initial_payload_and_metadata_only_revisions(isolated, monkeypatch):
    monkeypatch.setattr(repo, "stamp", lambda: "2026-10-01T12:00:00Z")
    repo.upsert_events([event()])
    initial = db_export()
    assert len(initial["changes"]) == 1
    assert initial["changes"][0]["before"] == {}
    assert initial["changes"][0]["after"]["symbols"][0]["symbol"] == "AAA"
    original_exit = compile_calendar(initial, {}).exits
    monkeypatch.setattr(repo, "stamp", lambda: "2026-10-15T12:00:00Z")
    repo.upsert_events([event(symbols=[{"symbol": "BBB", "relation_type": "subject"}])])
    after = db_export()
    assert len(after["changes"]) == 2
    assert after["changes"][-1]["before"]["symbols"][0]["symbol"] == "AAA"
    assert compile_calendar(after, {}).exits == original_exit


def test_legacy_record_gets_current_anchor_not_invented_old_snapshot(isolated, monkeypatch):
    monkeypatch.setattr(repo, "stamp", lambda: "2026-10-01T12:00:00Z")
    repo.upsert_events([event()])
    with db.connection() as conn:
        conn.execute("DELETE FROM trading_calendar_changes")
        conn.commit()
    monkeypatch.setattr(repo, "stamp", lambda: "2026-10-15T12:00:00Z")
    repo.upsert_events([event()])
    result = db_export()
    assert len(result["changes"]) == 1
    assert result["changes"][0]["changed_at"] == "2026-10-15T12:00:00Z"
    assert result["changes"][0]["before"] == {}
    assert compile_calendar(result, {}).exits == []


def test_fetch_only_update_does_not_create_false_revision(isolated, monkeypatch):
    monkeypatch.setattr(repo, "stamp", lambda: "2026-10-01T12:00:00Z")
    repo.upsert_events([event(fetched_at="2026-10-01T12:00:00Z")])
    before = db_export()
    monkeypatch.setattr(repo, "stamp", lambda: "2026-10-02T12:00:00Z")
    repo.upsert_events([event(fetched_at="2026-10-02T12:00:00Z")])
    after = db_export()
    assert after["changes"] == before["changes"]
    assert compile_calendar(after, {}).exits == compile_calendar(before, {}).exits


def test_missing_snapshot_fields_do_not_inherit_todays_subject():
    current = event(updated_at="2026-10-15T12:00:00Z")
    broken = version(event())
    del broken["symbols"]
    result = compile_calendar(exported([current], [{"event_id": "e1",
        "changed_at": "2026-10-01T12:00:00Z", "before": {}, "after": broken}]), {})
    assert not result.exits


def test_revision_datetime_offsets_are_ordered_by_instant_not_text():
    original = event()
    b = event(symbols=[{"symbol": "BBB", "relation_type": "subject"}])
    c = event(symbols=[{"symbol": "CCC", "relation_type": "subject"}])
    result = compile_calendar(exported([c], [change(None, original, original["first_seen_at"]),
        change(b, c, "2026-10-12T10:30:00Z"),
        change(original, b, "2026-10-12T11:00:00+01:00")]), {})
    assert [e["subjects"] for e in result.exits] == [["CCC"]]


def test_cancellation_and_subject_relation_change_before_cutoff():
    original = event()
    for updates in ({"status": "cancelled"},
                    {"symbols": [{"symbol": "AAA", "relation_type": "related"}]}):
        later = {**original, **updates}
        result = compile_calendar(exported([later], [change(None, original, original["first_seen_at"]),
            change(original, later, "2026-10-12T12:00:00Z")]), {})
        assert not result.exits


def test_import_cannot_spoof_archive_time_and_reads_do_not_anchor(isolated, monkeypatch):
    monkeypatch.setattr(repo, "stamp", lambda: "2026-10-15T12:00:00Z")
    repo.upsert_events([event()])
    before = db_export()
    assert before["changes"][0]["changed_at"] == "2026-10-15T12:00:00Z"
    assert not compile_calendar(before, {}).exits
    repo.event_get("e1")
    repo.changes_get("e1")
    assert db_export() == before


def test_failed_write_rolls_back_event_and_full_revision(isolated, monkeypatch):
    monkeypatch.setattr(repo, "stamp", lambda: "2026-10-01T12:00:00Z")
    repo.upsert_events([event()])
    before = db_export()
    bad = event(title="metadata revision", importance=float("nan"))
    with pytest.raises(ValueError):
        repo.upsert_events([bad])
    assert db_export() == before


def test_partial_revision_blocks_old_full_state_until_new_complete_anchor():
    original = event()
    later = event(updated_at="2026-10-15T12:00:00Z")
    cancelled = {"event_id": "e1", "changed_at": "2026-10-12T12:00:00Z",
                 "before": {"status": "scheduled"}, "after": {"status": "cancelled"}}
    result = compile_calendar(exported([later], [change(None, original, original["first_seen_at"]),
        cancelled, change(None, later, later["updated_at"])]), {})
    assert not result.exits


def test_truncated_full_history_never_uses_before_payload_at_first_seen():
    original = event()
    later = event(updated_at="2026-10-15T12:00:00Z")
    result = compile_calendar(exported([later], [change(original, later, later["updated_at"])]), {})
    assert not result.exits
    assert result.audit["unknown_prior_version"] == 1


def test_missing_optional_source_fields_create_one_valid_anchor(isolated, monkeypatch):
    monkeypatch.setattr(repo, "stamp", lambda: "2026-10-01T12:00:00Z")
    item = event()
    for field in ("confidence", "detail", "symbols", "status"):
        item.pop(field)
    repo.upsert_events([item])
    repo.upsert_events([item])
    result = db_export()
    assert len(result["changes"]) == 1
    assert result["events"][0]["confidence"] == "unverified"
    assert result["changes"][0]["after"]["_calendar_snapshot_version"] == 2


def test_equal_timestamp_revision_order_preserves_unknown_barrier():
    original = event(updated_at="2026-10-12T12:00:00Z")
    later = event(updated_at="2026-10-15T12:00:00Z")
    partial = {"event_id": "e1", "changed_at": original["updated_at"],
               "before": {"status": "scheduled"}, "after": {"status": "cancelled"}}
    result = compile_calendar(exported([later], [change(None, original, original["updated_at"]),
        partial, change(None, later, later["updated_at"])]), {})
    assert not result.exits


def test_legacy_updated_time_alone_cannot_certify_metadata_availability():
    original = event()
    assert not compile_calendar({"events": [original], "changes": []}, {}).exits
    # Old persistence could delete a field without advancing updated_at.
    # The export, not the old update clock, is the first complete evidence.
    late_export = exported([original])
    assert not compile_calendar(late_export, {}).exits
    early_export = {**late_export, "exported_at": "2026-10-02T12:00:00Z"}
    result = compile_calendar(early_export, {})
    assert result.exits[0]["known_at"] == "2026-10-02T12:00:00+00:00"
