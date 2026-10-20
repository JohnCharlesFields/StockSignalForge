"""Offline, version-aware previous-close exits. Does not certify event coverage."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
import math

import pandas as pd

from market_calendar import previous_trading_day, session_close_et
from trading_calendar_history import SNAPSHOT_KEY, is_full_snapshot, semantic_payload
from corporate_action_research import shares_between

VERSION = 'event_exit_complete_versions_v2'


def _instant(value):
    try:
        stamp = pd.Timestamp(value)
        return stamp.tz_convert('UTC') if stamp.tzinfo is not None and not pd.isna(stamp) else None
    except (ValueError, TypeError):
        return None


def _kind(item):
    if item.get('category') == 'earnings':
        return 'EARNINGS'
    if item.get('category') != 'macro' or item.get('confidence') != 'official':
        return None
    text = (str(item.get('title', ''))+' '+str((item.get('detail') or {}).get('summary', ''))).lower()
    if 'fomc' in text and not any(s in text for s in ['minutes', '纪要']):
        return 'FOMC'
    if 'cpi' in text or 'consumer price index' in text or '消费者价格' in text:
        return 'CPI'
    if 'pce' in text or 'personal income and outlays' in text or '个人消费' in text:
        return 'PCE'
    if 'employment situation' in text or '非农' in text:
        return 'NFP'
    if 'gdp' in text or 'gross domestic product' in text or '国内生产总值' in text:
        return 'GDP'
    return None


@dataclass
class EventExitSchedule:
    exits: list[dict]
    audit: dict


def compile_calendar(export, announcements):
    audit = Counter()
    versions = defaultdict(list)
    changes = defaultdict(list)
    for change in export.get('changes', []):
        changes[change['event_id']].append(change)
    for item in export.get('events', []):
        kind = _kind(item)
        audit['source_'+(kind or 'unrecognized_macro')] += 1
        first = _instant(item.get('first_seen_at'))
        if first is None:
            audit['unknown_first_seen'] += 1
            continue
        ident = item['event_id']
        full = []
        history_states = []
        partial_times = []
        invalid_time = False
        for change in changes[ident]:
            known = _instant(change.get('changed_at'))
            if known is None or known < first:
                audit['invalid_revision_time'] += 1
                invalid_time = True
                continue
            state = change.get('after')
            if is_full_snapshot(state) and state.get('event_id') == ident:
                value = (known, {k: v for k, v in state.items() if k != SNAPSHOT_KEY})
                full.append(value)
                history_states.append(value)
            else:
                partial_times.append(known)
                history_states.append((known, dict(event_id=ident, category='unknown', status='unknown')))
                audit['legacy_partial_revisions'] += 1
        full.sort(key=lambda pair: pair[0])
        if invalid_time:
            audit['unusable_revision_history'] += 1
            continue
        # An incomplete revision is an unknown interval, not permission to keep
        # using the previous complete state until a later complete anchor.
        versions[ident].extend(history_states)
        if full:
            audit['complete_version_events'] += 1
            audit['complete_versions'] += len(full)
            # No reconstruction before the first complete snapshot, including
            # legacy records first anchored long after their original discovery.
            if full[0][0] > first:
                audit['unknown_prior_version'] += 1
            if (semantic_payload(full[-1][1]) == semantic_payload(item)
                    and not any(t >= full[-1][0] for t in partial_times)):
                continue
            audit['unarchived_current_payload'] += 1
        # Legacy updated_at did not reliably track removed metadata fields.
        # Only the actual complete export proves this payload was observed.
        observed = _instant(export.get('exported_at'))
        updated = _instant(item.get('updated_at'))
        fetched = _instant(item.get('fetched_at')) if item.get('fetched_at') else updated
        if observed is None or updated is None or fetched is None:
            audit['unknown_current_version_time'] += 1
            if full:
                versions[ident].append((full[-1][0], dict(event_id=ident, category='unknown', status='unknown')))
            continue
        known = max(observed, first, updated, fetched, *partial_times)
        if full:
            known = max(known, full[-1][0])
        versions[ident].append((known, dict(item)))
        audit['legacy_current_only'] += 1
        if known > first:
            audit['unknown_prior_version'] += 1
    for source in announcements.get('announcements', []):
        known = _instant(source.get('known_at'))
        if known is None:
            raise ValueError('invalid_announcement_time')
        for day in source['decision_dates']:
            ident = 'advance:FOMC:'+day
            versions[ident].append((known, dict(event_id=ident, date=day, category='macro', title='FOMC decision',
                confidence='official', status='scheduled', source_url=source['source_url'], symbols=[])))
            audit['advance_FOMC'] += 1
    exits = []
    seen = set()
    for ident, history in versions.items():
        history.sort(key=lambda pair: pair[0])
        for known, item in history:
            kind = _kind(item)
            if kind is None:
                continue
            try:
                day = date.fromisoformat(str(item.get('end_date') if kind == 'FOMC' and item.get('end_date') else item.get('date')))
            except ValueError:
                audit['invalid_event_date'] += 1
                continue
            exit_day = previous_trading_day(day)
            cutoff = pd.Timestamp(session_close_et(exit_day)-timedelta(minutes=15)).tz_convert('UTC')
            available = [(stamp, row) for stamp, row in history if stamp <= cutoff]
            if not available:
                audit['versions_known_after_exit_cutoff'] += 1
                continue
            effective_time, effective = available[-1]
            if effective_time != known or effective != item:
                continue
            if item.get('status') not in {'scheduled', 'estimated', 'confirmed'}:
                audit['inactive_or_reported_version'] += 1
                continue
            subjects = sorted({str(s['symbol']).upper() for s in item.get('symbols', [])
                               if s.get('relation_type') == 'subject' and s.get('symbol')}) if kind == 'EARNINGS' else []
            if kind == 'EARNINGS' and not subjects:
                audit['earnings_missing_subject'] += 1
                continue
            key = (kind, str(day), str(exit_day), tuple(subjects))
            if key in seen:
                continue
            seen.add(key)
            exits.append(dict(event_id=ident, kind=kind, event_date=str(day), exit_date=str(exit_day),
                known_at=known.isoformat(), decision_cutoff=cutoff.isoformat(), subjects=subjects,
                confidence=item.get('confidence'), source_url=item.get('source_url')))
    exits.sort(key=lambda row: (row['exit_date'], row['event_id']))
    return EventExitSchedule(exits, dict(audit))


def apply_event_exit(trade, frame, schedule, side, view='combined'):
    if side not in {'LONG', 'SHORT'} or view not in {'combined', 'fomc_only', 'earnings_only'}:
        raise ValueError('invalid_event_exit_view')
    candidates = []
    for event in schedule.exits:
        # Original exits are at the open or intraday, strictly before this close.
        if not trade['entry_date'] <= event['exit_date'] < trade['exit_date']:
            continue
        if view == 'fomc_only' and event['kind'] != 'FOMC':
            continue
        if view == 'earnings_only' and event['kind'] != 'EARNINGS':
            continue
        if event['kind'] == 'EARNINGS' and str(trade['symbol']).upper() not in event['subjects']:
            continue
        candidates.append(event)
    if not candidates:
        return dict(trade)
    day = candidates[0]['exit_date']
    matches = [event for event in candidates if event['exit_date'] == day]
    row = frame.loc[frame.index.strftime('%Y-%m-%d') == day]
    if len(row) != 1:
        raise ValueError('missing_event_exit_close')
    close = float(row.Close.iloc[0])
    if not math.isfinite(close) or close <= 0:
        raise ValueError('missing_event_exit_close')
    held = int(((frame.index >= pd.Timestamp(trade['entry_date'])) & (frame.index <= pd.Timestamp(day))).sum()-1)
    multiplier = shares_between(trade['symbol'], trade['entry_date'], day)
    gross = (close*multiplier/trade['entry_price']-1)*(1 if side == 'LONG' else -1)
    gross_key = 'gross_return' if side == 'LONG' else 'gross_directional_return'
    return {**trade, gross_key:gross, 'share_multiplier':multiplier, 'exit_date':day, 'exit_price':close, 'exit_reason':'event_previous_close', 'held_sessions':held,
        'event_exit':True, 'event_ids':[event['event_id'] for event in matches],
        'event_types':sorted({event['kind'] for event in matches}), 'event_dates':sorted({event['event_date'] for event in matches}),
        'event_known_at':[event['known_at'] for event in matches],
        'event_gross_return':gross}
