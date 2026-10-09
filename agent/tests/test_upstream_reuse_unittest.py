"""Offline regressions for the bounded upstream compatibility patches."""
from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from backtest.metrics import calc_metrics, win_rate_and_stats
from backtest.run_card import write_run_card
from backtest.validation import _path_metrics
from src.tools.backtest_summary import build_backtest_summary
from src.tools.run_artifact_tool import read_run_artifact


class MetricsRegression(unittest.TestCase):
    def test_no_loss_ratios_are_missing_not_zero(self):
        for pnls in ([10, 20], [0, 0], [10, 0]):
            trades = [SimpleNamespace(pnl=p, holding_bars=1) for p in pnls]
            stats = win_rate_and_stats(trades)
            self.assertIsNone(stats["profit_factor"])
            self.assertIsNone(stats["profit_loss_ratio"])
        self.assertEqual(win_rate_and_stats([])["profit_factor"], 0)

    def test_first_bar_loss_is_counted(self):
        equity = pd.Series([90., 95.], index=pd.date_range("2025-01-01", periods=2))
        metrics = calc_metrics(equity, [], 100, 252)
        self.assertAlmostEqual(metrics["max_drawdown"], -.1)
        expected_returns = np.array([-.1, 95 / 90 - 1])
        expected = expected_returns.mean() / np.sqrt(np.mean(np.minimum(expected_returns, 0) ** 2)) * np.sqrt(252)
        self.assertAlmostEqual(metrics["sortino"], expected, places=4)

    def test_single_bar_metrics_finite(self):
        values = calc_metrics(pd.Series([90.], index=pd.date_range("2025-01-01", periods=1)), [], 100, 252)
        self.assertTrue(all(v is None or np.isfinite(v) for v in values.values()))
        self.assertAlmostEqual(values["max_drawdown"], -.1)

    def test_bad_equity_rejected(self):
        for values in ([np.nan], [-1.], [0., 10.]):
            with self.assertRaises(ValueError):
                calc_metrics(pd.Series(values), [], 100, 252)

    def test_monte_carlo_first_loss_and_empty(self):
        self.assertAlmostEqual(_path_metrics(np.array([-10., 5.]), 100)["max_dd"], -.1)
        self.assertEqual(_path_metrics(np.array([]), 100)["max_dd"], 0)

    def test_options_metrics_same_initial_drawdown(self):
        from backtest.engines.options_portfolio import _calc_options_metrics
        values = _calc_options_metrics(pd.Series([90., 95.]), 100, [{"pnl": 5}], 252)
        self.assertAlmostEqual(values["max_drawdown"], -.1)
        self.assertIsNone(values["profit_factor"])


class FactorMissingData(unittest.TestCase):
    def test_all_count_factors_keep_gaps_and_warmup(self):
        for n in (5, 10, 20, 30, 60):
            for kind, expected in (("cntp", 1.), ("cntn", 0.), ("cntd", 1.)):
                with self.subTest(kind=kind, n=n):
                    compute = importlib.import_module(f"src.factors.zoo.qlib158.{kind}{n}").compute
                    close = pd.DataFrame({"A": np.arange(2 * n + 10, dtype=float), "B": np.ones(2 * n + 10)})
                    clean = compute({"close": close})
                    self.assertTrue(clean.iloc[:n].isna().all().all())
                    self.assertAlmostEqual(clean.iloc[n]["A"], expected)
                    self.assertEqual(clean.iloc[n]["B"], 0.)
                    close.loc[n + 1, "A"] = np.nan
                    result = compute({"close": close})
                    self.assertTrue(result.loc[n + 1:2 * n + 1, "A"].isna().all())
                    self.assertAlmostEqual(result.loc[2 * n + 2, "A"], expected)
                    pd.testing.assert_series_equal(result["B"], clean["B"])


class MarketBasisAndCalendar(unittest.TestCase):
    def test_source_status_is_not_a_live_health_claim_or_probe(self):
        import market_data_service as service
        with patch.object(service, "_massive_get", side_effect=AssertionError("status GET must not fetch")):
            result = service.data_source_status()
        self.assertTrue(result["configuration_only"])
        self.assertEqual(result["daily_cache_price_basis"], "raw_unadjusted")
        self.assertTrue(all(v in {"configured_unprobed", "not_configured"} for v in result["provider_configuration"].values()))

    def test_bulk_massive_uses_raw_cache_basis(self):
        import market_data_service as service
        with patch.object(service, "_massive_get", return_value={"results": [{"T": "NVDA", "o": 10, "h": 12, "l": 9, "c": 11, "v": 100} ]}) as fetch:
            result = service.massive_grouped_daily("2026-10-07")
            self.assertEqual(fetch.call_args.kwargs["adjusted"], "false")
            self.assertEqual(result["NVDA"]["close"], 11)

    def test_early_close_market_cache_gate(self):
        from market_calendar import most_recent_session, session_close_et
        from market_data_service import get_latest_us_market_close_utc
        before = datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc)
        after = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(most_recent_session(before), date(2026, 11, 25))
        self.assertEqual(most_recent_session(after), date(2026, 11, 27))
        self.assertEqual(get_latest_us_market_close_utc(after), after)
        self.assertEqual(session_close_et(date(2026, 7, 2)).hour, 16)
        self.assertEqual(session_close_et(date(2027, 7, 2)).hour, 16)
        self.assertEqual(session_close_et(date(2028, 7, 3)).hour, 13)


class ArtifactPaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "run"
        (self.root / "artifacts").mkdir(parents=True)
        (self.root / "config.json").write_text('{"source":"yfinance"}', encoding="utf-8")
        pd.DataFrame({"timestamp": [f"day{i}" for i in range(100)], "equity": range(100, 200)}).to_csv(self.root / "artifacts/equity.csv", index=False)
        pd.DataFrame([{"win_rate": .5, "profit_factor": None, "trade_count": 2}]).to_csv(self.root / "artifacts/metrics.csv", index=False)
        (self.root / "artifacts/wide.csv").write_text("text\n" + "x" * 12000 + "\n", encoding="utf-8")
        self.card = write_run_card(self.root, {"source": "yfinance", "codes": ["NVDA"]}, {"win_rate": .5, "validation": {"walk_forward": {"oos": .1}}})
        self.env = patch.dict(os.environ, {"VIBE_TRADING_ALLOWED_RUN_ROOTS": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def read(self, artifact="equity", **kwargs):
        return json.loads(read_run_artifact(str(self.root), artifact, **kwargs))

    def test_paging_and_projection(self):
        first = self.read(limit=7, columns=["equity"])
        second = self.read(offset=first["next_offset"], limit=7, columns=["equity"])
        self.assertEqual(first["total_rows"], 100)
        self.assertEqual(first["rows"][-1], {"equity": 106})
        self.assertEqual(second["rows"][0], {"equity": 107})
        self.assertTrue(first["verified"])

    def test_preview_preserves_endpoints(self):
        sample = self.read(mode="downsample", limit=10)
        self.assertEqual(len(sample["rows"]), 10)
        self.assertEqual(sample["rows"][0]["equity"], 100)
        self.assertEqual(sample["rows"][-1]["equity"], 199)

    def test_metadata_and_bad_columns(self):
        self.assertEqual(self.read(mode="meta")["rows"], [])
        self.assertEqual(self.read(columns=["wrong"])["status"], "error")

    def test_escape_unlisted_and_non_csv_rejected(self):
        for name in ("../config.json", "/etc/passwd", "C:/secret.csv", "//host/share.csv", "artifacts/missing.csv", "config.json"):
            self.assertEqual(self.read(name)["status"], "error")

    def test_tampered_same_size_rejected(self):
        path = self.root / "artifacts/equity.csv"
        path.write_text(path.read_text().replace("day0", "DAY0"), encoding="utf-8")
        self.assertIn("mismatch", self.read()["error"])

    def test_wide_row_never_returns_broken_json(self):
        self.assertEqual(self.read("artifacts/wide.csv")["status"], "error")

    def test_summary_grounded_metrics_and_validation(self):
        summary = build_backtest_summary(self.root)
        self.assertEqual(summary["metrics"]["trade_count"], 2)
        self.assertIsNone(summary["metrics"]["profit_factor"])
        self.assertEqual(summary["validation"], {"walk_forward": {"oos": .1}})
        self.assertEqual(len(summary["equity_preview"]), 10)

    def test_saved_json_can_be_recovered_without_recursive_truncation(self):
        from src.tools.read_file_tool import ReadFileTool
        path = self.root / "logs/evidence.json"
        path.parent.mkdir()
        raw = json.dumps({"status": "ok", "rows": ["value" * 5000]})
        path.write_text(raw, encoding="utf-8")
        recovered, offset = "", 0
        while offset is not None:
            page = ReadFileTool().execute(path="logs/evidence.json", run_dir=str(self.root), max_chars=2000, offset_chars=offset)
            self.assertLess(len(page), 10000)
            payload = json.loads(page)
            self.assertEqual(payload["status"], "ok")
            recovered += payload["content"]
            offset = payload["next_offset_chars"]
        self.assertEqual(recovered, raw)

    def test_backtest_envelope_and_best_effort_summary(self):
        from src.tools.backtest_tool import run_backtest
        (self.root / "code").mkdir()
        (self.root / "code/signal_engine.py").write_text("# synthetic test", encoding="utf-8")
        fake = SimpleNamespace(success=True, exit_code=0, stdout="done", stderr="", artifacts={})
        with patch("src.tools.backtest_tool.Runner") as runner:
            runner.return_value.execute.return_value = fake
            payload = json.loads(run_backtest(str(self.root)))
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["stdout"], "done")
            self.assertIn("summary", payload)
            (self.root / "run_card.json").unlink()
            payload = json.loads(run_backtest(str(self.root)))
            self.assertEqual(payload["status"], "ok")
            self.assertIn("summary_warning", payload)
            fake.success = False
            payload = json.loads(run_backtest(str(self.root)))
            self.assertNotIn("summary", payload)


class AgentEvidence(unittest.TestCase):
    def test_projection_preserves_distinct_metadata_errors_and_types(self):
        from src.tools.mcp import _agent_result_projection
        data = {"price": 42}
        blocks = [{"type": "text", "text": json.dumps(data)}, {"type": "text", "text": "warning"}]
        payload = {"status": "ok", "data": data, "structured_content": {"result": data}, "content": blocks, "text": blocks[0]["text"] + "\nwarning", "server": "test"}
        result = _agent_result_projection(payload)
        self.assertNotIn("structured_content", result)
        self.assertEqual(result["text"], "warning")
        self.assertEqual(len(payload["content"]), 2)
        self.assertEqual(result["server"], "test")
        for extra in ({"annotations": {"audience": ["assistant"]}}, {"_meta": {"id": 3}}):
            block = {"type": "text", "text": json.dumps(data), **extra}
            projected = _agent_result_projection({"status": "ok", "data": data, "content": [block]})
            self.assertEqual(projected["content"], [block])
        for other in (True, 1.0, "1"):
            projected = _agent_result_projection({"status": "ok", "data": 1, "structured_content": other})
            self.assertIn("structured_content", projected)
        error = {"status": "error", "data": data, "structured_content": data}
        self.assertEqual(_agent_result_projection(error), error)

    def test_budget_counts_tools_and_chinese(self):
        from src.agent.loop import estimate_tokens
        english = [{"role": "user", "content": "x" * 1000}]
        chinese = [{"role": "user", "content": "股" * 1000}]
        self.assertGreater(estimate_tokens(chinese), estimate_tokens(english))
        self.assertGreater(estimate_tokens(english, [{"schema": "x" * 5000}]), estimate_tokens(english))

    def test_small_context_not_pruned_and_unseen_batch_protected(self):
        from src.agent.loop import _microcompact, _context_collapse
        messages = [{"role": "system", "content": "system"}] + [
            {"role": "tool", "tool_call_id": f"tc{i}", "content": "x" * 5000} for i in range(10)]
        _microcompact(messages, budget=100000)
        self.assertTrue(all(m["content"] != "[cleared]" for m in messages))
        _microcompact(messages, budget=1000, protected_ids={"tc1"})
        self.assertEqual(messages[1]["content"], "[cleared]")
        self.assertEqual(messages[2]["content"], "x" * 5000)
        _context_collapse(messages, protected_ids={"tc1"})
        self.assertEqual(messages[2]["content"], "x" * 5000)

    def test_large_result_saved_and_json_remains_valid(self):
        from src.agent.loop import AgentLoop
        from src.agent.memory import WorkspaceMemory
        from src.agent.tools import ToolRegistry
        from src.agent.context import ContextBuilder
        with tempfile.TemporaryDirectory() as directory:
            memory = WorkspaceMemory(run_dir=directory)
            agent = AgentLoop(ToolRegistry(), SimpleNamespace(), memory=memory)
            context = SimpleNamespace(format_tool_result=lambda id, name, content: {"role": "tool", "tool_call_id": id, "content": content})
            trace = SimpleNamespace(write=lambda data: None)
            messages = []
            raw = json.dumps({"status": "ok", "rows": ["value" * 5000]})
            agent._finalize_tool_result(SimpleNamespace(id="../../untrusted", name="test"), raw, 1, context, messages, trace, [], 1)
            result = json.loads(messages[0]["content"])
            self.assertTrue(result["truncated"])
            self.assertEqual(Path(result["result_path"]).read_text(encoding="utf-8"), raw)
            self.assertLess(len(messages[0]["content"]), 10000)
            self.assertIn("../../untrusted", agent._pending_tool_ids)

    def test_pruned_evidence_retains_recovery_path(self):
        from src.agent.loop import _microcompact
        messages = [{"role": "tool", "tool_call_id": f"id{i}", "content": "x" * 4000} for i in range(8)]
        _microcompact(messages, budget=100, result_paths={"id0": "/run/logs/tool_results/first.json"})
        self.assertEqual(json.loads(messages[0]["content"])["result_path"], "/run/logs/tool_results/first.json")

    def test_auto_compact_keeps_entire_unseen_tool_batch(self):
        from src.agent.loop import AgentLoop
        from src.agent.memory import WorkspaceMemory
        from src.agent.tools import ToolRegistry
        with tempfile.TemporaryDirectory() as directory:
            llm = SimpleNamespace(chat=lambda messages: SimpleNamespace(content="summary"))
            agent = AgentLoop(ToolRegistry(), llm, memory=WorkspaceMemory(run_dir=directory))
            calls = [{"id": f"tc{i}", "function": {"name": "read_file", "arguments": "{}"}} for i in range(8)]
            agent._pending_tool_ids = {c["id"] for c in calls}
            messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "old" * 3000},
                        {"role": "assistant", "tool_calls": calls, "content": ""}] + [
                        {"role": "tool", "tool_call_id": c["id"], "content": "x" * 12000} for c in calls]
            agent._auto_compact(messages, Path(directory), SimpleNamespace(write=lambda item: None))
            for message in messages:
                if message.get("role") == "tool":
                    self.assertEqual(message["content"], "x" * 12000)
            self.assertEqual(sum(m.get("role") == "tool" for m in messages), 8)


if __name__ == "__main__":
    unittest.main()
