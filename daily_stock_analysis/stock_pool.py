"""
股票池归属查询模块（多层级评分版）

通过股票代码判断其所属的投资池/指数/ETF，推断基本面质量和流动性。
不是简单的"有/没有"，而是多层级、有梯度的评分依据。

评分体系：
- Tier 1 (90-100): 全球顶级蓝筹（S&P100、道琼斯30）
- Tier 2 (80-89):  大盘成长（纳斯达克100、恒生指数）
- Tier 3 (70-79):  宽基指数成分（Russell 1000、恒生科技）
- Tier 4 (60-69):  中小盘指数（Russell 2000、中证500）
- Tier 5 (50-59):  主题ETF成分（行业主题ETF有显著权重）
- Tier 6 (40-49):  其他ETF成分（权重较低但有机构覆盖）
- Tier 7 (30-39):  未收录（无公开指数/ETF归属信息）
"""

import logging

logger = logging.getLogger(__name__)

# ============================================================
# Tier 1: 全球顶级蓝筹（基本面最优质，流动性极佳）
# ============================================================
SP100 = {
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AMD", "AMGN", "AMZN", "AVGO",
    "AXP", "BA", "BAC", "BK", "BLK", "BMY", "BRK.B", "C", "CAT", "CHTR",
    "CL", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO", "CVS", "CVX",
    "DE", "DHR", "DIS", "DUK", "EMR", "EXC", "F", "FDX", "GD", "GE",
    "GILD", "GM", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTC",
    "JNJ", "JPM", "KO", "LIN", "LLY", "LMT", "LOW", "MA", "MCD", "MDLZ",
    "MDT", "META", "MMM", "MO", "MRK", "MS", "MSFT", "NEE", "NFLX", "NKE",
    "NVDA", "ORCL", "PEP", "PFE", "PG", "PM", "PYPL", "QCOM", "RTX",
    "SBUX", "SCHW", "SO", "SPG", "T", "TGT", "TMO", "TMUS", "TSLA",
    "TXN", "UNH", "UNP", "UPS", "USB", "V", "VZ", "WFC", "WMT", "XOM"
}

DOW30 = {
    "AAPL", "AMGN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "DOW", "GS", "HD", "HON", "IBM", "INTC", "JNJ", "JPM", "KO", "MCD",
    "MMM", "MRK", "MSFT", "NKE", "PG", "TRV", "UNH", "V", "VZ", "WBA", "WMT"
}

# ============================================================
# Tier 2: 大盘成长（科技龙头，流动性极佳）
# ============================================================
NASDAQ100 = {
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD",
    "AMGN", "AMZN", "ANSS", "ARM", "ASML", "AVGO", "AZN", "BIIB", "BKNG",
    "BKR", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COIN", "COST", "CPRT",
    "CRWD", "CSCO", "CSGP", "CTAS", "CTSH", "DASH", "DDOG", "DLTR", "DXCM",
    "EA", "EXC", "FANG", "FAST", "FTNT", "GEHC", "GFS", "GILD", "GOOG",
    "GOOGL", "HON", "IDXX", "ILMN", "INTC", "INTU", "ISRG", "KDP", "KHC",
    "KLAC", "LRCX", "LULU", "MAR", "MCHP", "MDB", "META", "MELI", "MNST",
    "MRNA", "MRVL", "MSFT", "MU", "NFLX", "NVDA", "NXPI", "ODFL", "ON",
    "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PYPL", "QCOM", "REGN",
    "ROST", "SBUX", "SNPS", "TEAM", "TMUS", "TSLA", "TTD", "TTWO", "TXN",
    "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS"
}

HSI = {
    "0001.HK", "0002.HK", "0003.HK", "0005.HK", "0006.HK", "0011.HK",
    "0012.HK", "0016.HK", "0017.HK", "0027.HK", "0066.HK", "0101.HK",
    "0175.HK", "0241.HK", "0267.HK", "0288.HK", "0291.HK", "0316.HK",
    "0322.HK", "0386.HK", "0388.HK", "0669.HK", "0688.HK", "0700.HK",
    "0762.HK", "0823.HK", "0857.HK", "0868.HK", "0881.HK", "0883.HK",
    "0939.HK", "0941.HK", "0960.HK", "0968.HK", "0981.HK", "1038.HK",
    "1044.HK", "1093.HK", "1109.HK", "1177.HK", "1211.HK", "1299.HK",
    "1378.HK", "1398.HK", "1810.HK", "1876.HK", "1928.HK", "1929.HK",
    "1997.HK", "2007.HK", "2018.HK", "2020.HK", "2269.HK", "2313.HK",
    "2318.HK", "2319.HK", "2331.HK", "2382.HK", "2388.HK", "2628.HK",
    "2688.HK", "3328.HK", "3690.HK", "3968.HK", "3988.HK", "6098.HK",
    "6618.HK", "6862.HK", "9618.HK", "9633.HK", "9698.HK", "9888.HK",
    "9961.HK", "9988.HK", "9999.HK"
}

# ============================================================
# Tier 3: 宽基指数成分（覆盖面广，基本面有保障）
# ============================================================
RUSSELL1000 = {
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK.B",
    "UNH", "JNJ", "V", "XOM", "JPM", "PG", "MA", "HD", "CVX", "MRK",
    "LLY", "ABBV", "PEP", "KO", "COST", "AVGO", "WMT", "MCD", "CSCO",
    "ACN", "TMO", "ABT", "DHR", "NEE", "LIN", "PM", "TXN", "UNP",
    "RTX", "LOW", "ORCL", "HON", "AMGN", "COP", "BMY", "UPS", "QCOM",
    "BA", "CAT", "GS", "BLK", "AXP", "ISRG", "SPGI", "AMD", "INTC",
    "DE", "SYK", "ADBE", "MDLZ", "GILD", "CI", "CB", "MMC", "PLD",
    "SCHW", "SO", "DUK", "BDX", "ZTS", "CL", "EOG", "SLB", "CME",
    "USB", "PNC", "TFC", "FDX", "GM", "F", "GD", "LMT", "NOC", "HUM",
    "EL", "TGT", "ROST", "ORLY", "MCK", "CMG", "LULU", "FTNT", "PANW",
    "CRWD", "DDOG", "SNOW", "ZS", "NET", "MDB", "TEAM", "WDAY",
}

# 恒生科技指数
HSTECH = {
    "0268.HK", "0285.HK", "0522.HK", "0698.HK", "0772.HK", "0780.HK",
    "0909.HK", "0981.HK", "1024.HK", "1347.HK", "1478.HK", "1810.HK",
    "1833.HK", "2013.HK", "2015.HK", "2018.HK", "2382.HK", "2518.HK",
    "3690.HK", "3888.HK", "6060.HK", "6618.HK", "6690.HK", "9618.HK",
    "9626.HK", "9698.HK", "9888.HK", "9961.HK", "9988.HK", "9999.HK"
}

# ============================================================
# Tier 4: 中小盘指数（Russell 2000，有机构覆盖）
# ============================================================
RUSSELL2000_SAMPLE = {
    "BKSY", "IREN", "AMCS", "SOFI", "PLTR", "HOOD", "RIVN", "LCID",
    "SMCI", "MARA", "RIOT", "CLSK", "BITF", "HIVE", "BTBT", "IONQ",
    "RGTI", "QUBT", "ARQQ", "RDW", "LUNR", "RKLB", "ASTR", "ASTS",
    "MNTS", "SPIR", "BKSY", "SATL", "VORB", "BLDE", "JOBY", "ACHR",
    "EVTL", "LILM", "EH", "EVLV", "OPEN", "WISH", "BARK", "HIMS",
    "CLOV", "AFRM", "UPST", "LMND", "ROOT", "VNET", "TIO", "BBAI",
    "BIGC", "PAYO", "RELY", "DAVE", "NU", "MELI", "SE", "GRAB",
    "CPNG", "SEZL", "GLBE", "TOST", "BILL", "BRZE", "S", "CFLT",
    "DOCN", "NET", "ESTC", "SUMO", "VERX", "FRSH", "GTLB", "KVYO",
}

# ============================================================
# Tier 5-6: 主题ETF成分股及权重
# ============================================================
# 格式: {stock_code: [(etf_name, weight_pct, theme_desc), ...]}
ETF_HOLDINGS = {
    # 航天与国防
    "BKSY": [
        ("NASA", 3.85, "航天主题ETF，权重较高"),
        ("KOMP", 0.63, "新经济与颠覆式创新ETF"),
        ("SHLD", 0.26, "国防科技主题ETF"),
        ("IWM", 0.04, "Russell 2000宽基ETF"),
    ],
    "RKLB": [
        ("NASA", 5.2, "航天主题ETF"),
        ("ARKX", 3.1, "ARK太空探索ETF"),
        ("IWM", 0.03, "Russell 2000宽基ETF"),
    ],
    "LUNR": [
        ("NASA", 2.8, "航天主题ETF"),
        ("ARKX", 1.5, "ARK太空探索ETF"),
    ],
    "ASTS": [
        ("NASA", 2.1, "航天主题ETF"),
        ("ARKX", 1.8, "ARK太空探索ETF"),
    ],
    "IONQ": [
        ("QTUM", 4.5, "量子计算主题ETF"),
        ("KOMP", 0.8, "新经济与颠覆式创新ETF"),
    ],
    "PLTR": [
        ("KOMP", 2.1, "新经济与颠覆式创新ETF"),
        ("CIBR", 1.5, "网络安全ETF"),
        ("IWM", 0.15, "Russell 2000宽基ETF"),
    ],
    "SOFI": [
        ("KOMP", 0.9, "新经济与颠覆式创新ETF"),
        ("FINX", 2.3, "金融科技ETF"),
        ("IWM", 0.08, "Russell 2000宽基ETF"),
    ],
    "HOOD": [
        ("KOMP", 0.7, "新经济与颠覆式创新ETF"),
        ("FINX", 1.8, "金融科技ETF"),
    ],
    "MARA": [
        ("BITS", 8.5, "比特币矿业ETF"),
        ("DAPP", 6.2, "数字资产ETF"),
        ("IWM", 0.05, "Russell 2000宽基ETF"),
    ],
    "RIOT": [
        ("BITS", 7.2, "比特币矿业ETF"),
        ("DAPP", 5.8, "数字资产ETF"),
    ],
    "SMCI": [
        ("KOMP", 1.5, "新经济与颠覆式创新ETF"),
        ("SMH", 1.2, "半导体ETF"),
        ("IWM", 0.12, "Russell 2000宽基ETF"),
    ],
    # 科技巨头（也有ETF权重信息）
    "NVDA": [
        ("SMH", 20.5, "半导体ETF，最大权重"),
        ("QQQ", 8.2, "纳斯达克100ETF"),
        ("XLK", 15.3, "科技板块ETF"),
        ("SOXX", 18.1, "半导体ETF"),
    ],
    "AAPL": [
        ("QQQ", 10.5, "纳斯达克100ETF"),
        ("XLK", 22.1, "科技板块ETF"),
        ("SPY", 6.8, "S&P500 ETF"),
    ],
    "TSLA": [
        ("QQQ", 3.8, "纳斯达克100ETF"),
        ("XLY", 15.2, "可选消费ETF"),
        ("ARKK", 10.5, "ARK创新ETF"),
    ],
}


def get_stock_pool(stock_code: str) -> dict:
    """
    查询股票所属的投资池（多层级评分）

    Returns:
        dict: {
            "pools": [{"name": "...", "tier": 1, "desc": "..."}, ...],
            "score": 30-100,
            "quality_tier": "blue_chip" | "growth" | "mid_cap" | "small_cap" | "themed" | "unknown",
            "liquidity": "excellent" | "good" | "moderate" | "low" | "unknown",
            "fundamental_assumption": "...",
            "etf_info": "ETF权重详情",
            "is_index_constituent": True/False,
            "sector_theme": "行业/主题标签"
        }
    """
    code = stock_code.upper().strip()
    pools = []
    etf_info = ""

    # === Tier 1: 全球顶级蓝筹 ===
    if code in SP100:
        pools.append({"name": "S&P100", "tier": 1, "desc": "全球顶级大市值蓝筹"})
    if code in DOW30:
        pools.append({"name": "道琼斯30", "tier": 1, "desc": "美国工业蓝筹指数"})

    # === Tier 2: 大盘成长 ===
    if code in NASDAQ100:
        pools.append({"name": "纳斯达克100", "tier": 2, "desc": "科技成长龙头"})
    if code in HSI:
        pools.append({"name": "恒生指数", "tier": 2, "desc": "港股蓝筹"})

    # === Tier 3: 宽基指数 ===
    if code in RUSSELL1000:
        pools.append({"name": "Russell 1000", "tier": 3, "desc": "美国大盘宽基指数"})
    if code in HSTECH:
        pools.append({"name": "恒生科技指数", "tier": 3, "desc": "港股科技成长"})

    # === Tier 4: 中小盘指数 ===
    if code in RUSSELL2000_SAMPLE:
        pools.append({"name": "Russell 2000", "tier": 4, "desc": "美国小盘股指数，有机构覆盖"})

    # === Tier 5-6: ETF权重 ===
    if code in ETF_HOLDINGS:
        holdings = ETF_HOLDINGS[code]
        max_weight = max(h[1] for h in holdings)
        for etf, weight, desc in holdings:
            tier = 5 if weight >= 2.0 else 6
            pools.append({"name": etf, "tier": tier, "desc": f"{desc}（权重{weight}%）"})
        etf_details = [f"{etf}({weight}%)" for etf, weight, _ in holdings]
        etf_info = "、".join(etf_details)

    # === 计算综合评分 ===
    if not pools:
        return {
            "pools": [],
            "score": 30,
            "score_basis": "pool_membership_coverage_not_fundamental_quality",
            "quality_tier": "unknown",
            "liquidity": "unknown",
            "fundamental_assumption": "未收录在主要指数或ETF中，基本面和流动性需自行验证",
            "etf_info": "",
            "is_index_constituent": False,
            "sector_theme": ""
        }

    # 评分逻辑：激进型投资者，中小盘爆发力有加分
    tier_scores = {1: 95, 2: 85, 3: 75, 4: 68, 5: 62, 6: 50}
    best_tier = min(p["tier"] for p in pools)
    base_score = tier_scores.get(best_tier, 30)

    # 多个pool有加分（最多+8分）
    bonus = min(len(pools) * 1.5, 8)
    # 主题ETF高权重额外加分（爆发力指标）
    if code in ETF_HOLDINGS:
        max_weight = max(h[1] for h in ETF_HOLDINGS[code])
        if max_weight >= 3.0:
            bonus += 5  # 高权重主题ETF，爆发力强
        elif max_weight >= 1.0:
            bonus += 3  # 中等权重
    score = min(base_score + bonus, 100)

    # 质量层级
    tier_to_quality = {1: "blue_chip", 2: "growth", 3: "mid_cap", 4: "small_cap", 5: "themed", 6: "themed"}
    quality_tier = tier_to_quality.get(best_tier, "unknown")

    # 指数或 ETF 归属只能说明覆盖范围，不能替代实时成交量、换手率和盘口校验。
    liquidity = "requires_market_check"

    # 归属证据提示：不把指数纳入或 ETF 持仓误写成基本面结论。
    if best_tier <= 2:
        fa = "主要指数覆盖度较高；这不等同于基本面优秀。仍需复核财报、估值、最新成交量和盘口。"
    elif best_tier == 3:
        fa = "宽基指数成分股；可作为机构覆盖证据，但不能替代基本面和实时流动性检查。"
    elif best_tier == 4:
        fa = "中小盘指数成分股；需重点检查成长质量、波动率、成交量和退出难度。"
    elif best_tier == 5:
        max_h = max(ETF_HOLDINGS.get(code, [(None, 0, "")]), key=lambda x: x[1])
        fa = f"主题ETF成分股（{max_h[0]}权重{max_h[1]}%），有主题投资机构关注，基本面需结合行业逻辑判断"
    else:
        fa = "有ETF持仓但权重较低，机构关注度有限，基本面需自行验证"

    # 行业主题
    sector_themes = {
        "NASA": "航天航空", "ARKX": "太空探索", "SHLD": "国防科技",
        "KOMP": "颠覆式创新", "SMH": "半导体", "SOXX": "半导体",
        "QQQ": "科技", "XLK": "科技", "FINX": "金融科技",
        "CIBR": "网络安全", "BITS": "比特币矿业", "DAPP": "数字资产",
        "QTUM": "量子计算", "ARKK": "颠覆式创新", "IWM": "小盘宽基",
    }
    themes = set()
    for p in pools:
        theme = sector_themes.get(p["name"])
        if theme:
            themes.add(theme)
    sector_theme = "、".join(themes) if themes else ""

    return {
        "pools": pools,
        "score": score,
        "score_basis": "pool_membership_coverage_not_fundamental_quality",
        "quality_tier": quality_tier,
        "liquidity": liquidity,
        "fundamental_assumption": fa,
        "etf_info": etf_info,
        "is_index_constituent": best_tier <= 4,
        "sector_theme": sector_theme
    }


def get_pool_context(stock_code: str) -> str:
    """获取股票池上下文信息，用于注入LLM分析prompt"""
    info = get_stock_pool(stock_code)

    if not info["pools"]:
        return ""

    # 按tier分组显示
    tier_labels = {1: "顶级蓝筹", 2: "大盘成长", 3: "宽基指数", 4: "中小盘指数", 5: "主题ETF(高权重)", 6: "ETF持仓"}
    pool_lines = []
    for p in sorted(info["pools"], key=lambda x: x["tier"]):
        label = tier_labels.get(p["tier"], "其他")
        pool_lines.append(f"  - {p['name']}（{label}）：{p['desc']}")

    lines = [
        f"【股票池覆盖评分】{info['score']}/100（仅反映指数/ETF归属，不等同于基本面或买入评分）",
        f"【质量层级】{info['quality_tier']}",
        f"【流动性】{info['liquidity']}",
    ]
    if info["sector_theme"]:
        lines.append(f"【行业主题】{info['sector_theme']}")
    if info["etf_info"]:
        lines.append(f"【ETF持仓】{info['etf_info']}")
    lines.append(f"【基本面推断】{info['fundamental_assumption']}")
    lines.append("【指数/ETF归属】")
    lines.extend(pool_lines)

    if info["score"] >= 70:
        lines.append("【提示】指数/ETF背书明确，基本面有保障。激进型投资者可重点关注趋势动能和题材催化，资金流缺失不应成为降级理由")
    elif info["score"] >= 55:
        lines.append("【提示】主题ETF覆盖+机构关注，爆发力评估应结合题材催化强度、资金关注度、趋势动能，而非单纯估值安全边际")

    return "\n".join(lines)


def get_pool_summary(stock_code: str) -> str:
    """获取简短的股票池摘要（用于飞书推送等场景）"""
    info = get_stock_pool(stock_code)

    if not info["pools"]:
        return ""

    best_pool = min(info["pools"], key=lambda x: x["tier"])
    score = info["score"]
    theme = info.get("sector_theme", "")

    parts = [best_pool["name"]]
    if theme:
        parts.append(theme)
    parts.append(f"{score}分")

    return " · ".join(parts)


def get_pool_score(stock_code: str) -> int:
    """获取股票池评分（供后处理逻辑使用）"""
    info = get_stock_pool(stock_code)
    return info["score"]



# ============================================================
# 概念股关联映射（提供关联线索，不写死加分）
# LLM会结合最新资讯动态判断催化强度
# ============================================================
CONCEPT_ASSOCIATIONS = {
    # SpaceX生态（SpaceX上市/发射/星链等事件催化）
    "BKSY": {"concept": "SpaceX/航天", "relation": "航天卫星图像服务商，SpaceX发射合作伙伴", "elasticity": "极高"},
    "RKLB": {"concept": "SpaceX/航天", "relation": "小型火箭发射商，SpaceX竞争对手/互补", "elasticity": "极高"},
    "LUNR": {"concept": "SpaceX/航天", "relation": "月球着陆器开发商，NASA合同受益方", "elasticity": "极高"},
    "ASTS": {"concept": "SpaceX/航天", "relation": "太空蜂窝网络，SpaceX星链竞争对手", "elasticity": "极高"},
    "ASTR": {"concept": "SpaceX/航天", "relation": "小型火箭发射商", "elasticity": "高"},
    "RDW": {"concept": "SpaceX/航天", "relation": "太空基础设施服务商", "elasticity": "高"},

    # NVDA产业链（NVDA财报/AI算力需求催化）
    "SMCI": {"concept": "NVDA/AI算力", "relation": "AI服务器核心供应商，NVDA GPU直接受益方", "elasticity": "极高"},
    "DELL": {"concept": "NVDA/AI服务器", "relation": "AI服务器/基础设施龙头，NVDA GPU服务器核心OEM，企业级AI部署受益方", "elasticity": "高"},
    "HPE": {"concept": "NVDA/AI服务器", "relation": "AI服务器供应商，企业级AI基础设施", "elasticity": "高"},
    "DELL": {"concept": "NVDA/AI服务器", "relation": "AI服务器/基础设施龙头，NVDA GPU服务器核心OEM，企业级AI部署受益方", "elasticity": "高"},
    "HPE": {"concept": "NVDA/AI服务器", "relation": "AI服务器供应商，企业级AI基础设施", "elasticity": "高"},
    "AVGO": {"concept": "NVDA/AI算力", "relation": "AI芯片互联核心供应商", "elasticity": "高"},
    "AMD": {"concept": "NVDA/AI算力", "relation": "GPU竞争对手，AI算力替代方案", "elasticity": "高"},
    "MU": {"concept": "NVDA/AI算力", "relation": "HBM内存核心供应商，AI服务器必需", "elasticity": "高"},
    "MRVL": {"concept": "NVDA/AI算力", "relation": "AI芯片定制化供应商", "elasticity": "高"},
    "ARM": {"concept": "NVDA/AI算力", "relation": "AI芯片架构授权方", "elasticity": "高"},
    "TSM": {"concept": "NVDA/AI算力", "relation": "NVDA芯片代工方", "elasticity": "中"},
    "ASML": {"concept": "NVDA/AI算力", "relation": "光刻机核心供应商，芯片制造上游", "elasticity": "中"},

    # 加密货币板块（BTC/ETH价格突破催化）
    "MARA": {"concept": "加密货币/BTC", "relation": "比特币矿业龙头，BTC价格直接受益方", "elasticity": "极高"},
    "RIOT": {"concept": "加密货币/BTC", "relation": "比特币矿业，BTC价格直接受益方", "elasticity": "极高"},
    "COIN": {"concept": "加密货币/BTC", "relation": "加密货币交易所，交易量受益方", "elasticity": "高"},
    "CLSK": {"concept": "加密货币/BTC", "relation": "比特币矿业", "elasticity": "高"},
    "BITF": {"concept": "加密货币/BTC", "relation": "比特币矿业", "elasticity": "高"},
    "MSTR": {"concept": "加密货币/BTC", "relation": "大量持有BTC的上市公司", "elasticity": "极高"},

    # 量子计算板块（量子计算突破催化）
    "IONQ": {"concept": "量子计算", "relation": "量子计算纯正标的，商业化进展直接受益", "elasticity": "极高"},
    "RGTI": {"concept": "量子计算", "relation": "量子计算芯片开发商", "elasticity": "极高"},
    "QUBT": {"concept": "量子计算", "relation": "量子计算软件", "elasticity": "高"},
    "ARQQ": {"concept": "量子计算", "relation": "量子安全加密", "elasticity": "高"},

    # AI应用板块（AI应用落地催化）
    "PLTR": {"concept": "AI应用/国防", "relation": "AI数据分析平台，政府/军工AI应用龙头", "elasticity": "高"},
    "SOFI": {"concept": "AI金融科技", "relation": "AI驱动的金融科技平台", "elasticity": "高"},
    "UPST": {"concept": "AI金融科技", "relation": "AI信贷评估", "elasticity": "极高"},
    "AFRM": {"concept": "AI金融科技", "relation": "AI驱动的消费信贷", "elasticity": "高"},
    "HOOD": {"concept": "金融科技/散户", "relation": "散户交易平台，加密货币交易受益", "elasticity": "高"},
    "JOBY": {"concept": "eVTOL/飞行汽车", "relation": "电动垂直起降飞行器龙头", "elasticity": "极高"},
    "ACHR": {"concept": "eVTOL/飞行汽车", "relation": "电动飞行器制造商", "elasticity": "极高"},

    # Mag7 - 七巨头（AI/科技革命核心标的）
    "NVDA": {"concept": "Mag7/AI算力", "relation": "AI算力真神，GPU垄断者，AI革命核心受益方", "elasticity": "中（大盘蓝筹，绝对龙头）"},
    "AAPL": {"concept": "Mag7/AI终端", "relation": "AI终端入口，Apple Intelligence生态", "elasticity": "中（大盘蓝筹）"},
    "MSFT": {"concept": "Mag7/AI云", "relation": "AI云服务龙头，OpenAI最大投资方", "elasticity": "中（大盘蓝筹）"},
    "GOOGL": {"concept": "Mag7/AI搜索", "relation": "AI搜索+云计算+Gemini大模型", "elasticity": "中（大盘蓝筹）"},
    "AMZN": {"concept": "Mag7/AI云", "relation": "AWS云服务龙头，AI基础设施提供方", "elasticity": "中（大盘蓝筹）"},
    "META": {"concept": "Mag7/AI社交", "relation": "AI社交+广告+Llama大模型开源", "elasticity": "中（大盘蓝筹）"},
    "TSLA": {"concept": "Mag7/AI自动驾驶", "relation": "AI自动驾驶+机器人+能源，弹性最高Mag7", "elasticity": "高（Mag7中弹性最大）"},
}


def get_concept_context(stock_code: str) -> str:
    """获取概念股关联上下文（供LLM参考）"""
    code = stock_code.upper().strip()
    if code in CONCEPT_ASSOCIATIONS:
        assoc = CONCEPT_ASSOCIATIONS[code]
        return (
            "【概念股关联】该股票属于 **" + assoc['concept'] + "** 概念板块\n"
            "【关联逻辑】" + assoc['relation'] + "\n"
            "【弹性等级】" + assoc['elasticity'] + "\n"
            "【重要提示】请结合最新资讯判断该概念板块的催化强度，动态评估爆发力"
        )

    # 动态识别：未收录的股票，让LLM从资讯中自行判断
    return (
        "【概念股关联】该股票未在静态概念池中，请根据以下信息动态判断：\n"
        "1. 从上方的【消息面/最新资讯】中提取该股票的行业、板块、概念关联\n"
        "2. 判断该股票是否属于当前市场热点概念的受益标的（如AI、航天、加密货币、新能源等）\n"
        "3. 评估该股票的弹性等级（极高/高/中/低）\n"
        "4. 如果资讯中没有明确的概念关联信息，请根据公司主营业务推断其最可能受益的概念板块\n"
        "5. 重点关注：是否有重大催化事件（如IPO、财报、政策、行业突破）即将发生"
    )


def get_concept_context(stock_code: str) -> str:
    """获取概念股关联上下文（供LLM参考）"""
    code = stock_code.upper().strip()
    if code in CONCEPT_ASSOCIATIONS:
        assoc = CONCEPT_ASSOCIATIONS[code]
        return f"""【概念股关联】该股票属于 **{assoc['concept']}** 概念板块
【关联逻辑】{assoc['relation']}
【弹性等级】{assoc['elasticity']}
【重要提示】请结合最新资讯判断该概念板块的催化强度，动态评估爆发力"""

    # 动态识别：未收录的股票，让LLM从资讯中自行判断
    return f"""【概念股关联】该股票未在静态概念池中，请根据以下信息动态判断：
1. 从上方的【消息面/最新资讯】中提取该股票的行业、板块、概念关联
2. 判断该股票是否属于当前市场热点概念的受益标的（如AI、航天、加密货币、新能源等）
3. 评估该股票的弹性等级（极高/高/中/低）
4. 如果资讯中没有明确的概念关联信息，请根据公司主营业务推断其最可能受益的概念板块
5. 重点关注：是否有重大催化事件（如IPO、财报、政策、行业突破）即将发生"""


# ============================================================
# 供应链图谱：股票 → Mag7 的供应链路径
# 格式: [(路径描述, 关系说明, 层级), ...]
# 路径用 → 连接，如 "DELL → NVDA"
# ============================================================
SUPPLY_CHAIN_TO_MAG7 = {
    # === NVDA 供应链 ===
    "NVDA": [("NVDA", "Mag7核心：AI算力垄断者", 0)],
    "TSM": [("TSM → NVDA", "台积电为NVDA代工芯片（一级供应商）", 1)],
    "ASML": [("ASML → TSM → NVDA", "ASML光刻机→台积电代工→NVDA芯片（二级供应商）", 2)],
    "AVGO": [("AVGO → NVDA", "博通为NVDA提供NVLink互联芯片（一级供应商）", 1)],
    "MU": [("MU → NVDA", "美光HBM内存→NVDA GPU（一级供应商）", 1)],
    "SK HYNIX": [("SK HYNIX → NVDA", "海力士HBM内存→NVDA GPU（一级供应商）", 1)],
    "SMCI": [("SMCI → NVDA", "超微组装AI服务器，搭载NVDA GPU（一级客户/集成商）", 1)],
    "DELL": [("DELL → NVDA", "戴尔AI服务器搭载NVDA GPU，企业级AI部署（一级客户/集成商）", 1)],
    "HPE": [("HPE → NVDA", "HPE AI服务器搭载NVDA GPU（一级客户/集成商）", 1)],
    "AMD": [("AMD ↔ NVDA", "GPU竞争对手，同为AI算力供应商（竞合关系）", 1)],
    "ARM": [("ARM → NVDA", "ARM架构授权→NVDA芯片设计（一级IP供应商）", 1)],
    "MRVL": [("MRVL → NVDA", "Marvell定制芯片/AI互联→NVDA生态（一级供应商）", 1)],
    "INTC": [("INTC ↔ NVDA", "Intel GPU/AI加速器→NVDA竞品（竞合关系）", 1)],

    # === AAPL 供应链 ===
    "AAPL": [("AAPL", "Mag7核心：AI终端入口", 0)],
    "QCOM": [("QCOM → AAPL", "高通基带芯片→iPhone（一级供应商）", 1)],
    "GOOG": [("GOOG → AAPL", "Google搜索分成→Apple生态（一级合作伙伴）", 1)],
    "TSM": [("TSM → AAPL", "台积电代工Apple A/M系列芯片（一级供应商）", 1)],

    # === MSFT 供应链 ===
    "MSFT": [("MSFT", "Mag7核心：AI云+OpenAI", 0)],
    "NVDA": [("NVDA → MSFT", "NVDA GPU→Azure AI云（一级供应商）", 1)],

    # === GOOGL 供应链 ===
    "GOOGL": [("GOOGL", "Mag7核心：AI搜索+Gemini", 0)],

    # === AMZN 供应链 ===
    "AMZN": [("AMZN", "Mag7核心：AWS AI云", 0)],
    "NVDA": [("NVDA → AMZN", "NVDA GPU→AWS AI云（一级供应商）", 1)],

    # === META 供应链 ===
    "META": [("META", "Mag7核心：AI社交+Llama", 0)],
    "NVDA": [("NVDA → META", "NVDA GPU→Meta AI训练集群（一级供应商）", 1)],

    # === TSLA 供应链 ===
    "TSLA": [("TSLA", "Mag7核心：AI自动驾驶+机器人", 0)],
    "NVDA": [("NVDA → TSLA", "NVDA GPU→Tesla自动驾驶训练（一级供应商）", 1)],
    "AMD": [("AMD → TSLA", "AMD芯片→Tesla车载娱乐系统（一级供应商）", 1)],

    # === 航天供应链 ===
    "LUNR": [("LUNR → NASA", "月球着陆器→NASA合同（一级供应商）", 1)],
    "RKLB": [("RKLB → SpaceX", "小型火箭发射→SpaceX竞合（一级竞合）", 1)],
    "BKSY": [("BKSY → NASA", "卫星图像→NASA合作（一级供应商）", 1)],
    "ASTS": [("ASTS ↔ SpaceX", "太空蜂窝网络→SpaceX星链竞品（一级竞合）", 1)],
    "IONQ": [("IONQ", "量子计算纯正标的，无直接Mag7供应链", 0)],
}


# Dict literals silently discard earlier entries when a symbol is repeated.
# Keep the original catalog readable, then restore the intentional many-to-many
# relationships before exposing it to the analyzer.
_SUPPLY_CHAIN_ADDITIONS = {
    "NVDA": [
        ("NVDA", "Mag7核心：AI算力垄断者", 0),
        ("NVDA → MSFT", "NVDA GPU→Azure AI云（一级供应商）", 1),
        ("NVDA → AMZN", "NVDA GPU→AWS AI云（一级供应商）", 1),
        ("NVDA → META", "NVDA GPU→Meta AI训练集群（一级供应商）", 1),
    ],
    "TSM": [("TSM → NVDA", "台积电为NVDA代工芯片（一级供应商）", 1)],
    "AMD": [("AMD → NVDA", "GPU竞争对手，同为AI算力供应商（竞合关系）", 1)],
}
for _symbol, _paths in _SUPPLY_CHAIN_ADDITIONS.items():
    _existing = SUPPLY_CHAIN_TO_MAG7.setdefault(_symbol, [])
    for _path in _paths:
        if _path not in _existing:
            _existing.append(_path)


def get_supply_chain(stock_code: str) -> list:
    """获取股票到Mag7的供应链路径"""
    code = stock_code.upper().strip()
    return SUPPLY_CHAIN_TO_MAG7.get(code, [])


def get_supply_chain_summary(stock_code: str) -> str:
    """获取供应链简短摘要（用于飞书通知）"""
    chains = get_supply_chain(stock_code)
    if not chains:
        return ""
    parts = []
    for path, desc, level in chains:
        parts.append(f"{path}（{desc}）")
    return " | ".join(parts)


def get_supply_chain_context(stock_code: str) -> str:
    """获取供应链上下文（注入LLM prompt）"""
    chains = get_supply_chain(stock_code)
    if chains:
        lines = []
        for path, desc, level in chains:
            level_label = {0: "Mag7核心", 1: "一级", 2: "二级", 3: "三级"}.get(level, f"{level}级")
            lines.append(f"  {path} —— {desc}（{level_label}）")
        return "【供应链图谱】\n" + "\n".join(lines)
    return ""


if __name__ == "__main__":
    test_stocks = ["NVDA", "AAPL", "TSLA", "BKSY", "IREN", "AMCS", "PLTR", "SOFI", "MARA", "IONQ", "0700.HK", "9988.HK", "XYZ"]
    print("=" * 80)
    print("股票池多层级评分测试")
    print("=" * 80)
    for stock in test_stocks:
        info = get_stock_pool(stock)
        summary = get_pool_summary(stock)
        pools_str = ", ".join([p["name"] for p in info["pools"]]) if info["pools"] else "未收录"
        print(f"{stock:10} | 评分:{info['score']:3d} | {info['quality_tier']:10} | {info['liquidity']:10} | {pools_str}")
    print("=" * 80)
    print()
    # 详细测试BKSY
    print("BKSY 详细信息:")
    ctx = get_pool_context("BKSY")
    print(ctx)
