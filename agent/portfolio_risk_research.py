"""Offline portfolio overlay on archived entries, with causal daily phases."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import hashlib
import math

import pandas as pd

import cost_model
from backtest.engines.global_equity import GlobalEquityEngine
from backtest.metrics import calc_metrics
from backtest.models import Position
from market_calendar import is_trading_day
from pullback_validation import shift_session
from trade_risk_rules import RISK_PER_TRADE, MAX_POSITION_RATIO

VERSION = 'archived_portfolio_risk_v1'
PLAN_FIELDS = ('id', 'symbol', 'side', 'decision_date', 'entry_date', 'stop', 'adv')


@dataclass(frozen=True)
class PortfolioConfig:
    initial_cash: float = 100000.
    risk_per_trade: float = RISK_PER_TRADE
    max_position_ratio: float = MAX_POSITION_RATIO
    total_risk: float = .03
    max_positions: int = 5
    fixed_weight: float = .20
    borrow_rate: float = .05
    hold_sessions: int = 8

    def __post_init__(self):
        values = (self.initial_cash, self.risk_per_trade, self.max_position_ratio,
                  self.total_risk, self.fixed_weight, self.borrow_rate)
        if not all(math.isfinite(v) for v in values) or self.initial_cash <= 0:
            raise ValueError('invalid_portfolio_config')
        if not (0 < self.risk_per_trade <= self.total_risk <= 1
                and 0 < self.max_position_ratio <= 1 and 0 < self.fixed_weight <= 1
                and 0 <= self.borrow_rate <= 1):
            raise ValueError('invalid_portfolio_limits')
        if type(self.max_positions) is not int or self.max_positions < 1 or type(self.hold_sessions) is not int or self.hold_sessions < 1:
            raise ValueError('invalid_portfolio_integer_limits')


def entry_plans(rows):
    plans = []
    for row in rows:
        trade = row['variants']['baseline']
        if trade is None:
            continue
        plan = {k: trade[k] for k in PLAN_FIELDS}
        if plan['side'] not in ('LONG', 'SHORT') or shift_session(pd.Timestamp(plan['decision_date']).date(), 1).isoformat() != plan['entry_date']:
            raise ValueError('entry_not_next_session_after_completed_decision')
        if not all(math.isfinite(float(plan[k])) and plan[k] > 0 for k in ('stop', 'adv')):
            raise ValueError('invalid_archived_plan')
        plans.append(plan)
    if len({p['id'] for p in plans}) != len(plans):
        raise ValueError('duplicate_archived_plan_id')
    return plans


def priority(plan):
    return hashlib.sha256((VERSION+':'+plan['id']).encode()).hexdigest()


def exit_at(bar, meta, age, phase, events=()):
    sign = 1 if meta['side'] == 'LONG' else -1
    if phase == 'open':
        if sign*(bar.Open-meta['stop']) <= 0:
            return float(bar.Open), 'gap_stop'
        if age >= meta['hold_sessions']:
            return float(bar.Open), 'time_exit_at_open'
        if sign*(bar.Open-meta['target']) >= 0:
            return float(bar.Open), 'gap_target'
    elif phase == 'intraday':
        if (bar.Low <= meta['stop'] if sign == 1 else bar.High >= meta['stop']):
            return float(meta['stop']), 'stop_first_conservative'
        if (bar.High >= meta['target'] if sign == 1 else bar.Low <= meta['target']):
            return float(meta['target']), 'target'
    elif phase == 'close':
        if any(e['kind'] != 'EARNINGS' or meta['symbol'] in e['subjects'] for e in events):
            return float(bar.Close), 'event_previous_close'
    else:
        raise ValueError('invalid_portfolio_phase')
    return None


class ArchivedPortfolio(GlobalEquityEngine):
    """Reuse the engine's collateral/PnL book without its weight-rebalance loop."""

    def __init__(self, config=PortfolioConfig()):
        super().__init__({'initial_cash': config.initial_cash, 'leverage': 1.}, market='us')
        self.rules = config
        self.meta = {}
        self.entries = []
        self.rejects = Counter()
        self.closed = []

    def calc_commission(self, size, price, _direction, is_open):
        m = self.meta[self._active_symbol]
        return size*m['entry_price']*m['one_way_cost']

    def close(self, symbol, price, day, reason):
        m, p = self.meta[symbol], self.positions[symbol]
        self._active_symbol = symbol
        fee = self.calc_commission(p.size, price, p.direction, False)
        super()._close_position(symbol, price, pd.Timestamp(day), reason)
        gross = self.trades[-1].pnl
        net = gross-p.entry_commission-fee-m['borrow_paid']
        self.trades[-1] = replace(self.trades[-1], pnl=net,
                                 pnl_pct=net/(p.size*p.entry_price)*100,
                                 commission=p.entry_commission+fee)
        self.closed.append(dict(id=m['id'], symbol=symbol, side=m['side'], entry_date=str(p.entry_time.date()),
            exit_date=day, entry_price=p.entry_price, exit_price=price, qty=p.size, net_pnl=net,
            fees=p.entry_commission+fee, borrow=m['borrow_paid'], exit_reason=reason,
            initial_risk=p.size*m['risk_per_share']))
        del self.meta[symbol]

    def equity_at(self, bars, field):
        return self.capital+sum(self._calc_margin(s, p.size, p.entry_price, p.leverage)
            + self._calc_pnl(s, p.direction, p.size, p.entry_price, float(bars[s][field]))
            for s, p in self.positions.items())

    def allocate(self, plan, bar, marks, day, sizing):
        symbol = plan['symbol']
        if symbol in self.positions:
            self.rejects['already_held'] += 1
            return
        if len(self.positions) >= self.rules.max_positions:
            self.rejects['position_slots'] += 1
            return
        sign, price = (1 if plan['side'] == 'LONG' else -1), float(bar.Open)
        risk = sign*(price-plan['stop'])
        target = price+sign*2*risk
        if risk <= 0 or target <= 0:
            self.rejects['invalid_open_structure'] += 1
            return
        equity = self.equity_at(marks, 'Open')
        if equity <= 0:
            raise ValueError('nonpositive_open_equity')
        used_risk = sum(p.size*self.meta[s]['risk_per_share'] for s, p in self.positions.items())
        gross = sum(p.size*float(marks[s].Open) for s, p in self.positions.items())
        cost = cost_model.equity_round_trip_cost(price, plan['adv'])/2
        r = self.rules
        reserve = sum(p.size*p.entry_price*r.borrow_rate*max(0, (pd.Timestamp(m['borrow_deadline'])-pd.Timestamp(day)).days)/365
                      for s, p in self.positions.items() for m in [self.meta[s]] if p.direction == -1)
        deadline = shift_session(pd.Timestamp(day).date(), r.hold_sessions).isoformat()
        borrow_buffer = r.borrow_rate*(pd.Timestamp(deadline)-pd.Timestamp(day)).days/365 if sign == -1 else 0.
        desired = equity*r.fixed_weight/price if sizing == 'fixed_notional' else equity*r.risk_per_trade/risk
        # Include entry fees in the post-fill equity denominators of all caps.
        raw = min(desired, equity*r.risk_per_trade/(risk+r.risk_per_trade*price*cost),
                  equity*r.max_position_ratio/(price*(1+r.max_position_ratio*cost)),
                  max(0., r.total_risk*equity-used_risk)/(risk+r.total_risk*price*cost),
                  max(0., equity-gross)/(price*(1+cost)), max(0., self.capital-reserve)/(price*(1+cost+borrow_buffer)))
        qty = math.floor(max(0., raw))
        if qty < 1:
            self.rejects['risk_gross_cash_or_minimum_share'] += 1
            return
        fee = qty*price*cost
        self.meta[symbol] = dict(**plan, entry_price=price, target=target, risk_per_share=risk,
                                 one_way_cost=cost, borrow_paid=0., last_borrow_day=day,
                                 hold_sessions=r.hold_sessions, borrow_deadline=deadline)
        self.capital -= self._calc_margin(symbol, qty, price, 1.)+fee
        self.positions[symbol] = Position(symbol, sign, price, pd.Timestamp(day), qty,
                                         entry_bar_idx=self._bar_idx, entry_commission=fee)
        post = equity-fee
        risk_fraction = (used_risk+qty*risk)/post
        gross_fraction = (gross+qty*price)/post
        assert risk_fraction <= r.total_risk+1e-10 and gross_fraction <= 1+1e-10 and self.capital >= -1e-7
        self.entries.append(dict(id=plan['id'], symbol=symbol, side=plan['side'], date=day, qty=qty,
            price=price, fee=fee, equity_before=equity, equity_after=post,
            trade_risk_fraction=qty*risk/post, total_risk_fraction=risk_fraction,
            gross_fraction=gross_fraction, adv_participation=qty*price/plan['adv']))


def replay_portfolio(data, plans, days, schedule, *, mode='LONG', sizing='equal_risk',
                     config=PortfolioConfig(), reverse_priority=False, require_closed=True, benchmark_returns=None):
    if mode not in ('LONG', 'SHORT', 'BOTH') or sizing not in ('fixed_notional', 'equal_risk'):
        raise ValueError('invalid_portfolio_policy')
    days = list(days)
    if len(set(days)) != len(days) or days != sorted(days) or any(not is_trading_day(pd.Timestamp(d).date()) for d in days):
        raise ValueError('invalid_portfolio_calendar')
    if any(shift_session(pd.Timestamp(a).date(), 1) != pd.Timestamp(b).date() for a, b in zip(days, days[1:])):
        raise ValueError('missing_session_in_portfolio_calendar')
    frames = {}
    for symbol, frame in data.items():
        f = frame.copy()
        f.index = pd.DatetimeIndex([pd.Timestamp(d.date()) for d in f.index])
        if f.index.has_duplicates or not f.index.is_monotonic_increasing:
            raise ValueError('invalid_portfolio_quote_dates:'+symbol)
        frames[symbol] = f
    entries, exits = defaultdict(list), defaultdict(list)
    for plan in plans:
        if mode == 'BOTH' or mode == plan['side']:
            entries[plan['entry_date']].append(plan)
    for event in schedule.exits:
        exits[event['exit_date']].append(event)
    book, snapshots = ArchivedPortfolio(config), []

    def quote(symbol, day):
        stamp = pd.Timestamp(day)
        if symbol not in frames or stamp not in frames[symbol].index:
            raise ValueError('missing_active_or_entry_quote:'+symbol+':'+day)
        b = frames[symbol].loc[stamp]
        if not math.isfinite(float(b.Open)) or b.Open <= 0:
            raise ValueError('invalid_active_or_entry_open:'+symbol+':'+day)
        return b

    for i, day in enumerate(days):
        book._bar_idx = i
        bars = {s: quote(s, day) for s in book.positions}
        for s, p in list(book.positions.items()):
            m = book.meta[s]
            elapsed = (pd.Timestamp(day)-pd.Timestamp(m['last_borrow_day'])).days
            charge = p.size*p.entry_price*config.borrow_rate*elapsed/365 if p.direction == -1 else 0.
            book.capital -= charge
            m['borrow_paid'] += charge
            m['last_borrow_day'] = day
            hit = exit_at(bars[s], m, i-p.entry_bar_idx, 'open')
            if hit:
                book.close(s, hit[0], day, hit[1])
        candidates = sorted(entries.get(day, []), key=priority, reverse=reverse_priority)
        conflicts = {p['symbol'] for p in candidates if len({x['side'] for x in candidates if x['symbol'] == p['symbol']}) > 1}
        seen_symbols = set()
        for plan in candidates:
            s = plan['symbol']
            if s in conflicts or s in seen_symbols:
                book.rejects['conflicting_or_duplicate_candidate'] += 1
                continue
            seen_symbols.add(s)
            bars[s] = quote(s, day)
            book.allocate(plan, bars[s], bars, day, sizing)
        # All open orders are complete before any same-day high/low/close exit.
        for s in book.positions:
            b = bars[s]
            if not all(math.isfinite(float(b[k])) and b[k] > 0 for k in ('High', 'Low', 'Close')):
                raise ValueError('invalid_active_intraday_quote:'+s+':'+day)
            if b.High < max(b.Open, b.Close, b.Low) or b.Low > min(b.Open, b.Close, b.High):
                raise ValueError('inconsistent_intraday_ohlc:'+s+':'+day)
        for phase in ('intraday', 'close'):
            for s, p in list(book.positions.items()):
                hit = exit_at(bars[s], book.meta[s], i-p.entry_bar_idx, phase, exits[day])
                if hit:
                    book.close(s, hit[0], day, hit[1])
        equity = book.equity_at(bars, 'Close')
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError('insolvent_or_nonfinite_portfolio:'+day)
        frozen_risk = sum(p.size*book.meta[s]['risk_per_share'] for s, p in book.positions.items())
        gross = sum(p.size*float(bars[s].Close) for s, p in book.positions.items())
        snapshots.append(dict(date=day, equity=equity, free_collateral=book.capital,
            positions=len(book.positions), gross_fraction=gross/equity, planned_risk_fraction=frozen_risk/equity))
    if book.positions and require_closed:
        raise ValueError('unresolved_positions_at_portfolio_end')
    equity = pd.Series([s['equity'] for s in snapshots], index=pd.to_datetime(days), dtype=float)
    expected = config.initial_cash+sum(t.pnl for t in book.trades)
    expected += sum(book._calc_pnl(s, p.direction, p.size, p.entry_price, float(bars[s].Close))
                    -p.entry_commission-book.meta[s]['borrow_paid'] for s, p in book.positions.items())
    assert math.isclose(equity.iloc[-1] if len(equity) else config.initial_cash, expected, abs_tol=1e-6)
    metrics = calc_metrics(equity, book.trades, config.initial_cash, bars_per_year=252, bench_ret=benchmark_returns)
    return dict(version=VERSION, mode=mode, sizing=sizing, reverse_priority=reverse_priority,
        metrics=metrics, entries=book.entries, trades=book.closed, snapshots=snapshots, rejects=dict(book.rejects),
        fees=sum(t['fees'] for t in book.closed), borrow=sum(t['borrow'] for t in book.closed),
        mean_gross_fraction=sum(s['gross_fraction'] for s in snapshots)/len(snapshots) if snapshots else 0.,
        max_gross_fraction=max((s['gross_fraction'] for s in snapshots), default=0.),
        max_planned_risk_fraction=max((s['planned_risk_fraction'] for s in snapshots), default=0.),
        active_end=len(book.positions), source='archived_shared_opportunities_not_full_strategy_regeneration', eligible_for_activation=False)
