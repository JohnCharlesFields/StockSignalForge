"""Public-copy defaults must not assume private data or migrate the old DB."""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_runner_context_is_only_in_step_environment():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    job = workflow["jobs"]["test"]
    assert "runner." not in json.dumps(job.get("env", {}))
    tests = next(step for step in job["steps"] if step.get("name") == "Run tests")
    assert "runner.temp" in tests["env"]["VIBE_MARKET_DATA_CACHE_DIR"]


def test_private_training_is_not_automatically_enabled():
    config = json.loads((ROOT / "agent/config/pullback_parameters.json").read_text())
    assert config["training_enabled"] is False
    assert config["auto_activate"] is False
    assert config["dataset"] == "runs/your_private_training_events.json"


def test_branding_does_not_change_existing_database_filename():
    import app_database

    assert app_database.DEFAULT_DB_PATH.name == "easymoneysniper.sqlite3"


def test_v_swing_is_opt_in_without_private_labels():
    config = json.loads((ROOT / "agent/config/v_swing.json").read_text())
    assert config["enabled"] is False
    assert not (ROOT / "agent/config/v_swing_annotations.json").exists()
    assert json.loads((ROOT / "agent/config/v_swing_annotations.example.json").read_text()) == {}


def test_v_swing_disabled_board_does_not_read_or_train():
    from unittest.mock import patch
    import v_swing_service as swing

    board = {"picks": [{"symbol": "AAPL", "calibrated_probability": 0.5}]}
    with patch.object(swing, "cache_get", side_effect=AssertionError("disabled cache read")):
        assert swing.attach_board(board) is board


def test_readme_screenshot_links_exist():
    for name in ("13-v-swing-chart.png", "14-company-network.png"):
        assert (ROOT / "assets/screenshots/desktop" / name).is_file()


def test_public_readmes_use_the_requested_title():
    title = "# 金融机构开发者 × 聚源数据地图 MCP：开源 AI 量化与投研系统。"
    for name in ("README.md", "README_zh.md"):
        assert (ROOT / name).read_text(encoding="utf-8").splitlines()[0] == title
