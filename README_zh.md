# StockSignalForge

**easymoneysniper**

[English](README.md) | **中文** | [日本語](README_ja.md) | [한국어](README_ko.md) | [العربية](README_ar.md)

[![CI](https://github.com/JohnCharlesFields/StockSignalForge/actions/workflows/test.yml/badge.svg)](https://github.com/JohnCharlesFields/StockSignalForge/actions/workflows/test.yml)

AI股票研究工作台，基于 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 框架构建，整合查行情、看新闻、筛选回调候选、研判个股、持仓复盘、策略回测等功能的一站式平台。

> **本项目主要面向美股和美股期权研究。A股、港股仍在适配中，暂不承诺完整支持。** 上游已有某个市场的适配器，不代表本分支的行情、日历、筛选、风险与验证流程都已适配完成。

## 市场支持范围

| 市场 | 当前范围 |
| --- | --- |
| 美股正股 | 核心研究流程：日常候选、图表、公司资料、新闻、自选和策略研究 |
| 美股期权 | 方向研究、可用IV/Greeks，以及手动计算的60–90天Call合约与退出情景；受行情授权及数据质量约束 |
| A股 | 适配中，尚未完成端到端流程验证 |
| 港股 | 适配中，尚未完成端到端流程验证 |

仅提供研究辅助，不保证收益，不连接券商或自动提交实盘订单。SOXL执行实验属于模拟，不是实盘交易服务。

## 功能地图

| 模块 | 用途 |
| --- | --- |
| 回调买入榜 | 缓存候选排序、持续生命周期监测、预测与后续兑现对账 |
| 个股研判 | K线、趋势结构、因果研究标记、公司/行业/供应链参考及风险价位 |
| 期权研究 | 区分Call/Put/观望方向，查看可用波动率数据，比较合约及退出情景，不编造缺失报价 |
| 新闻与博主雷达 | 按重要度阅读新闻卡片/清单、近期新闻总结、基于真实文稿的观点与来源状态 |
| 自选与持仓 | 管理研究范围、复核持仓和有日期的研究证据 |
| 三层信号 | 机会质量、启动择时/日隧道、事件风险及通过质量闸的GEX证据 |
| 验证与任务 | 因子、回测、分版本前向证据、定时扫描、断点复用和缓存日期 |

可用能力取决于配置的数据源。缺少IV、OI、成交量、字幕、财报日期或历史成分证据时，明确标未知，不当作0或伪造结果。

## 界面预览

![实验性日线买卖点与风险参考](assets/screenshots/desktop/13-v-swing-chart.png)

![公司关系与带日期的同业比较](assets/screenshots/desktop/14-company-network.png)

截图为历史研究快照，不是收益证明；更多见 [页面图册](SCREENSHOTS.md)。文档支持多语种，不代表应用所有页面都已翻译。

## Docker快速启动

使用Docker Engine/Compose，或Docker Desktop的Linux容器模式。在仓库根目录执行；已有私有环境文件不会被下面命令覆盖。

```powershell
if (!(Test-Path agent/.env)) { Copy-Item agent/.env.example agent/.env }
# Configure agent/.env locally before starting the application.
docker compose up -d --build vibe-trading
```

启动前在本地编辑 `agent/.env`，填写自己使用的数据和模型服务凭据。打开 [工作台](http://127.0.0.1:19090)，通过 [健康检查](http://127.0.0.1:19090/health) 验证服务。

```sh
docker compose logs --tail 100 vibe-trading
docker compose stop vibe-trading
```

默认只绑定本机端口；公网访问必须配置认证与明确的可信Origin。

### 更新已有部署

先等待扫描、校准、富集及其他活动任务结束，再使用SQLite在线备份接口备份数据库，并保留所有命名卷。

```sh
docker compose build vibe-trading
docker compose up -d --no-deps vibe-trading
```

不要执行 `docker compose down -v`。克隆源码不会自动迁移历史数据库、缓存、私有配置或Cookies，应另行迁移一致性备份。

## 数据和私有配置

| 配置 | 本地位置 |
| --- | --- |
| 模型服务 | `agent/.env`中的`LANGCHAIN_PROVIDER`、`LANGCHAIN_MODEL_NAME`及对应凭据 |
| 聚源MCP | `GILDATA_MCP_URL`与`GILDATA_MCP_TOKEN`分别填写；见 [接入说明](GILDATA_MCP.md) |
| 其他行情源 | 自己的API key及对应数据权限 |
| API认证 | `API_AUTH_KEY`，不要暴露无认证的私人部署 |
| 外部MCP工具 | 从`config/agent.example.json`和`config/mcp-client.example.json`开始配置 |

聚源是可选研究证据来源，不是项目联合品牌或收益背书。股票数据授权不等于OPRA期权授权；请核对报价日期和备用来源状态。

源码不包含密钥、带认证MCP地址、Cookies、数据库、供应商历史数据或私人训练标注。可选后台更新/训练需要明确配置，公开默认值不会自动激活新模型。

## 胜率与回测边界

- 排序分、正股校准概率、交易净正收益比例、相对SPY的alpha与期权盈利概率不是同一口径。
- 正股方向判断正确，期权仍可能因权利金、买卖价差、时间衰减和IV变化亏损；情景表不是概率加权的期望值。
- v-swing及多空研究标记不等于已验证可盈利成交；完整日线确认后不能假定还能以刚刚观察到的收盘价成交。
- 公司行动单位、实际可知时间、历史成分和完整事件修订需有证据；缺失时阻断认证。
- 严格5/8交易日校准重建因合格历史时点样本不足未激活新曲线，旧数值保留为未验证研究参考。
- 固定规则历史复算未证实稳定正期望；已看历史不称为新盲测，软件测试通过也不保证全系统无泄漏或过拟合。

## 更新与验证

10月20日恢复 **StockSignalForge / easymoneysniper**，同步拆股记账、历史时点证据修复，并修复CI的夹具导入、合成数据、财报工作日和SDK异常兼容。

已发布的`162054e`在GitHub CI中 **3321项测试及45项子测试通过，2项既有跳过**。当前提交是否通过，请查看顶部实时徽标，不能把CI当作交易盈利证明。

- [更新日志](CHANGELOG.md)
- [配置和实际测试记录](PUBLIC_RELEASE.md)
- [最新功能、Bug原因及研究边界](wiki/RELEASE_20261020_ZH.md)
- [实验波段研究口径](wiki/V_SWING_RESEARCH_ZH.md)
- [Call情景口径](wiki/CALL_PLAN_RESEARCH_ZH.md)

## 开发

Python 3.11+、Node.js 20+；开发前端通过`VITE_API_URL`配置API目标。

```sh
python -m pip install -e ".[dev]"
pytest --ignore=agent/tests/e2e_backtest --ignore=agent/tests/test_e2e_harness_v2.py --tb=short -q
cd frontend
npm ci
npm run build
```

真实模型端到端测试需单独明确启用并提供授权，不要把生产数据卷挂入隔离测试。

## 来源与许可

基于 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)，保留 [英文上游说明](README.upstream.md) 和 [中文上游说明](README_zh.upstream.md)。

沿用 [MIT License](LICENSE)，第三方声明见 [NOTICE](NOTICE)。软件开源许可不授予第三方数据访问权或再分发授权。
