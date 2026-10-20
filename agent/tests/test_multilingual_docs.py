"""Keep the public core guides and shipped brand consistent, without network I/O."""
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]
GUIDES = {
    "README.md": ("US equities", "US-listed equity options", "still being adapted"),
    "README_zh.md": ("美股和美股期权", "A股、港股仍在适配中"),
    "README_ja.md": ("米国株", "株式オプション", "対応作業中"),
    "README_ko.md": ("미국 주식", "주식 옵션", "연동 작업 중"),
    "README_ar.md": ("الأسهم الأمريكية", "خيارات الأسهم", "قيد التكييف"),
}


@pytest.mark.parametrize("filename,terms", GUIDES.items())
def test_localized_core_guide(filename, terms):
    text = (ROOT / filename).read_text(encoding="utf-8")
    assert text.startswith("# StockSignalForge\n")
    assert "**easymoneysniper**" in text[:150]
    assert all(term in text[:2000] for term in terms)
    for other in GUIDES:
        if other != filename:
            assert f"]({other})" in text[:500]
    assert text.count("```") % 2 == 0
    for required in (
        "docker compose up -d --build vibe-trading",
        "agent/.env.example", "http://127.0.0.1:19090/health",
        "HKUDS/Vibe-Trading", "MIT", "GILDATA_MCP_URL",
    ):
        assert required in text
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if "://" not in target and not target.startswith("#"):
            assert (ROOT / target.split("#", 1)[0]).is_file(), target


def test_arabic_direction_wrappers_are_balanced():
    text = (ROOT / "README_ar.md").read_text(encoding="utf-8")
    assert '<div dir="rtl">' in text
    assert '<div dir="ltr">' in text
    assert text.count("<div ") == text.count("</div>")


def test_shipped_html_and_sidebar_brand():
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    layout = (ROOT / "frontend/src/components/layout/Layout.tsx").read_text(encoding="utf-8")
    assert "<title>StockSignalForge · easymoneysniper</title>" in html
    assert "美股和美股期权" in html
    assert "A股、港股仍在适配中" in html
    assert "StockSignalForge" in layout
    assert "easymoneysniper" in layout


def test_favicon_matches_sidebar_and_invalidates_old_icon_cache():
    from xml.etree import ElementTree

    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    icon = ElementTree.fromstring((ROOT / "frontend/public/favicon.svg").read_text(encoding="utf-8"))
    ns = {"svg": "http://www.w3.org/2000/svg"}
    assert icon.find("svg:title", ns).text == "StockSignalForge"
    assert icon.find("svg:text", ns).text == "35"
    assert 'href="/favicon.svg?v=stocksignalforge-1"' in html
