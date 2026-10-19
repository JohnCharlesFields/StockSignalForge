"""Causal downtrend/rebound research. No option pricing or borrow assumptions."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

import pandas as pd

from pullback_entry_research import _bars, feature_history
from pullback_validation import shift_session
from technical_signal_metrics import calculate_atr

VERSION = 'rebound_short_research_v1'


@dataclass(frozen=True)
class ShortEntryConfig:
    hold_sessions: int = 8
    confirmation_sessions: int = 3
    pressure_tolerance_atr: float = .5
    upper_shadow_range_threshold: float = .35
    close_location_threshold: float = .45


def short_feature_history(frame, config=None):
    cfg = config or ShortEntryConfig()
    f = _bars(frame)
    if f.empty:
        return pd.DataFrame()
    out = feature_history(f)[['trend_down', 'cycle_state', 'ma50', 'ma50_slope']].copy()
    ma10, ma20 = f.Close.rolling(10).mean(), f.Close.rolling(20).mean()
    pressure_low = pd.concat([ma10, ma20], axis=1).min(axis=1)
    pressure_high = pd.concat([ma10, ma20], axis=1).max(axis=1)
    atr = calculate_atr(f, 14)
    spread = (f.High-f.Low).where(f.High > f.Low)
    upper = (f.High-pd.concat([f.Open, f.Close], axis=1).max(axis=1))/spread
    location = (f.Close-f.Low)/spread
    rebound = (f.High > f.High.shift(1)) | (f.Close.shift(1) > f.Open.shift(1))
    pressure = (f.High >= pressure_low-cfg.pressure_tolerance_atr*atr) & (f.Close < pressure_high)
    rejection = (upper >= cfg.upper_shadow_range_threshold) & (location <= cfg.close_location_threshold)
    out['setup'] = (out.trend_down & pressure & rebound & rejection & (f.Volume > 0)).fillna(False)
    out['atr14'] = atr
    out['adv'] = (f.Close*f.Volume).rolling(20).mean()
    return out


def build_short_timeline(symbol, frame, config=None):
    cfg = config or ShortEntryConfig()
    if (isinstance(cfg.hold_sessions, bool) or not isinstance(cfg.hold_sessions, int)
            or cfg.hold_sessions < 1 or isinstance(cfg.confirmation_sessions, bool)
            or not isinstance(cfg.confirmation_sessions, int) or not 1 <= cfg.confirmation_sessions <= 10):
        raise ValueError('invalid_short_holding_window')
    if (not all(math.isfinite(v) for v in (cfg.pressure_tolerance_atr, cfg.upper_shadow_range_threshold, cfg.close_location_threshold))
            or not 0 <= cfg.pressure_tolerance_atr <= 2
            or not 0 < cfg.upper_shadow_range_threshold <= 1 or not 0 < cfg.close_location_threshold < 1):
        raise ValueError('invalid_short_setup_configuration')
    result = dict(status='unavailable', contract=dict(version=VERSION, **asdict(cfg),
        direction='SHORT', entry='next_session_open', confirmation='close_below_setup_low',
        same_bar_exit='stop_first_if_both_touched', calibrated=False,
        return_kind='underlying_short_proxy', borrow_verified=False,
        replay_scope='current_frozen_rule_not_historical_prediction'), markers=[], states=[], transactions=[],
        latest={}, validated_profit_probability=None,
        note='空头独立规则研究；未核验借券、股息补偿与强平风险，不是Put收益或实盘成交。')
    f = _bars(frame)
    if len(f) < 55:
        result['reason'] = 'insufficient_or_invalid_ohlcv'
        return result
    features = short_feature_history(f, cfg)
    pending = position = None
    quality_error = None

    def mark(i, stage, price, reason):
        result['markers'].append(dict(time=f.index[i].strftime('%Y-%m-%d'), price=round(float(price), 4),
            side='BUY' if stage == 'EXIT' else 'SELL', direction='SHORT',
            action='COVER_SHORT' if stage == 'EXIT' else 'OPEN_SHORT' if stage == 'ENTRY' else 'OBSERVE',
            code=VERSION, rule_version=VERSION, research_only=True, stage=stage, strategy='trend_rebound_short',
            text={'WATCH':'空观', 'CONFIRMED':'空确', 'ENTRY':'空入', 'EXIT':'回补', 'INVALIDATED':'空失'}[stage],
            reason=reason))

    for i, (stamp, bar) in enumerate(f.iterrows()):
        x = features.iloc[i]
        stage, reason = 'WAIT', '等待中期下行中的反弹受阻；持续下跌本身不是做空触发。'
        if i and (pending or position) and shift_session(f.index[i-1].date(), 1) < stamp.date():
            quality_error = 'missing_session_before_'+stamp.strftime('%Y-%m-%d')
            result['states'].append(dict(date=stamp.strftime('%Y-%m-%d'), stage='DATA_UNKNOWN',
                reason='持有或确认路径缺少交易日，不猜测中间成交与止损。', direction='SHORT', research_only=True))
            break
        ended = False
        if position is None and pending and pending.get('confirmed'):
            target = float(bar.Open-2*(pending['high']-bar.Open))
            if bar.Open >= pending['high'] or target <= 0:
                mark(i, 'INVALIDATED', bar.Open, '次开盘越过失效位或风险距离无有效2R目标，不假设入场。')
                pending, ended = None, True
            else:
                position = dict(symbol=symbol, entry_index=i, entry_date=stamp.strftime('%Y-%m-%d'),
                    entry_price=float(bar.Open), stop=pending['high'], target=target,
                    signal_date=pending['confirmed_date'], adv=pending['adv'], direction='SHORT')
                mark(i, 'ENTRY', bar.Open, '规则回放：确认后次开盘假设开空，未核验借券，非真实成交。')
                pending = None
        if position:
            p = position
            held = i-p['entry_index']
            price = exit_reason = None
            if bar.Open >= p['stop']:
                price, exit_reason = float(bar.Open), 'gap_above_stop'
            elif held >= cfg.hold_sessions:
                price, exit_reason = float(bar.Open), 'time_exit_at_open'
            elif bar.Open <= p['target']:
                price, exit_reason = float(bar.Open), 'gap_below_target'
            elif bar.High >= p['stop']:
                price, exit_reason = p['stop'], 'stop_first_conservative'
            elif bar.Low <= p['target']:
                price, exit_reason = p['target'], 'target'
            if price is not None:
                result['transactions'].append({**p, 'exit_date':stamp.strftime('%Y-%m-%d'), 'exit_price':price,
                    'held_sessions':held, 'exit_reason':exit_reason,
                    'gross_directional_return':(p['entry_price']-price)/p['entry_price'],
                    'return_kind':'underlying_short_proxy', 'borrow_verified':False})
                mark(i, 'EXIT', price, '规则回放回补：'+exit_reason+'；不是实际平仓记录。')
                position, ended = None, True
            else:
                stage, reason = 'RESEARCH_HOLD', '空头规则观察持有中，不重复开空。'
        if not position and not ended:
            if pending:
                if bar.High > pending['high']:
                    mark(i, 'INVALIDATED', max(bar.Open, pending['high']), '突破观察K线高点，空头观察失效。')
                    pending, ended = None, True
                elif i-pending['index'] > cfg.confirmation_sessions:
                    pending = None
                elif i > pending['index'] and bar.Close < pending['low'] and bar.Close < bar.Open:
                    if x.trend_down and x.cycle_state == 'down_cycle' and bar.Volume > 0:
                        pending.update(confirmed=True, confirmed_date=stamp.strftime('%Y-%m-%d'), adv=float(x.adv))
                        mark(i, 'CONFIRMED', bar.Close, '收盘跌破反弹受阻K线低点，中期和日隧道均下行；仅次开盘假设入场。')
                        stage, reason = 'PENDING_ENTRY', '空头收盘确认，下一交易日开盘价格尚未知。'
                    else:
                        stage, reason = 'WAITING_CONFIRMATION', '跌破但中期或日隧道下行未确认，不开放空头入场。'
                else:
                    stage, reason = 'WAITING_CONFIRMATION', '已有反弹受阻，等待后续收盘跌破观察低点。'
            if pending is None and not ended and i >= 54 and x.setup:
                pending = dict(index=i, high=float(bar.High), low=float(bar.Low))
                mark(i, 'WATCH', bar.Close, '中期下行、反弹至均线压力附近后上影受阻，仅观察，尚未确认。')
                stage, reason = 'SHORT_WATCH', '反弹受阻观察，尚未收盘跌破观察低点。'
        result['states'].append(dict(date=stamp.strftime('%Y-%m-%d'), stage=stage, direction='SHORT',
            research_only=True, trend_down=bool(x.trend_down), cycle_state=str(x.cycle_state), reason=reason,
            entry_price=position['entry_price'] if position else None,
            stop_price=position['stop'] if position else pending['high'] if pending else None,
            target_price=position['target'] if position else None,
            ma50=float(x.ma50) if pd.notna(x.ma50) else None,
            ma50_slope=float(x.ma50_slope) if pd.notna(x.ma50_slope) else None))
    result.update(status='research_only', latest=result['states'][-1])
    if quality_error or f.attrs.get('invalid_bar_at'):
        result.update(status='unavailable', reason=quality_error or 'invalid_bar_at_'+f.attrs['invalid_bar_at'],
            latest=dict(stage='DATA_UNKNOWN', direction='SHORT', research_only=True,
                        reason='路径存在缺失或无效K线，不继续确认或猜测退出；已知历史标记保留。'))
    return result
