import json

import pytest

from scripts import screening_framework_v2_optimized as sf
from scripts import validate_causal_calibration as calibration
from datetime import date
from scripts.build_pit_snapshots import change_grid, membership_as_of


def test_future_capture_and_naive_capture_cannot_be_historical_members(tmp_path, monkeypatch):
    root = tmp_path/'universe_snapshots'
    folder = root/'spx'
    folder.mkdir(parents=True)
    monkeypatch.setitem(sf.CONFIG, 'universe_snapshot_dir', str(root))
    for captured in ('2030-01-01T00:00:00Z', '2024-01-01T00:00:00'):
        (folder/'2024-01-02.json').write_text(json.dumps(dict(effective_date='2024-01-02',
            captured_at=captured, tickers=['AAA'], source='live:fixture')))
        assert sf.load_universe_snapshot('spx','2024-01-03') is None


def test_same_effective_date_is_append_only_and_historical_selection_is_stable(tmp_path, monkeypatch):
    monkeypatch.setitem(sf.CONFIG, 'universe_snapshot_dir', str(tmp_path))
    a = sf.archive_universe_snapshot('spx', ['AAA'], 'live:fixture', '2024-01-02')
    raw = a.read_bytes()
    b = sf.archive_universe_snapshot('spx', ['BBB'], 'live:fixture', '2024-01-02')
    assert a != b and a.read_bytes() == raw
    assert sf.load_universe_snapshot('spx','2024-01-03') is None


def test_historical_request_without_safe_archive_cannot_fall_back_to_today(tmp_path, monkeypatch):
    monkeypatch.setitem(sf.CONFIG, 'universe_snapshot_dir', str(tmp_path))
    def forbidden():
        pytest.fail('historical request must not fetch current constituents')
    with pytest.raises(ValueError, match='snapshot'):
        sf.resolve_snapshot_aware_universe('spx','S&P',forbidden,'2020-01-01',False)


def test_membership_change_effective_mid_month_is_not_delayed_to_next_month():
    changes = [dict(date=date(2024,6,15), added='NEW', removed='OLD')]
    grid = change_grid(date(2024,6,1), date(2024,6,30), changes)
    selected = max(d for d in grid if d <= date(2024,6,20))
    assert membership_as_of(selected, {'KEEP','NEW'}, changes) == {'KEEP','NEW'}
    with pytest.raises(ValueError):
        change_grid(date(2024,6,1), date(2024,6,30), [])


def test_versions_effective_same_day_choose_only_observed_version(tmp_path, monkeypatch):
    folder = tmp_path/'spx'
    folder.mkdir()
    monkeypatch.setitem(sf.CONFIG, 'universe_snapshot_dir', str(tmp_path))
    for name, captured, symbol in [('a','2024-01-02T12:00:00Z','AAA'), ('b','2024-01-04T12:00:00Z','BBB')]:
        (folder/(name+'.json')).write_text(json.dumps(dict(effective_date='2024-01-02',
            captured_at=captured, tickers=[symbol], source='live:fixture')))
    assert sf.load_universe_snapshot('spx','2024-01-03')[0] == ['AAA']
    assert sf.load_universe_snapshot('spx','2024-01-05')[0] == ['BBB']


def test_unverified_pit_builder_refuses_activation_before_fetch_or_database_write(monkeypatch):
    import sys
    from scripts import build_databento_pit_calibration as builder
    monkeypatch.setattr(sys, 'argv', ['builder', '--make-active'])
    def forbidden(*args, **kwargs):
        pytest.fail('unverified activation must fail before external IO')
    monkeypatch.setattr(builder, '_fetch_tables', forbidden)
    monkeypatch.setattr(builder, '_load_databento_panel', forbidden)
    monkeypatch.setattr(builder, 'signal_calibration_upsert', forbidden)
    with pytest.raises(ValueError, match='cannot_activate'):
        builder.main()
