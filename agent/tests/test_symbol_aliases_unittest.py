from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

import app_database as db
from symbol_seed import load_symbol_seed


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite3")
    monkeypatch.setattr(db, "_INITIALIZED", False)
    db.ensure_database()
    db.symbol_directory_upsert_many([
        {"symbol": "AMZN", "name": "Amazon.com, Inc."},
        {"symbol": "MSFT", "name": "微软"},
        {"symbol": "GOOG", "name": "Alphabet Class C"},
        {"symbol": "GOOGL", "name": "Alphabet Class A"},
        {"symbol": "HPE", "name": "Hewlett Packard Enterprise"},
        {"symbol": "HPQ", "name": "HP Inc."},
        {"symbol": "NVDA", "name": "NVIDIA Corporation"},
    ])
    return db.DB_PATH


def alias(symbol, name, kind="official_cn", source="fixture", url="https://example.com/company"):
    return {"symbol": symbol, "alias": name, "alias_kind": kind, "source": source, "source_url": url}


def test_chinese_full_prefix_substring_and_code(catalog):
    db.symbol_directory_alias_upsert_many([alias("AMZN", "亚马逊")])
    for term in ("亚马逊", "亚马", "马逊", "amzn", "Amazon"):
        result = db.symbol_directory_search(term)
        assert result[0]["symbol"] == "AMZN"
        assert result[0]["name"] == "Amazon.com, Inc."
        assert result[0]["name_cn"] == "亚马逊"
    assert db.symbol_directory_search("亚马逊")[0]["matched_alias"] == "亚马逊"


def test_aliases_survive_display_name_replacement_and_restart(catalog):
    db.symbol_directory_alias_upsert_many([alias("AMZN", "亚马逊")])
    db.symbol_directory_upsert_many([{"symbol": "AMZN", "name": "Amazon Inc."}])
    db._INITIALIZED = False
    db.ensure_database()
    assert db.symbol_directory_search("亚马逊")[0]["name"] == "Amazon Inc."


def test_english_seed_name_survives_current_chinese_name(catalog):
    db.symbol_directory_refresh_aliases(load_symbol_seed(), [])
    assert db.symbol_directory_search("Microsoft")[0]["symbol"] == "MSFT"
    assert db.symbol_directory_search("微软")[0]["symbol"] == "MSFT"


def test_alias_dedup_multi_share_class_and_limit(catalog):
    db.symbol_directory_alias_upsert_many([
        alias("GOOG", "谷歌", "common_cn"), alias("GOOGL", "谷歌", "common_cn"),
        alias("GOOG", "谷歌", "vendor_cn", "other"),
    ])
    assert [r["symbol"] for r in db.symbol_directory_search("谷歌")] == ["GOOG", "GOOGL"]
    assert len(db.symbol_directory_search("谷歌", 1)) == 1
    assert len(db.symbol_directory_search("谷歌", 12)) == 2


def test_parent_child_brand_is_not_assigned_to_different_issuer(catalog):
    db.symbol_directory_alias_upsert_many([alias("HPE", "慧与"), alias("HPQ", "惠普")])
    assert [r["symbol"] for r in db.symbol_directory_search("惠普")] == ["HPQ"]
    assert [r["symbol"] for r in db.symbol_directory_search("慧与")] == ["HPE"]


def test_existing_cached_gildata_names_imported_without_external_call(catalog):
    db.cache_set("gildata:research:company:NVDA", {"symbol": "NVDA", "name": "NVIDIA Corp.",
                 "name_cn": "英伟达公司", "short_name": "英伟达", "source": "gildata:company"})
    db.symbol_directory_refresh_aliases([], [])
    item = db.symbol_directory_search("英伟达")[0]
    assert item["symbol"] == "NVDA"
    assert item["alias_kind"] == "vendor_cn"
    assert item["alias_source"] == "gildata:company"
    assert db.symbol_directory_search("NVIDIA")[0]["symbol"] == "NVDA"


def test_official_name_preferred_to_vendor_translation(catalog):
    db.symbol_directory_alias_upsert_many([alias("AMZN", "亚马逊电商公司", "vendor_cn", "vendor"),
                                         alias("AMZN", "亚马逊")])
    assert db.symbol_directory_search("AMZN")[0]["name_cn"] == "亚马逊"


def test_alias_insert_never_creates_unknown_security_and_rejects_invalid_source(catalog):
    db.symbol_directory_alias_upsert_many([alias("UNKNOWN", "不存在"), alias("AMZN", "暂无"),
        alias("AMZN", "错误官方名", url="https://example.com/?token=secret"),
        alias("AMZN", "错误", source="")])
    assert db.symbol_directory_search("不存在") == []
    assert db.symbol_directory_search("错误") == []
    assert db.symbol_directory_count() == 7


def test_alias_index_query_plan_and_literal_wildcards(catalog):
    db.symbol_directory_alias_upsert_many([alias("AMZN", "名称100%_测试", "common_cn")])
    assert db.symbol_directory_search("%_")[0]["symbol"] == "AMZN"
    with sqlite3.connect(catalog) as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT symbol FROM symbol_directory_aliases "
                            "WHERE alias_upper >= ? AND alias_upper < ?", ("亚", "亚" + chr(0x10FFFF))).fetchall()
    assert any("SEARCH" in str(r) and "idx_symbol_directory_alias" in str(r) for r in plan)


def test_read_aliases_never_initializes_or_writes(catalog):
    db.symbol_directory_alias_upsert_many([alias("AMZN", "亚马逊")])
    with patch.object(db, "ensure_database", side_effect=AssertionError("read must not write")):
        assert db.symbol_directory_search("亚马逊")[0]["symbol"] == "AMZN"


def test_legacy_catalog_without_alias_table_still_searches(catalog):
    with sqlite3.connect(catalog) as conn:
        conn.execute("DROP TABLE symbol_directory_aliases")
    assert db.symbol_directory_search("AMZN")[0]["symbol"] == "AMZN"


def test_official_alias_seed_has_sources_and_expected_names():
    from symbol_seed import load_symbol_alias_seed
    rows = load_symbol_alias_seed()
    lookup = {(r["symbol"], r["alias"]): r for r in rows}
    for key in [("AMZN", "亚马逊"), ("WMT", "沃尔玛"), ("NKE", "耐克"), ("MCD", "麦当劳"),
                ("QCOM", "高通"), ("ORCL", "甲骨文"), ("CSCO", "思科")]:
        assert lookup[key]["alias_kind"] == "official_cn"
        assert lookup[key]["source_url"].startswith("https://")
    assert lookup["GOOG", "谷歌"]["alias_kind"] == "common_cn"
    assert lookup["GOOGL", "谷歌"]["alias_kind"] == "common_cn"
