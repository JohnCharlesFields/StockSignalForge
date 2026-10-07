"""Security regression tests for run_dir-based file tools."""

from __future__ import annotations

import json
from pathlib import Path

from src.tools.backtest_tool import run_backtest
from src.tools.edit_file_tool import EditFileTool
from src.tools.read_file_tool import ReadFileTool
from src.tools.write_file_tool import WriteFileTool


def _body(raw: str) -> dict:
    """Parse a JSON tool response."""
    return json.loads(raw)


def test_write_file_rejects_unconfigured_absolute_run_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", raising=False)

    body = _body(WriteFileTool().execute(
        path="code/signal_engine.py",
        content="print('nope')",
        run_dir=str(tmp_path),
    ))

    assert body["status"] == "error"
    assert "outside allowed run roots" in body["error"]
    assert not (tmp_path / "code" / "signal_engine.py").exists()


def test_read_and_edit_file_accept_configured_run_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    target = tmp_path / "run" / "notes.md"
    target.parent.mkdir(parents=True)
    target.write_text("alpha beta", encoding="utf-8")

    read_body = _body(ReadFileTool().execute(path="notes.md", run_dir=str(target.parent)))
    edit_body = _body(EditFileTool().execute(
        path="notes.md",
        old_text="beta",
        new_text="gamma",
        run_dir=str(target.parent),
    ))

    assert read_body["status"] == "ok"
    assert "alpha beta" in read_body["content"]
    assert edit_body["status"] == "ok"
    assert target.read_text(encoding="utf-8") == "alpha gamma"


def test_backtest_rejects_unconfigured_absolute_run_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", raising=False)
    (tmp_path / "code").mkdir()
    (tmp_path / "config.json").write_text('{"source":"auto","codes":["AAPL"]}', encoding="utf-8")
    (tmp_path / "code" / "signal_engine.py").write_text(
        "class SignalEngine:\n    def generate(self, data_map):\n        return {}\n",
        encoding="utf-8",
    )

    body = _body(run_backtest(str(tmp_path)))

    assert body["status"] == "error"
    assert "outside allowed run roots" in body["error"]
