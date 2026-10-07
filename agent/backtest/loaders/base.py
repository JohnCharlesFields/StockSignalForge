"""DataLoader Protocol, shared exceptions, and bounded-retry helpers.

The retry/budget helpers are the canonical pattern for any loader that calls
a flaky external API: a wall-clock deadline plus a small backoff schedule
applied only to a declared transient exception class. New loaders should
import :func:`check_budget` and :func:`retry_with_budget` rather than
re-implementing the loop.
"""

from __future__ import annotations

import time
from typing import Callable, Protocol, TypeVar, runtime_checkable

import pandas as pd


class NoAvailableSourceError(Exception):
    """Raised when no data source is available for a given market."""


def validate_date_range(start_date: str, end_date: str) -> None:
    """Validate that start_date <= end_date.

    Args:
        start_date: Start date string (YYYY-MM-DD).
        end_date: End date string (YYYY-MM-DD).

    Raises:
        ValueError: If dates are invalid or start > end.
    """
    try:
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
    except Exception as exc:
        raise ValueError(f"Invalid date format: start={start_date!r}, end={end_date!r}") from exc
    if start > end:
        raise ValueError(f"start_date ({start_date}) > end_date ({end_date})")


# ---------------------------------------------------------------------------
# Bounded retry / budget helpers (shared by ccxt_loader, okx, and any future
# loader calling a flaky external API).
# ---------------------------------------------------------------------------

DEFAULT_BACKOFF: tuple[float, ...] = (0.5, 1.5, 4.0)
DEFAULT_MAX_RETRIES = 3


def check_budget(deadline: float, label: str, budget_s: float | None = None) -> None:
    """Raise :class:`TimeoutError` if the monotonic clock has crossed ``deadline``.

    Use this between pages of a paginated fetch to fail fast instead of
    grinding through more requests once the wall-clock budget is gone.

    Args:
        deadline: ``time.monotonic()`` instant past which we abort.
        label: Free-form label used in the exception message
            (e.g. ``"ccxt fetch for BTC/USDT"``).
        budget_s: Original budget in seconds, included verbatim in the
            message when present.
    """
    if time.monotonic() > deadline:
        suffix = f" exceeded {budget_s:.0f}s budget" if budget_s is not None else " exceeded budget"
        raise TimeoutError(f"{label}{suffix}")


_T = TypeVar("_T")


def retry_with_budget(
    fn: Callable[[], _T],
    *,
    transient: type[BaseException] | tuple[type[BaseException], ...],
    deadline: float,
    label: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff: tuple[float, ...] = DEFAULT_BACKOFF,
) -> _T:
    """Call ``fn`` with a bounded retry budget on declared transient errors.

    Between attempts sleeps ``min(backoff[attempt], remaining_budget)`` so a
    short remaining budget never spends the full backoff. The terminal
    transient failure — whether ``max_retries`` is exhausted OR the deadline
    has passed — is wrapped in :class:`TimeoutError`, preserving the original
    exception as ``__cause__``. Anything not in ``transient`` propagates
    unchanged on the first occurrence (we never retry an exception class
    the caller didn't opt in to).

    Args:
        fn: Zero-arg callable producing the result.
        transient: Exception class(es) considered transient and retryable.
        deadline: ``time.monotonic()`` instant past which retries are aborted.
        label: Free-form label used in the TimeoutError message
            (e.g. ``"OKX fetch for BTC-USDT"``).
        max_retries: Additional attempts after the first call. Total
            attempts = ``max_retries + 1``.
        backoff: Per-retry sleep seconds. Must have at least
            ``max_retries`` entries.

    Returns:
        Whatever ``fn`` returns.

    Raises:
        ValueError: ``backoff`` is shorter than ``max_retries``.
        TimeoutError: All retries exhausted or the deadline crossed.
        Any non-transient exception: Propagated unchanged from ``fn``.
    """
    if len(backoff) < max_retries:
        raise ValueError(
            f"backoff has {len(backoff)} entries; need >= max_retries ({max_retries})"
        )
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except transient as exc:
            remaining = deadline - time.monotonic()
            if attempt == max_retries or remaining <= 0:
                raise TimeoutError(
                    f"{label} failed after {attempt + 1} attempt(s): {exc}"
                ) from exc
            time.sleep(min(backoff[attempt], max(0.0, remaining)))
    raise AssertionError("unreachable: retry loop must return or raise")  # pragma: no cover


@runtime_checkable
class DataLoaderProtocol(Protocol):
    """Interface that every data source loader must satisfy."""

    name: str
    markets: set[str]
    requires_auth: bool

    def is_available(self) -> bool:
        """Check whether this data source is usable (token present, network ok, etc.)."""
        ...

    def fetch(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: list[str] | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV data.

        Returns:
            Mapping ``{symbol: DataFrame(trade_date, open, high, low, close, volume)}``.
        """
        ...
