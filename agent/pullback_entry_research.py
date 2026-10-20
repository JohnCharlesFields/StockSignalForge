"""Causal research setups and next-open path replay, never a profit probability.

The old percentile chart is deliberately not a source for this contract. Scores
match the live launch/tunnel rules; frozen legacy calibration curves stay intact.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

from technical_signal_metrics import _runs, _tail_run
from pullback_validation import shift_session
from corporate_action_research import action_aware_replay

VERSION = 'pullback_entry_research_v3'


@dataclass(frozen=True)
class EntryConfig:
    hold_sessions: int = 8
    setup_threshold: float = .58
    confirmation_sessions: int = 3
    weights: tuple[float, float] = (.5, .5)
    allow_countertrend: bool = False  # Offline comparison only; live callers leave false.


def pullback_score(launch, hv_rise, weights=(.5, .5)):
    a, b = weights
    if not all(math.isfinite(x) and x >= 0 for x in (a, b)) or a + b <= 0:
        raise ValueError('invalid_pullback_weights')
    launch = min(1., max(0., float(launch)))
    hv = .5 if hv_rise is None or not math.isfinite(float(hv_rise)) else min(1., max(0., .5 + .5 * float(hv_rise)))
    return min(1., max(0., (a * (1-launch) + b * hv) / (a+b)))


def _bars(frame):
    required = ['Open', 'High', 'Low', 'Close', 'Volume']
    if frame is None or not isinstance(frame.index, pd.DatetimeIndex) or not set(required) <= set(frame):
        return pd.DataFrame()
    out = frame[required].apply(pd.to_numeric, errors='coerce').sort_index()
    if out.index.has_duplicates:
        return pd.DataFrame()
    valid = pd.Series(np.isfinite(out.to_numpy()).all(axis=1), index=out.index)
    valid &= (out[['Open', 'High', 'Low', 'Close']] > 0).all(axis=1) & (out.Volume >= 0)
    valid &= (out.Low <= out[['Open', 'Close']].min(axis=1)) & (out.High >= out[['Open', 'Close']].max(axis=1))
    if not valid.all():
        first_bad = int(np.flatnonzero(~valid.to_numpy())[0])
        prefix = out.iloc[:first_bad].copy()
        prefix.attrs['invalid_bar_at'] = str(out.index[first_bad].date())
        return prefix
    return out


def current_context(frame, scored=None):
    """One-day policy context; does not promote or overwrite a probability."""
    f = _bars(frame)
    if len(f) < 55 or f.attrs.get('invalid_bar_at'):
        return dict(version=VERSION, stage='DATA_UNKNOWN', research_only=True, reason='行情不足或存在无效K线，不确认入场。')
    ma50 = float(f.Close.tail(50).mean())
    slope = ma50-float(f.Close.iloc[:-5].tail(50).mean())
    up = bool(float(f.Close.iloc[-1]) >= ma50 and slope >= 0)
    down = bool(float(f.Close.iloc[-1]) < ma50 and slope < 0)
    return dict(version=VERSION, stage='TREND_STRUCTURE' if up else 'COUNTERTREND_WATCH',
        strategy='trend_pullback' if up else 'countertrend', trend_up=up, trend_down=down,
        cycle_state=(scored or {}).get('daily_tunnel', {}).get('cycle_state'),
        date=f.index[-1].strftime('%Y-%m-%d'), ma50=ma50, ma50_slope=slope, research_only=True,
        reason='顺势结构，仍须收盘确认。' if up else '中期偏弱或下行，只作逆势观察，不开放确认买点。')


def feature_history(frame, weights=(.5, .5)):
    """Trailing-only vector history, regression-checked against _score_frame.

    The bounded run scan below reproduces the live tunnel's last-80-session
    cycle statistics, not the legacy replay's different tunnel formula.
    """
    f = _bars(frame)
    if f.empty:
        return pd.DataFrame()
    c = f.Close
    ma = {n: c.rolling(n).mean() for n in (5, 10, 15, 20, 50, 200)}
    slope = ma[5].diff()
    ret5, ret20 = c.pct_change(5, fill_method=None), c.pct_change(20, fill_method=None)
    up = ((c >= ma[5]) & (ma[5] >= ma[10]) & (slope >= 0)).to_numpy()
    repair = ((c < ma[5]) & (c >= ma[20]) & (ma[10] >= ma[20]) & (ret5 > -.08)).to_numpy()
    down = (((c < ma[10]) & (slope < 0)) | ((ma[5] < ma[10]) & (ret5 < 0))).to_numpy()
    above5, above20 = (c >= ma[5]).to_numpy(), (c >= ma[20]).to_numpy()
    aligned = ((ma[5] >= ma[10]) & (ma[10] >= ma[15]) & (ma[15] >= ma[20])).to_numpy()
    breaks = ((ma[5]-c)/ma[5]).clip(lower=0).to_numpy()
    above10, rising5 = (c >= ma[10]).to_numpy(), (slope > 0).to_numpy()
    tunnel_scores, cycles = [], []
    for i in range(len(f)):
        if i < 26:
            tunnel_scores.append(0.)
            cycles.append('insufficient_data')
            continue
        start, sample = max(19, i-79), max(19, i-19)
        lengths = _runs(up[start:i+1])
        longest = max(lengths or [0])
        average = sum(lengths)/len(lengths) if lengths else 0.
        power = min(1., .65*min(longest, 30)/30 + .35*min(average, 18)/18)
        recent_down = int(down[max(start, i-11):i+1].sum())
        turn = min(1., min(.35, recent_down/12*.35) + .25*above10[i] + .25*rising5[i] + .15*above5[i])
        streak = _tail_run(above5[sample:i+1])
        if turn >= .65 and recent_down >= 2:
            state, zone = 'down_to_up_turn', .9
        elif up[i] and streak >= 3:
            state, zone = 'up_cycle', 1.
        elif repair[i]:
            state, zone = 'constructive_pullback', .68
        elif down[i]:
            state, zone = 'down_cycle', .25
        else:
            state, zone = 'neutral', .45
        components = [turn*24, power*20, zone*18, above20[sample:i+1].mean()*12,
                      aligned[sample:i+1].mean()*10, max(0., 1-breaks[sample:i+1].max()/.10)*8,
                      above5[sample:i+1].mean()*8]
        tunnel_scores.append(round(min(100., sum(round(float(x), 1) for x in components)), 1))
        cycles.append(state)
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rsi = (100-100/(1+gain/loss.replace(0, np.nan))).fillna(100.)
    vol = f.Volume / f.Volume.rolling(20).mean().clip(lower=1)
    breakout = c/c.shift(1).rolling(20).max()-1
    base = (.5 + .28*(3.2*ret5+1.8*ret20).clip(-1, 1) + .20*((vol-1)/2).clip(-.4, .8)
            + .22*(breakout*12).clip(-.4, .8) + .10*((rsi-50)/100).clip(-.3, .3)).clip(0, 1)
    out = pd.DataFrame({'launch_score': (.78*base+.22*np.array(tunnel_scores)/100).clip(0, 1).round(4),
                        'tunnel_score': tunnel_scores, 'cycle_state': cycles}, index=f.index)
    hv = np.log(c/c.shift(1)).rolling(20).std()*np.sqrt(252.)
    out['hv_rise'] = (hv/hv.shift(10)-1).replace([np.inf, -np.inf], np.nan)
    out['pullback_hv_score'] = [pullback_score(l, h, weights) if pd.notna(l) else np.nan
                                for l, h in zip(out.launch_score, out.hv_rise)]
    out['ma20'], out['ma50'], out['ma200'] = ma[20], ma[50], ma[200]
    out['ma50_slope'] = ma[50]-ma[50].shift(5)
    out['trend_up'] = (c >= ma[50]) & (out.ma50_slope >= 0)
    out['trend_down'] = (c < ma[50]) & (out.ma50_slope < 0)
    return out


@action_aware_replay
def build_entry_timeline(symbol, frame, config=None):
    cfg = config or EntryConfig()
    if isinstance(cfg.hold_sessions, bool) or not isinstance(cfg.hold_sessions, int) or cfg.hold_sessions < 1:
        raise ValueError('invalid_hold_sessions')
    if not 0 <= cfg.setup_threshold <= 1 or not 1 <= cfg.confirmation_sessions <= 10:
        raise ValueError('invalid_entry_configuration')
    contract = dict(version=VERSION, hold_sessions=cfg.hold_sessions, weights=list(cfg.weights),
                    setup_threshold=cfg.setup_threshold, confirmation_sessions=cfg.confirmation_sessions,
                    threshold_source='fixed_rule_not_full_sample_quantile', entry='next_session_open',
                    confirmation='close_above_setup_high', same_bar_exit='stop_first_if_both_touched',
                    replay_scope='current_frozen_rule_not_historical_prediction', calibrated=False)
    contract['allow_countertrend'] = cfg.allow_countertrend
    result = dict(status='unavailable', contract=contract, markers=[], states=[], transactions=[],
                  latest={}, validated_profit_probability=None)
    f = _bars(frame)
    if len(f) < 55:
        result['reason'] = 'insufficient_or_invalid_ohlcv'
        return result
    features = feature_history(f, cfg.weights)
    pending = position = None
    quality_error = None
    next_setup = 0
    markers, states, trades = result['markers'], result['states'], result['transactions']

    def mark(i, stage, price, strategy, reason, **extra):
        markers.append(dict(time=f.index[i].strftime('%Y-%m-%d'), price=round(float(price), 4),
            side='SELL' if stage in {'EXIT', 'INVALIDATED'} else 'BUY', code=VERSION,
            research_only=True, stage=stage, strategy=strategy,
            text={'WATCH':'观', 'CONFIRMED':'确', 'ENTRY':'入', 'EXIT':'离', 'INVALIDATED':'失'}[stage],
            reason=reason, rule_version=VERSION, **extra))

    for i, (stamp, bar) in enumerate(f.iterrows()):
        x = features.iloc[i]
        if i and (pending or position) and shift_session(f.index[i-1].date(), 1) < stamp.date():
            quality_error = 'missing_session_before_'+stamp.strftime('%Y-%m-%d')
            states.append(dict(date=stamp.strftime('%Y-%m-%d'), stage='DATA_UNKNOWN', research_only=True,
                               reason='确认或持仓路径缺少交易日，不沿用旧结构或猜测成交。'))
            break
        strategy = 'trend_pullback' if x.trend_up else 'countertrend' if pd.notna(x.ma50_slope) else 'unknown'
        stage, explanation = 'WAIT', '等待回调与收盘确认；不是入场许可。'
        ended = False
        if position is None and pending and pending.get('confirmed'):
            if bar.Open <= pending['low']:
                mark(i, 'INVALIDATED', bar.Open, pending['strategy'], '确认后的次开盘跳空越过失效位，不假设成交。')
                pending, ended = None, True
            else:
                position = dict(entry_index=i, entry_date=stamp.strftime('%Y-%m-%d'), entry_price=float(bar.Open),
                    stop=pending['low'], target=float(bar.Open+2*(bar.Open-pending['low'])),
                    strategy=pending['strategy'], signal_date=pending['confirmed_date'], adv=pending['adv'])
                mark(i, 'ENTRY', bar.Open, position['strategy'], '规则回放：确认后次开盘假设入场，非真实成交。',
                     entry_convention='next_session_open')
                pending = None
        if position:
            p = position
            held = i-p['entry_index']
            price = reason = None
            if bar.Open <= p['stop']:
                price, reason = float(bar.Open), 'gap_below_stop'
            elif held >= cfg.hold_sessions:
                price, reason = float(bar.Open), 'time_exit_at_open'
            elif bar.Open >= p['target']:
                price, reason = float(bar.Open), 'gap_above_target'
            elif bar.Low <= p['stop']:
                price, reason = p['stop'], 'stop_first_conservative'
            elif bar.High >= p['target']:
                price, reason = p['target'], 'target'
            if price is not None:
                trades.append({**p, 'exit_date': stamp.strftime('%Y-%m-%d'), 'exit_price':price,
                    'held_sessions':held, 'exit_reason':reason, 'gross_return':price/p['entry_price']-1})
                mark(i, 'EXIT', price, p['strategy'], '规则回放退出：'+reason+'；不是账户交易记录。')
                position, ended, next_setup = None, True, i+1
            else:
                stage, explanation = 'RESEARCH_HOLD', '规则回放观察持有中，不重复产生入场。'
        if not position and not ended:
            if pending:
                if bar.Low < pending['low']:
                    mark(i, 'INVALIDATED', min(bar.Open, pending['low']), pending['strategy'], '实际跌破原观察K线低点，观察失效。')
                    pending, ended = None, True
                elif i-pending['index'] > cfg.confirmation_sessions:
                    pending = None
                elif i > pending['index'] and bar.Close > pending['high'] and bar.Close > bar.Open:
                    if ((x.trend_up and x.cycle_state != 'down_cycle') or cfg.allow_countertrend) and bar.Volume > 0:
                        pending.update(confirmed=True, confirmed_date=stamp.strftime('%Y-%m-%d'), strategy=strategy,
                                       adv=float((f.Close*f.Volume).iloc[max(0,i-19):i+1].mean()))
                        mark(i, 'CONFIRMED', bar.Close, pending['strategy'],
                             '收盘站上观察K线高点且中期结构未下行；次开盘才可做规则试算。' if x.trend_up
                             else '离线逆势对照组：收盘反弹确认，未启用中期趋势过滤。')
                        stage, explanation = 'PENDING_ENTRY', '顺势收盘确认，仅研究；下一交易日开盘价格尚未知。'
                    else:
                        stage, explanation = 'COUNTERTREND_WATCH', '短期反弹尚未修复中期下行，仅逆势观察，不产生确认入场。'
                else:
                    stage, explanation = 'WAITING_CONFIRMATION', '观察尚未收盘突破，等待确认。'
            if pending is None and not ended and i >= max(54, next_setup) and x.pullback_hv_score >= cfg.setup_threshold:
                pending = dict(index=i, high=float(bar.High), low=float(bar.Low), strategy=strategy)
                mark(i, 'WATCH', bar.Close, strategy, '超卖与波动扩张观察；下行不等于买点，等待后续收盘确认。')
                stage = 'TREND_WATCH' if x.trend_up else 'COUNTERTREND_WATCH'
                explanation = '顺势回调观察，尚未确认。' if x.trend_up else '中期偏弱或下行，仅逆势观察，不开放确认买点。'
        states.append(dict(date=stamp.strftime('%Y-%m-%d'), stage=stage, strategy=strategy,
            cycle_state=str(x.cycle_state), trend_up=bool(x.trend_up), trend_down=bool(x.trend_down),
            reason=explanation, entry_price=position['entry_price'] if position else None,
            ma50=float(x.ma50) if pd.notna(x.ma50) else None,
            ma50_slope=float(x.ma50_slope) if pd.notna(x.ma50_slope) else None))
    result.update(status='research_only', latest=states[-1],
                  note='未过滤逆势的离线对照，不作为线上买点。' if cfg.allow_countertrend else
                  '橙色仅观察；蓝色确/入是规则研究，不是已验证买点或真实成交。中期下行不产生确认入场。')
    if quality_error or f.attrs.get('invalid_bar_at'):
        result.update(status='unavailable', reason=quality_error or 'invalid_bar_at_'+f.attrs['invalid_bar_at'],
            latest=dict(stage='DATA_UNKNOWN', research_only=True, reason='路径存在缺失或无效K线，不继续推演；历史标记保留。'))
    return result
