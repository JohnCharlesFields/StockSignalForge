"""Interactive CLI front door for Vibe-Trading.

Responsibilities:

1. Detect whether ``~/.vibe-trading/.env`` exists; if missing, run the
   onboarding wizard (:mod:`cli.onboard`) before doing anything else.
2. Render the startup banner (:mod:`cli.intro`) on interactive entry.
3. For interactive entry (no subcommand, or ``chat``) drive the REPL
   built on :mod:`cli.input`, :mod:`cli.completer`,
   :mod:`cli.commands.slash_router`, :mod:`cli.components.working_indicator`,
   :mod:`cli.components.tool_event`, and :mod:`cli.components.hint_bar`.
4. For every other subcommand delegate to ``cli._legacy.main`` so the
   long tail of ``serve``, ``run``, ``mcp``, ``sessions``, ``swarm`` etc.
   keeps working without regression.

The console script entry in ``pyproject.toml`` (``vibe-trading = "cli:main"``)
hits :func:`main`.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from cli.intro import print_banner
from cli.onboard import run_onboarding
from cli.theme import Theme, get_console

_ENV_PATH = Path.home() / ".vibe-trading" / ".env"
# Best-effort fallbacks used only when the probe genuinely fails (missing
# dependency, broken install). The numbers track the actual bundled counts
# so a probe failure still shows a plausible banner rather than "0 loaded".
_FALLBACK_SKILLS = 77
_FALLBACK_TOOLS = 31
_HISTORY_RETAINED_TURNS = 6  # how many prior turns to feed the agent loop

# Cached banner-stats and session-store so ``/clear`` and repeat slash handlers
# don't redo the heavy build_registry() / SessionStore construction.
_BANNER_STATS_CACHE: Dict[str, Any] = {}
_SESSION_STORE_CACHE: Any = None


# ---------------------------------------------------------------------------
# Stat probes (best-effort, never blocking)
# ---------------------------------------------------------------------------


def _probe_model_name() -> str:
    """Return the configured LLM model id, or a placeholder."""
    name = os.environ.get("LANGCHAIN_MODEL_NAME") or os.environ.get("OPENAI_MODEL")
    if name:
        return name
    try:
        text = _ENV_PATH.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("LANGCHAIN_MODEL_NAME="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "unset (use /model to pick one)"


def _probe_tool_count() -> int:
    """Count registered tools without blocking startup on import errors."""
    try:
        from src.tools import build_registry

        return len(build_registry())
    except Exception:  # noqa: BLE001 — never block startup on stats
        return _FALLBACK_TOOLS


def _probe_skill_count() -> int:
    """Count bundled + user skills without blocking startup on import errors.

    Reads ``SkillsLoader.skills`` directly — that is the authoritative list
    populated by :meth:`SkillsLoader._load` from bundled ``agent/skills/``
    plus ``~/.vibe-trading/skills/user/``.
    """
    try:
        from src.agent.skills import SkillsLoader

        loader = SkillsLoader()
        return len(loader.skills)
    except Exception:  # noqa: BLE001
        return _FALLBACK_SKILLS


def _probe_session_count() -> int:
    """Count recorded sessions from the SQLite store."""
    db_path = Path.home() / ".vibe-trading" / "sessions.db"
    if not db_path.exists():
        return 0
    try:
        import sqlite3

        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
            return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _collect_banner_stats(*, refresh: bool = False) -> Dict[str, Any]:
    """Return the four banner stat values, computing them at most once.

    The interactive launch path runs every probe synchronously (the
    user is already waiting for the prompt). Subsequent callers
    (``/clear`` re-render, ``/debug`` summary) reuse the cached values
    so they don't re-import the tool registry on every keystroke.

    Args:
        refresh: When True, recompute and overwrite the cache.

    Returns:
        ``{"model": str, "skills": int, "tools": int, "sessions": int}``.
    """
    if _BANNER_STATS_CACHE and not refresh:
        return dict(_BANNER_STATS_CACHE)
    stats: Dict[str, Any] = {
        "model": _probe_model_name(),
        "skills": _probe_skill_count(),
        "tools": _probe_tool_count(),
        "sessions": _probe_session_count(),
    }
    _BANNER_STATS_CACHE.clear()
    _BANNER_STATS_CACHE.update(stats)
    return dict(stats)


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------


def _is_interactive_invocation(argv: Sequence[str]) -> bool:
    """Decide whether this invocation should drive the interactive loop.

    Interactive entry requires:

    * Both stdin and stdout are TTYs (no piped / redirected I/O).
    * Either no arguments at all, or exactly the ``chat`` subcommand.

    Anything else — a flag (``-p``, ``--json``, ``--help``), a recognised
    subcommand (``serve``, ``run``, ``alpha``, ``hypothesis`` ...), or an
    unknown positional — is delegated to ``_legacy.main`` so argparse
    can either dispatch it or produce its standard "unrecognized
    arguments" error. Routing typos here would silently drop the user
    into chat, which is worse than the argparse error.

    Args:
        argv: The argument list passed to :func:`main`, *excluding*
            ``sys.argv[0]``.

    Returns:
        ``True`` if the caller should drive the interactive loop.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    if not argv:
        return True
    if argv[0].startswith("-"):
        return False
    return _is_supported_chat_invocation(argv)


def _is_supported_chat_invocation(argv: Sequence[str]) -> bool:
    """Return True for chat invocations handled by the interactive front door."""
    if not argv or argv[0] != "chat":
        return False
    if len(argv) == 1:
        return True
    if len(argv) == 2 and argv[1].startswith("--max-iter="):
        try:
            int(argv[1].split("=", 1)[1])
            return True
        except ValueError:
            return False
    if len(argv) == 3 and argv[1] == "--max-iter":
        try:
            int(argv[2])
            return True
        except ValueError:
            return False
    return False


def _maybe_run_onboarding() -> bool:
    """Run the first-launch wizard when ``.env`` is missing.

    Returns:
        ``True`` if startup should proceed, ``False`` if the user cancelled
        the wizard cleanly.
    """
    if _ENV_PATH.exists():
        return True
    console = get_console()
    written = run_onboarding(console=console)
    if written is None:
        return False
    # Reload env so downstream code picks up the fresh credentials.
    try:
        from dotenv import load_dotenv

        load_dotenv(written, override=True)
    except Exception:  # noqa: BLE001 — legacy will load again later
        pass
    return True


def _show_banner() -> None:
    """Print the welcome banner using best-effort stat probes."""
    stats = _collect_banner_stats()
    print_banner(get_console(), **stats)


# ---------------------------------------------------------------------------
# Interactive context
# ---------------------------------------------------------------------------


@dataclass
class InteractiveContext:
    """State bag handed to slash-command handlers and the run loop.

    Attributes:
        session_id: Active session id (populated lazily on first turn).
        history: Compact transcript (role/content pairs) fed back to the
            agent for follow-up context.
        max_iter: ReAct iteration ceiling.
        debug: Whether the ``/debug`` panel is currently shown.
        pending_prompt: Optional prompt queued by a slash handler
            (``/journal``, ``/shadow``) that the loop should execute as
            the next user turn. Consumed by :func:`_interactive_loop`
            and cleared.
    """

    session_id: Optional[str] = None
    history: List[Dict[str, str]] = field(default_factory=list)
    max_iter: int = 50
    debug: bool = False
    pending_prompt: Optional[str] = None


# ---------------------------------------------------------------------------
# Session-store integration
# ---------------------------------------------------------------------------


def _session_store() -> Any:
    """Return a process-wide :class:`SessionStore` rooted at ``agent/sessions``.

    Cached on the module so repeat ``_append_message`` / ``_new_session``
    calls don't re-import ``src.session.store`` every turn.
    """
    global _SESSION_STORE_CACHE
    if _SESSION_STORE_CACHE is None:
        from cli._legacy import SESSIONS_DIR  # filesystem path constant
        from src.session.store import SessionStore

        _SESSION_STORE_CACHE = SessionStore(base_dir=SESSIONS_DIR)
    return _SESSION_STORE_CACHE


def _new_session(prompt_preview: str) -> Optional[str]:
    """Create a fresh session record. Returns the id, or None on failure.

    Dual-writes to the filesystem :class:`SessionStore` (canonical JSONL
    log under ``agent/sessions/``) *and* to the SQLite FTS5 search index
    (``~/.vibe-trading/sessions.db``) so cross-session search via
    :class:`SessionSearchIndex` finds turns recorded from the interactive
    loop. Matches the pattern in :class:`SessionService`.
    """
    title = prompt_preview[:60] or "untitled"
    try:
        from src.session.models import Session, SessionStatus

        store = _session_store()
        session = Session(
            title=title,
            status=SessionStatus.ACTIVE,
        )
        store.create_session(session)
    except Exception:  # noqa: BLE001 — never block the turn on persistence
        return None

    # Index in SQLite for FTS5 cross-session search. Best-effort — never
    # block the turn if the search index is unavailable.
    try:
        from src.session.search import get_shared_index

        get_shared_index().index_session(session.session_id, title)
    except Exception:  # noqa: BLE001
        pass
    return session.session_id


def _append_message(session_id: str, role: str, content: str) -> None:
    """Append a single message to the session JSONL log + FTS5 index.

    Dual-writes:

    * Canonical: append the :class:`Message` to ``messages.jsonl`` via
      the filesystem :class:`SessionStore`. ``_maybe_resume_last_session``
      and the legacy ``sessions`` CLI both read from here.
    * Search index: insert the same row into the SQLite FTS5 index so
      ``SessionSearchTool`` finds it. Required for the CLAUDE.md promise
      that cross-session full-text search works.

    Args:
        session_id: Active session id. Skipped if empty.
        role: ``"user"`` / ``"assistant"`` / ``"tool"``.
        content: Message text. Empty/whitespace strings are skipped.
    """
    if not session_id or not content:
        return
    try:
        from src.session.models import Message

        store = _session_store()
        store.append_message(
            Message(session_id=session_id, role=role, content=content)
        )
    except Exception:  # noqa: BLE001 — persistence is best-effort
        pass

    # FTS5 cross-session search. Independent try/except so a JSONL write
    # that succeeded is not retried just because the search index failed.
    try:
        from src.session.search import get_shared_index

        get_shared_index().index_message(session_id, role, content)
    except Exception:  # noqa: BLE001
        pass


def _maybe_resume_last_session(console: Any) -> Optional[Dict[str, Any]]:
    """Prompt to resume the most recent session, if any exist.

    Returns:
        A dict ``{"session_id": str, "history": list[dict], "title": str}``
        when the user opts to resume, otherwise ``None`` (new session).
    """
    try:
        store = _session_store()
        sessions = store.list_sessions(limit=1)
    except Exception:  # noqa: BLE001
        return None
    if not sessions:
        return None

    last = sessions[0]
    title = last.title or "(untitled)"
    console.print()
    console.print(
        f"[dim]Resume last session ({title})? (r)esume / (n)ew (default: new)[/dim]"
    )
    try:
        choice = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice not in {"r", "resume", "y", "yes"}:
        return None

    try:
        messages = store.get_messages(last.session_id, limit=_HISTORY_RETAINED_TURNS * 2)
    except Exception:  # noqa: BLE001
        messages = []
    history = [
        {"role": m.role, "content": m.content}
        for m in messages
        if m.role in {"user", "assistant"} and m.content.strip()
    ]
    return {
        "session_id": last.session_id,
        "history": history[-_HISTORY_RETAINED_TURNS:],
        "title": title,
    }


# ---------------------------------------------------------------------------
# Async preflight
# ---------------------------------------------------------------------------


def _start_preflight_async() -> threading.Thread:
    """Run ``src.preflight.run_preflight`` in a daemon thread.

    The welcome banner has already painted by the time this runs, so the
    user sees something useful immediately while credential / network
    probes happen in the background. We swallow exceptions because the
    legacy path runs preflight again before any agent invocation — this
    pre-warm is opportunistic. Audit item 11.
    """
    def _worker() -> None:
        try:
            from src.preflight import run_preflight

            run_preflight(get_console())
        except Exception:  # noqa: BLE001
            pass

    thread = threading.Thread(target=_worker, daemon=True, name="vibe-preflight")
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Slash dispatch
# ---------------------------------------------------------------------------


# Module paths that fan out to multiple commands via ``run(ctx, name, *args)``.
_MULTI_COMMAND_MODULES = frozenset({
    "cli.commands.chat",
    "cli.commands.show",
    "cli.commands.session",
})


def _suggest_commands(unknown: str) -> List[str]:
    """Return up to three "did you mean" suggestions for ``unknown``.

    Uses :func:`difflib.get_close_matches` (edit-distance based) as the
    primary signal so single-character typos like ``/historu`` → ``/history``
    or ``/jurnal`` → ``/journal`` are caught — the subsequence scorer in
    ``slash_router.match_commands`` ranks transpositions poorly and is
    used here only as a fallback to fill any remaining slot.

    Args:
        unknown: The bare command token (no leading ``/``).

    Returns:
        Deduplicated command names, edit-distance suggestions first,
        capped at three entries.
    """
    import difflib

    from cli.commands.slash_router import SLASH_COMMANDS, match_commands

    all_names = [cmd.name for cmd in SLASH_COMMANDS]
    primary = difflib.get_close_matches(unknown, all_names, n=3, cutoff=0.6)

    # Fill remaining slots from the subsequence scorer so very short
    # typos (``/hi`` → ``/history``) still surface if difflib found
    # nothing close enough.
    suggestions: list[str] = list(primary)
    if len(suggestions) < 3:
        for cmd in match_commands("/" + unknown):
            if cmd.name not in suggestions:
                suggestions.append(cmd.name)
                if len(suggestions) >= 3:
                    break
    return suggestions[:3]


def _dispatch_slash(line: str, ctx: InteractiveContext) -> int:
    """Route a slash command line to its handler.

    Returns:
        Exit code from the handler. ``2`` is the conventional "user
        requested quit" sentinel (see ``cmd_quit``). Any other value is
        treated as continue-the-loop.
    """
    from cli.commands.slash_router import find_exact

    console = get_console()
    stripped = line.lstrip().lstrip("/")
    parts = stripped.split()
    if not parts:
        console.print("[dim]Type /help to see available commands.[/dim]")
        return 0
    name, *args = parts
    cmd = find_exact(name)
    if cmd is None:
        # Use edit-distance suggestions (difflib) so single-char typos
        # surface the right command — the subsequence scorer in
        # ``slash_router.match_commands`` does not handle transpositions.
        suggestions = _suggest_commands(name)
        console.print(f"[bold red]Unknown command:[/] /{name}")
        if suggestions:
            preview = ", ".join(f"/{s}" for s in suggestions)
            console.print(f"[dim]Did you mean: {preview}?[/dim]")
        console.print("[dim]Type /help to see available commands.[/dim]")
        return 0

    try:
        module = importlib.import_module(cmd.handler_module)
    except ImportError as exc:
        console.print(
            f"[bold red]Failed to load /{cmd.name} handler ({exc})[/bold red]"
        )
        return 0

    try:
        if cmd.handler_module in _MULTI_COMMAND_MODULES:
            return int(module.run(ctx, cmd.name, *args))
        return int(module.run(ctx, *args))
    except SystemExit as exc:
        # Treat the canonical "user quit" codes (0 / 2 / None) as
        # a loop break; anything else is a genuine handler failure
        # and should keep the REPL alive so the user can recover.
        code = exc.code
        if code in (None, 0, 2):
            return 2
        console.print(f"[bold red]/{cmd.name} exited with code {code}[/]")
        return 0
    except Exception as exc:  # noqa: BLE001 — never let a handler kill the loop
        console.print(f"[bold red]/{cmd.name} raised {type(exc).__name__}: {exc}[/]")
        return 0


# ---------------------------------------------------------------------------
# Interactive loop
# ---------------------------------------------------------------------------


def _summarise_tool_result(tool: str, status: str, preview: str) -> str:
    """Short suffix to render after a finished tool event."""
    if status != "ok":
        return preview[:48].replace("\n", " ")
    # Delegate to the legacy preview helper which already knows about
    # backtest sharpe / shadow id / etc.
    try:
        from cli._legacy import _format_tool_result_preview, _strip_rich_tags

        return _strip_rich_tags(_format_tool_result_preview(tool, status, preview))[:48]
    except Exception:  # noqa: BLE001
        return ""


def _print_debug_summary(
    console: Any,
    result: Dict[str, Any],
    elapsed: float,
    ctx: InteractiveContext,
) -> None:
    """Render the one-line ``[debug]`` summary after a turn.

    Reads the iteration count from ``result`` (populated by
    :class:`src.agent.loop.AgentLoop`), counts tool events from
    ``react_trace`` when present, and approximates the post-turn
    context size from ``ctx.history``. Best-effort — any missing field
    falls back to ``?`` so the summary still prints.
    """
    iterations = result.get("iterations", "?")
    trace = result.get("react_trace") or []
    if isinstance(trace, list):
        tools = sum(
            1
            for entry in trace
            if isinstance(entry, dict) and entry.get("type") == "tool_result"
        )
    else:
        tools = "?"
    history_chars = sum(len(m.get("content", "")) for m in ctx.history)
    # Rough char-to-token ratio (matches the loop's estimator).
    approx_tokens = history_chars // 4
    console.print(
        f"[dim][debug] iter={iterations} tools={tools} "
        f"elapsed={elapsed:.1f}s ctx≈{approx_tokens}tok ({history_chars}ch)[/dim]"
    )


def _run_one_turn(user_input: str, ctx: InteractiveContext) -> None:
    """Execute a single agent turn with the Rich dashboard.

    Routes through :func:`cli._legacy._run_agent` so all tool callbacks,
    persistent memory, and the ReAct engine remain untouched — we only
    swap in the Rich dashboard and persist the turn to ``SessionStore``.
    """
    from cli._legacy import _RunDashboard, _print_result, _run_agent
    from rich.live import Live

    console = get_console()

    if ctx.session_id is None:
        ctx.session_id = _new_session(user_input)
    _append_message(ctx.session_id or "", "user", user_input)

    start = time.perf_counter()
    dashboard = _RunDashboard(user_input, ctx.max_iter)
    # ``transient=False`` — keep the final timeline visible after the run
    # completes. Audit item 4.
    try:
        with Live(
            dashboard.render(),
            console=console,
            refresh_per_second=6,
            transient=False,
        ) as live:
            dashboard.live = live
            result = _run_agent(
                user_input,
                history=ctx.history[-_HISTORY_RETAINED_TURNS:],
                max_iter=ctx.max_iter,
                dashboard=dashboard,
            )
    except (KeyboardInterrupt, BrokenPipeError):
        # BrokenPipe: caller did ``vibe-trading chat | head`` and the
        # downstream pipe closed mid-render. Print may itself fail on
        # the closed fd, so swallow that defensively too.
        try:
            console.print("\n[yellow]Interrupted[/yellow]")
        except (BrokenPipeError, OSError):
            pass
        return

    elapsed = time.perf_counter() - start
    _print_result(result, elapsed)

    ctx.history.append({"role": "user", "content": user_input})
    answer = (result.get("content") or "").strip()
    if answer:
        ctx.history.append({"role": "assistant", "content": answer})
        _append_message(ctx.session_id or "", "assistant", answer)

    if ctx.debug:
        _print_debug_summary(console, result, elapsed, ctx)


def _print_input_hint(console: Any, hint: str) -> None:
    """Render the bottom hint bar in muted style."""
    from cli.components.hint_bar import render_hint_bar

    console.print(render_hint_bar(left=hint, right="Ctrl+D · /quit to exit"))


def _interactive_loop(max_iter: int) -> int:
    """Drive the new interactive REPL.

    Returns:
        Process exit code (always ``0`` on a clean exit).
    """
    console = get_console()

    # Warm up the preflight in a background thread so the wait is hidden.
    _start_preflight_async()

    ctx = InteractiveContext(max_iter=max_iter)

    # Offer to resume the most recent session. Audit item 8.
    resume = _maybe_resume_last_session(console)
    if resume is not None:
        ctx.session_id = resume["session_id"]
        ctx.history = list(resume["history"])
        console.print(
            f"[dim]Resumed session: {resume['title']} ({len(ctx.history)} prior turns)[/dim]"
        )

    # Build the prompt session once so history + completer persist.
    try:
        from cli.input import ctrl_c_within_window, make_session
    except Exception as exc:  # noqa: BLE001 — fall back gracefully if prompt_toolkit broken
        console.print(f"[red]Failed to initialise input layer: {exc}[/red]")
        console.print("[dim]Falling back to legacy interactive loop.[/dim]")
        from cli._legacy import cmd_interactive

        try:
            cmd_interactive(max_iter)
        except Exception:  # noqa: BLE001
            return 1
        return 0

    session = make_session()
    _print_input_hint(console, "↑/↓ history · / for commands · Esc-Enter newline")

    while True:
        try:
            user_input = session.prompt("> ")
        except KeyboardInterrupt:
            # Should not reach here — the keybinding raises EOFError instead.
            continue
        except EOFError:
            # Two interpretations: Ctrl+D (always exit), or Ctrl+C on an
            # empty line (show hint, exit on second press).
            #
            # ``ctrl_c_within_window`` reads a press-time decision cached
            # by the keybinding: True iff the gap between the *previous*
            # Ctrl+C press and *this* one is < 2 s. First press → False
            # (no prior press) → we print the hint and continue. Second
            # press inside the window → True → we break.
            if ctrl_c_within_window(session, window_sec=2.0):
                break
            console.print(
                "[dim]Press Ctrl+C again within 2s, Ctrl+D, or type /quit to exit[/dim]"
            )
            continue

        text = user_input.strip()
        if not text:
            continue

        # Slash command path.
        if text.startswith("/"):
            rc = _dispatch_slash(text, ctx)
            if rc == 2:
                break
            # A handler may queue an agent prompt (``/journal <path>``,
            # ``/shadow ...``). Drain it here so the slash command turns
            # into a real turn without round-tripping through stdin.
            queued = ctx.pending_prompt
            if queued:
                ctx.pending_prompt = None
                _run_one_turn(queued, ctx)
            continue

        # Natural-language path — drive the agent.
        _run_one_turn(text, ctx)

    console.print("[dim]Goodbye[/dim]")
    return 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint returning a process exit code.

    Behaviour:

    * Interactive entry (no subcommand, or ``chat`` + TTY): show banner,
      run onboarding wizard if needed, then drop into the interactive
      loop driven by ``cli/input.py``, ``cli/completer.py``,
      and ``cli/commands/*``.
    * Non-interactive entry (``serve``, ``run -p ...``, ``mcp``,
      ``swarm``, piped stdin, etc.): pass through to ``cli._legacy.main``
      so every existing subcommand keeps working unchanged.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    interactive = _is_interactive_invocation(raw_argv)

    if interactive:
        if not _maybe_run_onboarding():
            return 0
        _show_banner()
        # Strip the optional ``chat`` token + any ``--max-iter`` flag so
        # the new loop can read them directly without re-parsing argv.
        max_iter = _extract_max_iter(raw_argv, default=50)
        return _interactive_loop(max_iter)

    # Delegate every other path to the legacy dispatcher.
    try:
        from cli import _legacy
    except ImportError as exc:  # pragma: no cover — packaging error
        get_console().print(
            f"  Internal error: cannot import cli._legacy ({exc}).",
            style=Theme.danger,
        )
        return 2

    return int(_legacy.main(raw_argv))


def _extract_max_iter(argv: Sequence[str], *, default: int) -> int:
    """Pull ``--max-iter <N>`` (or ``--max-iter=N``) out of ``argv``.

    The legacy argparse setup accepts ``--max-iter`` both at the top
    level and as ``chat --max-iter``. We just need the integer; the
    presence/absence of ``chat`` was already determined upstream.
    """
    it = iter(range(len(argv)))
    for i in it:
        token = argv[i]
        if token == "--max-iter" and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return default
        if token.startswith("--max-iter="):
            try:
                return int(token.split("=", 1)[1])
            except ValueError:
                return default
    return default


def _entrypoint() -> None:
    """Thin wrapper so the console script and ``python -m cli.main`` agree."""
    sys.exit(main())


# ---------------------------------------------------------------------------
# Optional typer integration (only used by ``python -m cli.main --help``)
# ---------------------------------------------------------------------------


def _build_typer_app():  # type: ignore[no-untyped-def]
    """Build a typer app that mirrors the legacy surface. Best-effort only."""
    try:
        import typer
    except ImportError:
        return None

    app = typer.Typer(
        add_completion=False,
        no_args_is_help=False,
        help="Vibe-Trading — natural-language finance research agent.",
        rich_markup_mode=None,
    )

    @app.callback(invoke_without_command=True)
    def _default(ctx: typer.Context) -> None:  # noqa: ANN001
        if ctx.invoked_subcommand is None:
            sys.exit(main(ctx.args))

    @app.command("chat", help="Start the interactive ReAct chat loop.")
    def _chat(
        max_iter: int = typer.Option(50, "--max-iter", help="Maximum ReAct iterations."),
    ) -> None:
        sys.exit(main(["chat", "--max-iter", str(max_iter)]))

    @app.command("serve", help="Start the FastAPI server.")
    def _serve(
        host: str = typer.Option("0.0.0.0", "--host"),
        port: int = typer.Option(8000, "--port"),
        dev: bool = typer.Option(False, "--dev", help="Also boot the Vite dev server."),
    ) -> None:
        forwarded = ["serve", "--host", host, "--port", str(port)]
        if dev:
            forwarded.append("--dev")
        sys.exit(main(forwarded))

    @app.command("list", help="List recent runs.")
    def _list(limit: int = typer.Option(20, "--limit")) -> None:
        sys.exit(main(["list", "--limit", str(limit)]))

    @app.command("show", help="Show a recorded run by id.")
    def _show(run_id: str = typer.Argument(...)) -> None:
        sys.exit(main(["show", run_id]))

    @app.command("init", help="Re-run the interactive setup wizard.")
    def _init() -> None:
        run_onboarding(console=get_console())

    return app


# ``python -m cli.main`` support — uses typer help if available, else main().
if __name__ == "__main__":
    typer_app = _build_typer_app()
    if typer_app is not None:
        typer_app()
    else:
        _entrypoint()


__all__ = ["main", "InteractiveContext"]
