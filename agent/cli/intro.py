"""Startup banner for the interactive CLI.

The layout follows §3.3 of the 2026-05-19 UI/UX design proposal:

    Vibe-Trading v0.2.0
    ─────────────────────────────────────────────
    Natural-language finance research that
    thinks before it answers.

    Model:      deepseek/deepseek-v4-pro
    Skills:     72 loaded
    Tools:      27 registered
    Sessions:   3 prior (use /history to browse)

    Try one of these:
    > Compare AAPL and MSFT fundamentals
    > Backtest a momentum strategy on BTC, 2020-now
    > What's the implied vol on TSLA Jan 2026 calls?

    /help for commands · Ctrl+C to clear input · Ctrl+D to exit
    ─────────────────────────────────────────────

The wordmark "Vibe-Trading" is rendered in the brand orange (single accent),
everything else uses neutral / dim styles per the "Orange discipline rule".
No emoji, no ASCII-art logo — wordmark-only (proposal §1.3).
"""

from __future__ import annotations

from typing import Final, Sequence

from rich.console import Console
from rich.text import Text

from cli._version import __version__ as _VERSION
from cli.theme import Theme

_TAGLINE: Final[str] = (
    "Natural-language finance research that\nthinks before it answers."
)

_DEFAULT_EXAMPLES: Final[tuple[str, ...]] = (
    "Compare AAPL and MSFT fundamentals",
    "Backtest a momentum strategy on BTC, 2020-now",
    "What's the implied vol on TSLA Jan 2026 calls?",
)


def _rule(console: Console, *, width: int = 60) -> None:
    """Print a single-character horizontal rule using muted style."""

    char = "─"
    target = min(width, max(20, console.size.width - 2))
    console.print(Text(char * target, style=Theme.muted))


def print_banner(
    console: Console,
    *,
    model: str,
    skills: int,
    tools: int,
    sessions: int,
    examples: Sequence[str] | None = None,
    version: str = _VERSION,
) -> None:
    """Render the startup banner.

    Args:
        console: Shared Rich console (use :func:`cli.theme.get_console`).
        model: Active LLM model id, e.g. ``"deepseek/deepseek-v4-pro"``.
        skills: Number of skills currently loaded.
        tools: Number of tools registered in the agent registry.
        sessions: Number of prior sessions on disk. ``0`` suppresses the
            "/history to browse" hint.
        examples: Three prompt examples to show under "Try one of these:".
            Defaults to a curated triplet covering equities / crypto /
            options.
        version: Override version string (defaults to the package
            metadata version via :mod:`cli._version`).

    Notes:
        - Wordmark is the *only* primary-orange element (design proposal §1.3
          "Orange discipline rule").
        - Padding on the left is two spaces, matching dexter's intro.ts.
        - Examples are prefixed with ``>`` (not ``•``) so they read as REPL
          input prompts, hinting that the user can paste them verbatim.
    """

    pad = "  "
    console.print()  # leading blank line

    # ── Wordmark ────────────────────────────────────────────────────────
    wordmark = Text()
    wordmark.append(pad)
    wordmark.append("Vibe-Trading", style=Theme.primary)
    wordmark.append(f" v{version}", style=Theme.muted)
    console.print(wordmark)

    # ── Rule ────────────────────────────────────────────────────────────
    indent = Text(pad)
    rule = Text("─" * 50, style=Theme.muted)
    console.print(indent + rule)

    # ── Tagline ─────────────────────────────────────────────────────────
    for line in _TAGLINE.splitlines():
        console.print(Text(pad) + Text(line, style=Theme.muted))
    console.print()

    # ── Stats grid ──────────────────────────────────────────────────────
    rows: list[tuple[str, str]] = [
        ("Model", model),
        ("Skills", f"{skills} loaded"),
        ("Tools", f"{tools} registered"),
    ]
    if sessions > 0:
        rows.append(("Sessions", f"{sessions} prior (use /history to browse)"))
    else:
        rows.append(("Sessions", "none yet"))

    label_width = max(len(label) for label, _ in rows) + 2
    for label, value in rows:
        line = Text(pad)
        line.append(f"{label}:".ljust(label_width), style=Theme.label)
        line.append(value, style=Theme.muted)
        console.print(line)

    console.print()

    # ── Examples ────────────────────────────────────────────────────────
    console.print(Text(pad) + Text("Try one of these:", style=Theme.label))
    for ex in (examples or _DEFAULT_EXAMPLES):
        line = Text(pad)
        line.append("> ", style=Theme.primary_dim)
        line.append(ex, style=Theme.muted)
        console.print(line)

    console.print()

    # ── Footer hint ─────────────────────────────────────────────────────
    footer = Text(pad)
    footer.append("/help", style=Theme.info)
    footer.append(" for commands · ", style=Theme.muted)
    footer.append("Ctrl+C", style=Theme.info)
    footer.append(" to clear input · ", style=Theme.muted)
    footer.append("Ctrl+D", style=Theme.info)
    footer.append(" to exit", style=Theme.muted)
    console.print(footer)

    # ── Bottom rule ─────────────────────────────────────────────────────
    console.print(indent + Text("─" * 60, style=Theme.muted))
    console.print()


__all__ = ["print_banner"]
