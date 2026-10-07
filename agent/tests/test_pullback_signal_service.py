from pullback_signal_service import PullbackConfig, confirm_pullback_rejection, detect_pullback_rejection, detect_pullback_setup


def _bars(values):
    return [
        {"Date": f"2026-01-{i+1:02d}", "Open": o, "High": h, "Low": l, "Close": c, "Volume": 1000}
        for i, (o, h, l, c) in enumerate(values)
    ]


def _base_uptrend():
    return [(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(20)]


def test_plain_candle_is_not_pullback_rejection() -> None:
    bars = _bars(_base_uptrend() + [(116, 117, 115.5, 116.2)])
    signal = detect_pullback_rejection(bars, "TEST")
    assert signal["signal_stage"] == "NONE"


def test_long_lower_shadow_after_pullback_is_watch() -> None:
    bars = _bars(_base_uptrend() + [(116, 117, 108, 114.8)])
    signal = detect_pullback_rejection(bars, "TEST")
    assert signal["signal_stage"] in {"WATCH", "STRONG_WATCH"}
    assert signal["waiting_breakout_price"] == 117
    assert signal["invalidation_price"] == 108


def test_long_shadow_with_weak_close_is_not_high_quality() -> None:
    bars = _bars(_base_uptrend() + [(116, 117, 108, 110)])
    signal = detect_pullback_rejection(bars, "TEST")
    assert signal["signal_stage"] != "STRONG_WATCH"


def test_zero_body_and_zero_range_do_not_crash() -> None:
    bars = _bars(_base_uptrend() + [(116, 116, 116, 116)])
    signal = detect_pullback_rejection(bars, "TEST")
    assert signal["signal_stage"] in {"NONE", "WATCH", "STRONG_WATCH"}


def test_missing_data_does_not_crash() -> None:
    signal = detect_pullback_rejection([{"Open": 1}], "TEST")
    assert signal["signal_stage"] == "NONE"


def test_confirmation_states() -> None:
    rejection_bars = _bars(_base_uptrend() + [(116, 117, 108, 114.8)])
    rejection = detect_pullback_rejection(rejection_bars, "TEST")

    weak = confirm_pullback_rejection(rejection_bars + _bars([(114.5, 116, 113, 115.5)]), rejection, "TEST")
    assert weak["signal_stage"] in {"WEAK_CONFIRMED", "WAITING_CONFIRMATION"}

    confirmed = confirm_pullback_rejection(rejection_bars + _bars([(115, 118, 114, 116.5)]), rejection, "TEST")
    assert confirmed["signal_stage"] == "CONFIRMED"

    strong = confirm_pullback_rejection(rejection_bars + _bars([(115, 119, 114, 118.5)]), rejection, "TEST")
    assert strong["signal_stage"] == "STRONG_CONFIRMED"

    invalid = confirm_pullback_rejection(rejection_bars + _bars([(114, 115, 107, 108)]), rejection, "TEST")
    assert invalid["signal_stage"] == "INVALIDATED"


def test_rule_can_be_disabled() -> None:
    cfg = PullbackConfig(enable_pullback_rejection=False)
    signal = detect_pullback_rejection(_bars(_base_uptrend() + [(116, 117, 108, 114.8)]), "TEST", cfg)
    assert signal["signal_stage"] == "NONE"


def test_setup_finds_prior_rejection_and_later_confirmation() -> None:
    bars = _bars(_base_uptrend() + [(116, 117, 108, 114.8), (115, 119, 114, 118.5)])

    rejection, confirmation = detect_pullback_setup(bars, "TEST")

    assert rejection["signal_stage"] in {"WATCH", "STRONG_WATCH"}
    assert confirmation["signal_stage"] in {"CONFIRMED", "STRONG_CONFIRMED"}
    assert confirmation["rejection_high"] == rejection["waiting_breakout_price"]
