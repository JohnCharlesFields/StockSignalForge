"""Test point-in-time S&P 500 membership reconstruction (no network). ASCII-only."""

from __future__ import annotations

from datetime import date

from scripts.build_pit_snapshots import membership_as_of


def _changes():
    # Newest first (as the parser sorts them). Each: an Added joined / Removed dropped on a date.
    return [
        {"date": date(2024, 6, 1), "added": "NEW2", "removed": "OLD2"},
        {"date": date(2024, 1, 1), "added": "NEW1", "removed": "OLD1"},
    ]


def test_membership_after_all_changes_equals_current():
    current = {"AAPL", "NEW1", "NEW2"}
    # As of after the latest change: membership == current.
    assert membership_as_of(date(2024, 7, 1), current, _changes()) == current


def test_membership_unwinds_one_change():
    current = {"AAPL", "NEW1", "NEW2"}
    # Between the two changes: NEW2 not yet added, OLD2 still a member.
    members = membership_as_of(date(2024, 3, 1), current, _changes())
    assert "NEW2" not in members
    assert "OLD2" in members
    assert "NEW1" in members  # this change already happened by 2024-03
    assert "OLD1" not in members


def test_membership_unwinds_all_changes():
    current = {"AAPL", "NEW1", "NEW2"}
    # Before both changes: both OLD names members, neither NEW present.
    members = membership_as_of(date(2023, 12, 1), current, _changes())
    assert members == {"AAPL", "OLD1", "OLD2"}


def test_boundary_is_strictly_after():
    current = {"AAPL", "NEW1", "NEW2"}
    # On the exact change date, that change is considered already in effect (not unwound).
    members = membership_as_of(date(2024, 6, 1), current, _changes())
    assert "NEW2" in members and "OLD2" not in members
