"""Unit tests for the shared loader retry/budget helpers.

The ccxt + okx integration tests already exercise these helpers via their
real loaders; these tests pin the helper semantics directly so future
loaders that adopt :func:`retry_with_budget` and :func:`check_budget`
inherit the same guarantees:

- Happy path runs ``fn`` exactly once.
- Transient exceptions retry up to ``max_retries + 1`` total attempts,
  then are wrapped in ``TimeoutError`` with the original as ``__cause__``.
- Non-transient exceptions propagate immediately, unchanged.
- A deadline crossed mid-retry aborts before exhausting attempts.
- ``check_budget`` raises iff the deadline has passed.
- A short remaining budget is never overspent by ``backoff``.
"""

from __future__ import annotations

import time

import pytest

import backtest.loaders.base as base
from backtest.loaders.base import (
    DEFAULT_BACKOFF,
    DEFAULT_MAX_RETRIES,
    check_budget,
    retry_with_budget,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Keep retry tests instant + deterministic."""
    monkeypatch.setattr(base.time, "sleep", lambda *_a, **_k: None)


class _Transient(Exception):
    pass


class _Fatal(Exception):
    pass


def _scripted(*outcomes):
    """Return a fn() that walks through scripted outcomes (return value or raise)."""
    state = {"i": 0}

    def fn():
        i = state["i"]
        state["i"] += 1
        outcome = outcomes[min(i, len(outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    fn.state = state  # type: ignore[attr-defined]
    return fn


def test_happy_path_single_call():
    fn = _scripted("ok")
    deadline = time.monotonic() + 60
    assert retry_with_budget(fn, transient=_Transient, deadline=deadline, label="x") == "ok"
    assert fn.state["i"] == 1


def test_transient_retried_then_succeeds():
    fn = _scripted(_Transient("blip"), _Transient("blip"), "ok")
    deadline = time.monotonic() + 60
    assert retry_with_budget(fn, transient=_Transient, deadline=deadline, label="x") == "ok"
    assert fn.state["i"] == 3


def test_persistent_transient_exhausts_then_timeouts():
    fn = _scripted(_Transient("down"))
    deadline = time.monotonic() + 60
    with pytest.raises(TimeoutError) as ei:
        retry_with_budget(fn, transient=_Transient, deadline=deadline, label="myfetch")
    assert fn.state["i"] == DEFAULT_MAX_RETRIES + 1  # bounded
    # Original exception preserved as cause for diagnosability.
    assert isinstance(ei.value.__cause__, _Transient)
    assert "myfetch" in str(ei.value)
    assert "attempt(s)" in str(ei.value)


def test_non_transient_propagates_unchanged():
    fn = _scripted(_Fatal("logic bug"))
    deadline = time.monotonic() + 60
    with pytest.raises(_Fatal):  # NOT wrapped in TimeoutError
        retry_with_budget(fn, transient=_Transient, deadline=deadline, label="x")
    assert fn.state["i"] == 1


def test_deadline_crossed_mid_retry_aborts(monkeypatch):
    """A wall-clock deadline already past abort the retry on the first
    post-failure budget check, before max_retries is exhausted."""
    # ``monotonic`` always returns a value far past the deadline, so the
    # remaining-budget gate fires on the first transient failure.
    monkeypatch.setattr(base.time, "monotonic", lambda: 1_000_000.0)
    fn = _scripted(_Transient("slow"))
    with pytest.raises(TimeoutError):
        retry_with_budget(fn, transient=_Transient, deadline=2000.0, label="x")
    assert fn.state["i"] == 1  # aborted before any retry


def test_multi_transient_tuple():
    """Tuple of transient classes is supported (matches except T1|T2 semantics)."""

    class _A(Exception):
        pass

    class _B(Exception):
        pass

    fn = _scripted(_A("a"), _B("b"), "ok")
    deadline = time.monotonic() + 60
    assert retry_with_budget(fn, transient=(_A, _B), deadline=deadline, label="x") == "ok"


def test_backoff_shorter_than_retries_rejected():
    """Defensive: misconfigured backoff is rejected at call time, not silently
    indexed out of range mid-retry."""
    with pytest.raises(ValueError, match="backoff has"):
        retry_with_budget(
            _scripted("ok"),
            transient=_Transient,
            deadline=time.monotonic() + 60,
            label="x",
            max_retries=5,
            backoff=DEFAULT_BACKOFF,  # only 3 entries
        )


def test_check_budget_passes_before_deadline():
    check_budget(time.monotonic() + 60, "x")  # no raise


def test_check_budget_raises_past_deadline():
    with pytest.raises(TimeoutError, match="myfetch"):
        check_budget(time.monotonic() - 1, "myfetch", budget_s=60)


def test_check_budget_message_includes_budget_when_provided():
    with pytest.raises(TimeoutError, match="60s budget"):
        check_budget(time.monotonic() - 1, "label", budget_s=60.0)


def test_check_budget_message_omits_budget_when_absent():
    with pytest.raises(TimeoutError) as ei:
        check_budget(time.monotonic() - 1, "label")
    assert "budget" in str(ei.value)
    assert "0s" not in str(ei.value)  # don't print "0s budget" by accident
