"""Registered, immutable batch provenance. No orders, scanning or backfilling."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import app_database as db
from market_calendar import is_trading_day, most_recent_session, session_close_et

VERSION = 'portfolio_forward_capture_v2'
ET = ZoneInfo('America/New_York')


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    try:
        out = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return out.astimezone(timezone.utc) if out.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _recipe():
    from portfolio_risk_research import PortfolioConfig
    import cost_model
    from portfolio_signal_evidence import signal_contract
    return dict(version=VERSION, scope='complete_declared_board_roster',
                simulation_enabled=False, config=asdict(PortfolioConfig()), costs=cost_model.cost_summary(),
                signal_evidence=signal_contract(), capture_budget_seconds=20)


def _manifest():
    root = Path(__file__).resolve().parent
    files = ('portfolio_forward_service.py', 'portfolio_risk_research.py', 'prediction_ledger_service.py',
             'priority_board_service.py', 'app_database.py', 'pullback_entry_research.py',
             'rebound_short_research.py', 'technical_signal_metrics.py', 'market_calendar.py',
             'pullback_validation.py', 'cost_model.py', 'trade_risk_rules.py',
             'portfolio_signal_evidence.py', 'market_data_service.py', 'entry_path_research.py',
             'corporate_action_research.py', 'config/corporate_actions_verified.json')
    return {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}


def _registration_matches(run):
    # JSON persists tuples as arrays; compare the canonical persisted form.
    return (_digest(json.loads(run['manifest_json'])) == _digest(_manifest())
            and _digest(json.loads(run['recipe_json'])) == _digest(_recipe()))


def _first_full_session(now):
    # Conservatively skip the registration's ET calendar day, even pre-open.
    day = now.astimezone(ET).date()+timedelta(days=1)
    while not is_trading_day(day):
        day += timedelta(days=1)
    return day.isoformat()


def register():
    """No caller-supplied date: registration can never be backdated."""
    recipe, manifest = _recipe(), _manifest()
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute('SELECT * FROM portfolio_forward_runs WHERE active=1').fetchone()
        if active:
            return dict(status='already_registered', run_id=active['run_id'],
                        first_signal_date=active['first_signal_date'])
        now = _now()
        ident = 'pf_'+_digest(dict(at=now.isoformat(), recipe=recipe, manifest=manifest))[:24]
        first = _first_full_session(now)
        conn.execute('INSERT INTO portfolio_forward_runs VALUES(?,?,?,?,?,?,?)',
                     (ident, now.isoformat(), first, 1, None, _json(recipe), _json(manifest)))
    return dict(status='registered', run_id=ident, registered_at=now.isoformat(), first_signal_date=first,
                simulation_enabled=False)


def pause():
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        count = conn.execute('UPDATE portfolio_forward_runs SET active=0, paused_at=? WHERE active=1',
                             (_now().isoformat(),)).rowcount
    return dict(status='paused' if count else 'not_registered')


def capture_board(board, signal_date):
    """Append a batch after predictions are durable; never alters predictions."""
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        run = conn.execute('SELECT * FROM portfolio_forward_runs WHERE active=1').fetchone()
        if not run:
            return dict(status='not_registered', recorded=False)
        board = json.loads(_json(board))
        now = _now()
        day = date.fromisoformat(signal_date)
        if signal_date < run['first_signal_date']:
            return dict(status='before_registration_window', recorded=False)
        if not is_trading_day(day) or most_recent_session(now).isoformat() != signal_date:
            return dict(status='not_latest_completed_session', recorded=False)
        generated = _stamp(board.get('generated_at'))
        if (generated is None or not session_close_et(day) <= generated <= now
                or generated < _stamp(run['registered_at']) or board.get('data_as_of') != signal_date):
            return dict(status='unverified_board_time_or_price_date', recorded=False)
        if not _registration_matches(run):
            return dict(status='registered_code_or_cost_changed', recorded=False)
        reference = board.get('ranking_reference')
        if (not board.get('snapshot_id') or not board.get('parameter_version') or not isinstance(reference, dict)
                or not reference.get('formula_version') or not reference.get('configuration')):
            return dict(status='missing_ranking_contract', recorded=False)
        primary = conn.execute('SELECT batch_id,roster_hash FROM portfolio_forward_batches '
                               'WHERE run_id=? AND signal_date=? AND is_primary=1', (run['run_id'], signal_date)).fetchone()
        if primary:
            return dict(status='already_recorded', recorded=False, batch_id=primary['batch_id'])
        picks = board.get('picks')
        count = board.get('n_candidates')
        if (not isinstance(picks, list) or type(count) is not int or count < 0
                or any(not isinstance(p, dict) for p in picks)):
            return dict(status='invalid_or_empty_declared_roster', recorded=False)
        horizon = int(board.get('horizon_days') or 10)
        # A single indexed query, not one query per ticker.
        saved = {r['symbol']: dict(r) for r in conn.execute(
            'SELECT prediction_id,symbol,rank,mode,created_at,evaluation_version,forecast_json FROM predictions '
            'WHERE as_of_date=? AND horizon_days=? AND signal_type=?', (signal_date, horizon, 'pullback_hv'))}
        flags, members, seen = Counter(), [], set()
        if count != len(picks):
            flags['declared_roster_truncated_or_mismatched'] += 1
        for rank, p in enumerate(picks, 1):
            symbol = str(p.get('symbol') or '').strip().upper()
            reasons = []
            if not symbol or symbol in seen:
                reasons.append('invalid_or_duplicate_symbol')
            seen.add(symbol)
            item = dict(symbol=symbol, rank=rank, prediction_id=None, forecast_sha256=None)
            if p.get('price_as_of') != signal_date:
                reasons.append('stale_or_unverified_price')
                item['unlinked_evidence'] = p
            else:
                r = saved.get(symbol)
                created = _stamp(r.get('created_at')) if r else None
                try:
                    forecast = json.loads(r['forecast_json']) if r else {}
                except (TypeError, ValueError):
                    forecast = {}
                contract = forecast.get('validation_contract', {}) if isinstance(forecast, dict) else {}
                matches = (r and r['mode'] == 'live' and r['evaluation_version'] == 'recorded_open_v3'
                    and created is not None and _stamp(run['registered_at']) <= generated <= created <= now
                    and r['rank'] == rank and isinstance(forecast, dict) and forecast.get('features') == p
                    and forecast.get('snapshot_id') == board.get('snapshot_id')
                    and isinstance(contract, dict) and contract.get('board_generated_at') == board.get('generated_at')
                    and contract.get('ranking_version') == board.get('parameter_version')
                    and contract.get('ranking_reference') == reference)
                if matches:
                    item.update(prediction_id=r['prediction_id'], forecast_sha256=_digest(forecast))
                    if not contract.get('metadata_complete'):
                        reasons.append('incomplete_probability_metadata')
                else:
                    reasons.append('missing_or_conflicting_immutable_prediction')
                    item['unlinked_evidence'] = p
            for reason in reasons:
                flags[reason] += 1
            item['quality_flags'] = reasons
            item['execution_eligibility'] = 'not_evaluated_capture_only'
            members.append(item)
        contract = dict(parameter_version=board['parameter_version'], ranking_reference=reference,
                        snapshot_id=board.get('snapshot_id'), generated_at=board['generated_at'],
                        horizon_days=horizon, source=board.get('source'))
        digest = _digest(dict(members=members, contract=contract, declared_count=count))
        complete = not any(flags[k] for k in ('declared_roster_truncated_or_mismatched',
                       'invalid_or_duplicate_symbol', 'missing_or_conflicting_immutable_prediction'))
        linked = sum(m['prediction_id'] is not None for m in members)
        quality = dict(flags=dict(flags), linked_prediction_count=linked,
                       members_with_quality_flags=sum(bool(m['quality_flags']) for m in members))
        ident = run['run_id']+':'+signal_date+':'+digest[:24]
        inserted = conn.execute('INSERT OR IGNORE INTO portfolio_forward_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (ident, run['run_id'], signal_date, now.isoformat(), board['generated_at'], board.get('snapshot_id'),
             digest, count, len(members), int(complete), _json(quality), _json(members), _json(contract))).rowcount
    return dict(status='recorded' if complete else 'incomplete_batch_archived', recorded=bool(inserted),
                batch_id=ident, roster_complete=complete, member_count=len(members), declared_count=count,
                quality_flags=dict(flags), linked_prediction_count=linked, simulation_enabled=False)


def status():
    """Only local queries; no scan, market API, LLM or automatic registration."""
    with db.connection() as conn:
        run = conn.execute('SELECT * FROM portfolio_forward_runs ORDER BY active DESC,registered_at DESC LIMIT 1').fetchone()
        if not run:
            return dict(status='not_registered', simulation_enabled=False, collected_signal_days=0)
        batches = [dict(r) for r in conn.execute(
            'SELECT b.batch_id,b.signal_date,b.recorded_at,b.declared_count,b.member_count,b.is_primary,b.quality_json, '
            'e.recorded_at AS evidence_recorded_at FROM portfolio_forward_batches b '
            'LEFT JOIN portfolio_forward_evidence e ON e.batch_id=b.batch_id '
            'WHERE b.run_id=? ORDER BY b.signal_date DESC,b.recorded_at DESC', (run['run_id'],))]
    collected = {b['signal_date'] for b in batches if b['is_primary']}
    last = most_recent_session(_now())
    if run['paused_at']:
        last = min(last, most_recent_session(_stamp(run['paused_at'])))
    day, expected = date.fromisoformat(run['first_signal_date']), []
    while day <= last:
        if is_trading_day(day):
            expected.append(day.isoformat())
        day += timedelta(days=1)
    missing = [d for d in expected if d not in collected]
    for b in batches:
        quality = json.loads(b.pop('quality_json'))
        b['quality_flags'] = quality['flags']
        b['linked_prediction_count'] = quality['linked_prediction_count']
        b['members_with_quality_flags'] = quality['members_with_quality_flags']
        b['roster_complete'] = bool(b.pop('is_primary'))
    evidence_days = {b['signal_date'] for b in batches if b['roster_complete'] and b['evidence_recorded_at']}
    compatible = _registration_matches(run)
    return dict(status='registered' if run['active'] else 'paused', run_id=run['run_id'],
                registered_at=run['registered_at'], first_signal_date=run['first_signal_date'],
                capture_ready=bool(run['active']) and compatible,
                registration_block_reason=None if compatible else 'registered_code_or_cost_changed',
                collected_signal_days=len(collected), missing_signal_days=len(missing),
                evidence_collected_signal_days=len(evidence_days),
                missing_dates=missing[-120:], recent_batches=batches[:20], simulation_enabled=False,
                note='仅归档事前排名和证据；尚未生成模拟成交或收益，不是期权胜率。')


def batch_detail(batch_id):
    with db.connection() as conn:
        row = conn.execute('SELECT * FROM portfolio_forward_batches WHERE batch_id=?', (batch_id,)).fetchone()
        if not row:
            return None
        contract = json.loads(row['contract_json'])
        saved = {r['prediction_id']: r['forecast_json'] for r in conn.execute(
            'SELECT prediction_id,forecast_json FROM predictions WHERE as_of_date=? AND horizon_days=? AND signal_type=?',
            (row['signal_date'], contract['horizon_days'], 'pullback_hv'))}
    members = json.loads(row['members_json'])
    for item in members:
        if item['prediction_id'] is None:
            continue
        try:
            forecast = json.loads(saved.get(item['prediction_id']) or 'null')
            intact = isinstance(forecast, dict) and _digest(forecast) == item['forecast_sha256']
        except (TypeError, ValueError):
            forecast, intact = None, False
        item['reference_intact'] = intact
        item['forecast'] = forecast if intact else None
    return dict(batch_id=row['batch_id'], run_id=row['run_id'], signal_date=row['signal_date'],
                recorded_at=row['recorded_at'], roster_complete=bool(row['is_primary']),
                contract=contract, quality=json.loads(row['quality_json']), members=members,
                simulation_enabled=False)


def _calendar_evidence(now):
    """Archive only locally stored event facts; no inference of full coverage."""
    with db.connection() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trading_calendar_events'").fetchone()
        if not exists:
            return dict(status='unavailable', coverage='unknown', events=[])
        rows = conn.execute("SELECT payload_json,first_seen_at,updated_at FROM trading_calendar_events "
                            "WHERE category IN ('macro','earnings')").fetchall()
    events, omitted = [], 0
    for row in rows:
        first, updated = _stamp(row['first_seen_at']), _stamp(row['updated_at'])
        if first is None or updated is None or not first <= updated <= now:
            omitted += 1
            continue
        try:
            item = json.loads(row['payload_json'])
            event = {k: item.get(k) for k in ('event_id', 'category', 'source', 'date', 'end_date', 'start_utc',
                'title', 'status', 'confidence', 'time_precision', 'symbols', 'source_url')}
            event.update(first_seen_at=row['first_seen_at'], version_known_at=row['updated_at'])
            events.append(event)
        except (ValueError, TypeError, AttributeError):
            omitted += 1
    return dict(status='archived' if events else 'unavailable', coverage='unknown',
                observed_at=now.isoformat(), events=events, omitted_invalid_versions=omitted,
                note='仅当时本地已归档版本；未覆盖不代表没有财报或宏观事件。')


def capture_signal_evidence(batch_id):
    """Bounded, local-only evidence capture after the roster transaction ends."""
    from market_data_service import read_daily_cache_snapshot
    from portfolio_signal_evidence import decision_evidence, entry_timing, signal_contract
    started = time.monotonic()
    with db.connection() as conn:
        existing = conn.execute('SELECT 1 FROM portfolio_forward_evidence WHERE batch_id=?', (batch_id,)).fetchone()
        if existing:
            return dict(status='already_recorded', recorded=False, batch_id=batch_id)
        run = conn.execute('SELECT * FROM portfolio_forward_runs WHERE active=1').fetchone()
    if not run:
        return dict(status='not_registered', recorded=False)
    batch = batch_detail(batch_id)
    if not batch or batch['run_id'] != run['run_id'] or not batch['roster_complete']:
        return dict(status='complete_registered_batch_required', recorded=False)
    if not _registration_matches(run):
        return dict(status='registered_code_or_cost_changed', recorded=False)
    now = _now()
    if batch['signal_date'] != most_recent_session(now).isoformat() or _stamp(batch['recorded_at']) > now:
        return dict(status='not_current_captured_batch', recorded=False)
    calendar = _calendar_evidence(now)
    members, counts = [], Counter()
    budget = json.loads(run['recipe_json']).get('capture_budget_seconds', 20)
    for member in batch['members']:
        result = dict(symbol=member['symbol'], rank=member['rank'], status='DATA_UNKNOWN')
        if time.monotonic()-started >= budget:
            result['reason'] = 'capture_time_budget_exhausted'
        elif (not member.get('reference_intact')
              or 'stale_or_unverified_price' in member.get('quality_flags', [])):
            result['reason'] = 'unverified_forecast_reference_or_price_date'
        else:
            try:
                frame, provenance = read_daily_cache_snapshot(member['symbol'])
                observed = _now()
                result.update(decision_evidence(member['symbol'], frame, batch['signal_date']))
                result['price_evidence'] = {**provenance, 'observed_at': observed.isoformat()}
            except Exception as exc:
                result.update(reason='cached_evidence_unavailable', error_type=type(exc).__name__)
        counts[result['status']] += 1
        members.append(result)
    # Availability is completion time, never a file's old mtime or the session close.
    finished = _now()
    payload = dict(version=VERSION, batch_id=batch_id, signal_date=batch['signal_date'],
                   contract=signal_contract(), members=members, counts=dict(counts), calendar=calendar,
                   unprocessed_members=sum(m.get('reason') == 'capture_time_budget_exhausted' for m in members),
                   usable_members=sum(m['status'] != 'DATA_UNKNOWN' for m in members),
                   timing=entry_timing(batch['signal_date'], finished), simulation_enabled=False,
                   elapsed_ms=round((time.monotonic()-started)*1000, 2))
    raw, digest = _json(payload), _digest(payload)
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute('SELECT run_id FROM portfolio_forward_runs WHERE active=1').fetchone()
        if not active or active['run_id'] != run['run_id']:
            return dict(status='registration_paused_during_capture', recorded=False)
        if not _registration_matches(run):
            return dict(status='registered_code_or_cost_changed', recorded=False)
        written = conn.execute('INSERT OR IGNORE INTO portfolio_forward_evidence VALUES(?,?,?,?)',
                               (batch_id, finished.isoformat(), digest, raw)).rowcount
    return dict(status='recorded' if written else 'already_recorded', recorded=bool(written),
                batch_id=batch_id, counts=dict(counts), timing=payload['timing'], simulation_enabled=False)


def signal_evidence_detail(batch_id):
    with db.connection() as conn:
        row = conn.execute('SELECT payload_json,payload_sha256 FROM portfolio_forward_evidence WHERE batch_id=?',
                           (batch_id,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row['payload_json'])
        intact = _digest(payload) == row['payload_sha256']
    except (ValueError, TypeError):
        payload, intact = None, False
    return dict(batch_id=batch_id, reference_intact=intact, evidence=payload if intact else None)
