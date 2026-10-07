#!/usr/bin/env python3
"""启动后自动应用所有补丁"""

import os
import sys

def patch_file(filepath, old, new, desc):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        if old in content:
            content = content.replace(old, new)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"  ✅ {desc}")
            return True
        else:
            print(f"  ⏭️ {desc} (已应用或未找到)")
            return False
    except Exception as e:
        print(f"  ❌ {desc}: {e}")
        return False

def patch_lines(filepath, old_line, new_line, desc):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            if old_line in line:
                lines[i] = line.replace(old_line, new_line)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                print(f"  ✅ {desc}")
                return True
        print(f"  ⏭️ {desc} (已应用或未找到)")
        return False
    except Exception as e:
        print(f"  ❌ {desc}: {e}")
        return False

print("=" * 50)
print("🔧 应用所有补丁...")
print("=" * 50)

# 1. Tushare代理配置
print("\n[1] Tushare代理配置")
patch_lines(
    "/app/data_provider/tushare_fetcher.py",
    'api_url: str = "http://api.tushare.pro"',
    'api_url: str = os.getenv("TUSHARE_API_URL", "http://api.tushare.pro")',
    "Tushare API URL改为代理"
)

# 2. 评分上限移除
print("\n[2] 评分上限移除")
patch_lines(
    "/app/src/analyzer.py",
    "min(59, max(45, score))",
    "score",
    "移除评分上限"
)
patch_lines(
    "/app/src/analyzer.py",
    "min(79, max(45, score))",
    "score",
    "移除评分上限(79)"
)

# 3. 美股资金流不降级
print("\n[3] 美股资金流降级逻辑")
patch_file(
    "/app/src/analyzer.py",
    '            if "buy" in current_decision and current_score >= 55:\n                result.decision_type = "hold"\n                result.operation_advice = _build_operation_advice(\n                    "hold", support_price, pressure_price, entry_price, profit_target, stop_loss_price\n                )\n                result.sentiment_score = _bound_hold_watch_sentiment_score(current_score)\n                self._update_dashboard_conclusion(result, "hold", context.get("asset_type", "A股"))\n                result.add_decision_change("买入→持有观察", f"资金流数据暂不可用，保守观望，原评分{raw_score}分", "hold", downgrade=True)\n                logger.info(f"[decision_stability] Downgraded buy because capital flow is unavailable: {flow_bias}")',
    '            asset_type = fundamental_context.get("asset_type", "A股") if isinstance(fundamental_context, dict) else "A股"\n            if asset_type not in ("A股", "港股"):\n                logger.info(f"[decision_stability] 美股/海外资金流不可用，保留原决策: {flow_bias}")\n            elif "buy" in current_decision and current_score >= 55:\n                result.decision_type = "hold"\n                result.operation_advice = _build_operation_advice(\n                    "hold", support_price, pressure_price, entry_price, profit_target, stop_loss_price\n                )\n                result.sentiment_score = _bound_hold_watch_sentiment_score(current_score)\n                self._update_dashboard_conclusion(result, "hold", asset_type)\n                result.add_decision_change("买入→持有观察", f"资金流数据暂不可用，保守观望，原评分{raw_score}分", "hold", downgrade=True)\n                logger.info(f"[decision_stability] Downgraded buy because capital flow is unavailable: {flow_bias}")',
    "美股资金流不可用时保留原决策"
)

# 4. 投资大师显示全部
print("\n[4] 投资大师显示")
patch_lines(
    "/app/src/notification.py",
    "top_matches = master_review.get('top_matches', [])",
    "masters = master_review.get('masters', [])",
    "投资大师数据源改为masters"
)
patch_lines(
    "/app/src/notification.py",
    "if top_matches:",
    "if masters:",
    "投资大师条件判断"
)
patch_lines(
    "/app/src/notification.py",
    "top_matches[:3]",
    "masters",
    "投资大师显示全部"
)

# 5. 飞书bot默认报告类型改为full
print("\n[5] 飞书bot报告类型")
patch_file(
    "/app/bot/commands/analyze.py",
    '        report_type = "simple"',
    '        report_type = "full"',
    "飞书bot默认报告类型改为full"
)

# 5b. 报告类型改为full
print("\n[5] 报告类型配置")
patch_file(
    "/app/src/config.py",
    '    report_type: str = "simple"',
    '    report_type: str = "full"',
    "默认报告类型改为full"
)
# Pipeline中的fallback也改为full
patch_file(
    "/app/src/core/pipeline.py",
    "getattr(self.config, 'report_type', 'simple').lower()",
    "getattr(self.config, 'report_type', 'full').lower()",
    "Pipeline config读取默认值改为full"
)
patch_lines(
    "/app/src/core/pipeline.py",
    "            report_type = ReportType.SIMPLE\n",
    "            report_type = ReportType.FULL\n",
    "Pipeline fallback改为FULL"
)

# 6. 投资风格注入（在评分标准前）
print("\n[6] 投资风格注入")
style_marker = "## 评分标准"
if style_marker in open("/app/src/analyzer.py", "r").read():
    with open("/app/src/analyzer.py", "r") as f:
        content = f.read()
    if "用户投资风格偏好" not in content:
        style_text = """## 用户投资风格偏好

**风格：激进型（Aggressive）**
- 偏好中小盘成长股，追求高爆发力和高弹性
- 对波动容忍度高，愿意承担更高风险换取更高收益
- 重视题材催化、资金关注度、趋势动能
- 中小盘股的爆发力应被视为优势而非劣势
- 不因资金流数据缺失就保守降级

## 爆发力评估框架（核心！）

**不要仅看股票本身的基本面，要重点评估其"概念催化"强度：**
1. **概念关联度**：该股票是否属于当前市场热点概念的受益标的
2. **催化强度**：结合最新资讯判断催化因素的强度（强/中/弱）
3. **弹性评估**：中小盘概念股的弹性远高于大盘股
4. **动态评分**：根据当前资讯动态判断，不要给出固定分数

"""
        content = content.replace(style_marker, style_text + style_marker)
        with open("/app/src/analyzer.py", "w") as f:
            f.write(content)
        print("  ✅ 注入激进型投资风格")
    else:
        print("  ⏭️ 投资风格已存在")

# 6. 概念股注入（在stock_pool导入后注入到analyzer）
print("\n[6] 概念股关联注入")
with open("/app/src/analyzer.py", "r") as f:
    content = f.read()
if "get_concept_context" not in content:
    content = content.replace(
        "from stock_pool import get_pool_context, get_pool_summary, get_stock_pool",
        "from stock_pool import get_pool_context, get_pool_summary, get_stock_pool, get_concept_context"
    )
    with open("/app/src/analyzer.py", "w") as f:
        f.write(content)
    print("  \u2705 添加concept_context导入")

# 6b. 修复notification.py中的stock_code属性名
print("\n[6b] 修复notification属性名")
with open("/app/src/notification.py", "r") as f:
    ncontent = f.read()
if "result.stock_code" in ncontent:
    ncontent = ncontent.replace("result.stock_code", "result.code")
    with open("/app/src/notification.py", "w") as f:
        f.write(ncontent)
    print("  \u2705 修复result.stock_code -> result.code")
else:
    print("  \u23ed\ufe0f 属性名已修复")

# 7. 复制stock_pool.py（始终从patches目录复制最新版本）
print("\n[7] 股票池模块")
src = "/app/patches/stock_pool.py"
if os.path.exists(src):
    import shutil
    shutil.copy(src, "/app/stock_pool.py")
    print("  ✅ 复制stock_pool.py")
else:
    print("  ⏭️ stock_pool.py不存在于patches目录")

print("\n" + "=" * 50)
print("🎉 所有补丁应用完成！")
print("=" * 50)
