from signal_dashboard_page import signal_dashboard_html
from src.swarm.presets import inspect_preset, list_presets


def test_five_layer_research_presets_are_valid() -> None:
    names = {item["name"] for item in list_presets()}

    assert "five_layer_research_chain" in names
    assert "quality_investment_panel" in names

    five_layer = inspect_preset("five_layer_research_chain")
    quality_panel = inspect_preset("quality_investment_panel")

    assert five_layer["valid"], five_layer["errors"]
    assert quality_panel["valid"], quality_panel["errors"]
    assert len(five_layer["layers"]) == 5
    assert len(quality_panel["layers"]) == 2


def test_signal_dashboard_exposes_five_layer_action() -> None:
    html = signal_dashboard_html()

    assert "五层研究链" in html
    assert "/research-chain/run" in html
    assert "launchChain" in html
