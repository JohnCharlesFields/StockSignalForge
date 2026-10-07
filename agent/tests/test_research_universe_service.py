from research_universe_service import RESEARCH_UNIVERSE_CATALOG, list_research_universe_groups
from scripts.screening_framework_v2_optimized import resolve_universe


def test_research_universe_catalog_contains_only_requested_index_pools() -> None:
    keys = [item["universe"] for item in RESEARCH_UNIVERSE_CATALOG]

    assert keys == ["spx", "djia", "rua", "w5000", "ixic", "ndx", "rut", "mid", "sml", "sox", "watchlist"]
    assert len(keys) == len(set(keys))
    assert all(item["purpose"] and item["run_cost"] for item in RESEARCH_UNIVERSE_CATALOG)


def test_research_universe_catalog_is_grouped_and_prioritized() -> None:
    groups = list_research_universe_groups()
    group_order = {item["group"]: item["priority"] for item in groups}

    assert RESEARCH_UNIVERSE_CATALOG == sorted(
        RESEARCH_UNIVERSE_CATALOG,
        key=lambda item: (group_order[item["group"]], item["priority"]),
    )
    assert [item["label"] for item in groups] == [
        "01 美股宽基指数池",
        "02 科技成长指数池",
        "03 市值风格指数池",
        "04 行业指数池",
        "05 自选池",
    ]


def test_old_theme_pools_are_not_resolvable_as_platform_universes() -> None:
    for old_key in ["us_volume_top50", "sp100", "komp", "soxx", "spmo", "aiq"]:
        try:
            resolve_universe(old_key)
        except ValueError as exc:
            assert "Supported index universes" in str(exc)
        else:
            raise AssertionError(f"{old_key} should not resolve after index-pool reset")
