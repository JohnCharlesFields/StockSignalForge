# StockSignalForge · 股讯工坊

**基于恒生聚源（Glidata）MCP 打造的 AI 股票研究工作台。**

查行情、看新闻、筛选回调候选、研判个股，再到持仓复盘和策略回测，都可以在一个工作台里完成。恒生聚源（Glidata）MCP 接入用于补充个股研究数据，行情和信号计算也支持其他数据源。

项目框架基于 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)，沿用 MIT 许可。恒生聚源（Glidata）MCP 的服务授权和密钥需要自行申请，见 [接入说明](GILDATA_MCP.md)。

## 界面预览

[完整页面图册](SCREENSHOTS.md) · [恒生聚源（Glidata）MCP 接入说明](GILDATA_MCP.md)

截图中的行情与研究结果为历史快照，移动端持仓为演示数据。

### 回调买入榜：每日研究入口

每天先看这张榜：哪些股票正在回调，哪些相对大盘更强，哪些有足够的流动性。可以筛选高流动性或深超卖标的，也可以查看候选连续出现的天数、排名变化和后续表现。

榜单同时列出优先级、校准胜率、相对强度、波动趋势和择时折扣。校准胜率衡量的是历史样本中扣除成本后跑赢自身基线的概率；“已验证”表示经过历史校准。顶部会提示数据日期和过期状态。

![回调买入榜：每日研究入口](assets/screenshots/desktop/01-priority-overview.png)

[查看原图](assets/screenshots/desktop/01-priority-overview.png) · [更多截图](SCREENSHOTS.md#01-priority-overview)

### 个股研判：K 线、均线与价位结构

输入股票代码，就能查看 K 线、均线和关键价位。这里以 DELL 为例，叠加 EMA、历史买入标记、当前价、回踩区、停止买入线和止损线，方便把信号放回走势中看。

支持日线、小时线和分钟线切换，以及 VOL、MACD、RSI、KDJ、HV 等指标。红绿背景表示买卖主导区域，POC 线标记成交密集价位。报价旁会注明收盘日期和缓存状态。

![个股研判：K 线、均线与价位结构](assets/screenshots/desktop/03-stock-chart.png)

[查看原图](assets/screenshots/desktop/03-stock-chart.png) · [更多截图](SCREENSHOTS.md#03-stock-chart)

### 个股研判：新闻情绪、公司业务与产业链

看价格之前，也要知道公司做什么。公司卡片列出行业、市值、财报日期、主营业务和产品，并整理上游供应商、下游客户、竞争对手及赛道地位。

同页汇总近期新闻、来源和发布时间，按正面、中性、负面标注情绪，并给出短期影响估计。以 Dell 为例，可以一起查看 AI 服务器、存储、网络与 PC 业务的背景。新闻标签和影响值属于模型估计。

![个股研判：新闻情绪、公司业务与产业链](assets/screenshots/desktop/04-stock-context.png)

[查看原图](assets/screenshots/desktop/04-stock-context.png) · [更多截图](SCREENSHOTS.md#04-stock-context)

### 个股研判：信号判定、隔夜 Alpha 与行动区间

信号区同时展示校准胜率、回调状态、波动变化和相对大盘强度。比如这里的 DELL 正在回调，但波动还未扩张，页面会提示等待进一步确认。

左侧是隔夜 Alpha 统计，包括胜率、平均超额、样本量、Beta 和与 SPY 的走势对比。右侧列出强势确认、回踩买入、停止追高、停止买入、止损和止盈价位。这些条件供盘中复核，不会自动下单；隔夜统计与回调信号分别计算。

![个股研判：信号判定、隔夜 Alpha 与行动区间](assets/screenshots/desktop/05-stock-signal.png)

[查看原图](assets/screenshots/desktop/05-stock-signal.png) · [更多截图](SCREENSHOTS.md#05-stock-signal)

### 个股研判：AI 复核、支持因素与风险

AI 复核会给出判断依据，也会列出反对理由。趋势、相对强度、波动、Gamma、宏观环境和估值等数据都能在支持因素与风险栏中找到，方便检查结论是否有数据支撑。

下方汇总买入触发、停止买入、止损和止盈价位，并解释校准胜率、Beta 等指标的参考范围。模型给出的等待回踩或放弃条件与具体数值一起显示，避免只剩一句看多或看空。

![个股研判：AI 复核、支持因素与风险](assets/screenshots/desktop/06-stock-ai-review.png)

[查看原图](assets/screenshots/desktop/06-stock-ai-review.png) · [更多截图](SCREENSHOTS.md#06-stock-ai-review)

### 盘前新闻：宏观事件卡片与阅读反馈

盘前新闻可以一条条翻着看。每张卡片包含中文摘要、媒体来源、发布时间、涉及标的和事件标签；宏观新闻会单独标为全市场事件。

右侧显示冲击分、预计开盘影响和波动带宽，并提供相关指数入口。英文原文与出处可以展开，底部勾选或跳过用于处理阅读队列。顶部还能查看后台刷新状态和最新入库时间。

![盘前新闻：宏观事件卡片与阅读反馈](assets/screenshots/desktop/08-news-cards.png)

[查看原图](assets/screenshots/desktop/08-news-cards.png) · [更多截图](SCREENSHOTS.md#08-news-cards)

### 自选池：研究范围、备注与批量分析

把自己关注的股票放进自选池，记下关注原因，后续研究就从这份名单开始。支持逐只添加和批量导入，也能直接运行自选池三层分析。

列表汇总现价、涨跌幅、市值、PE、行业、20 日表现、60 日 Alpha、回调或启动状态、GEX 与财报日期。自选池的 Alpha 以纳斯达克／QQQ 为基准；数据补充进度和无信号状态也会显示。

![自选池：研究范围、备注与批量分析](assets/screenshots/desktop/10-watchlist.png)

[查看原图](assets/screenshots/desktop/10-watchlist.png) · [更多截图](SCREENSHOTS.md#10-watchlist)

### 任务中心：每日扫描进度与运行结果

每日扫描跑到哪了、上次为什么没完成，都可以在任务中心查看。页面显示股票池进度、计划时间、最近运行结果，以及新闻、事件和 AI 复核的更新状态。

中断后可以接着跑未完成的股票池，也可以强制重跑或恢复榜单。各池的完成与复用情况逐项列出。飞书移动端入口在这里查看隧道状态和访问地址。

![任务中心：每日扫描进度与运行结果](assets/screenshots/desktop/12-task-center.png)

[查看原图](assets/screenshots/desktop/12-task-center.png) · [更多截图](SCREENSHOTS.md#12-task-center)

## 功能地图

| 模块 | 能做什么 |
| --- | --- |
| 交易台 | 回调候选排序、连续监测、预测兑现对账与候选篮子 |
| 个股研究 | 行情图表、公司与赛道背景、研究信号和风险价位 |
| 新闻与观点 | 盘前卡片／清单、观点雷达与事件线索 |
| 自选与持仓 | 管理研究范围、复盘成本和仓位、比较研究建议 |
| 进阶研究 | 启动信号、实股机会、事件雷达、宏观风险与三维汇总 |
| 策略验证 | 因子库与 Bench、回测详情、策略对比和相关性矩阵 |
| 自动化与助手 | 任务中心、研究会话、工具反馈与 MCP 服务 |
| 移动端 | 榜单、新闻、个股、持仓、自选和更多入口 |

### 恒生聚源（Glidata）MCP 接入

通过 FinQuery 查询个股研究数据，解析返回表格后检查标的、日期和币种，再写入本地缓存供研究页面读取。查询需要手动触发，普通页面访问不会自动调用外部服务。

前端使用 React，后端使用 FastAPI，支持 Docker 单容器部署，也提供供其他客户端调用的 MCP 服务。

## 快速启动

需要 Docker Desktop（Linux 容器模式）。在仓库根目录执行：

```powershell
Copy-Item agent/.env.example agent/.env
# 编辑 agent/.env，填写所用模型和数据服务的凭据
docker compose up -d --build vibe-trading
```

打开 [本地网页](http://127.0.0.1:19090)，可用 [健康检查](http://127.0.0.1:19090/health) 验证服务。也可运行 Windows 的 ./start.ps1。

```powershell
docker compose logs --tail 100 vibe-trading
docker compose stop vibe-trading
```

## 私有配置

真实配置只保存在本地，均已加入 .gitignore：

| 配置 | 位置或变量 |
| --- | --- |
| 模型服务 | agent/.env 中的 LANGCHAIN_PROVIDER、LANGCHAIN_MODEL_NAME 和对应 API key |
| 恒生聚源（Glidata）MCP | agent/.env 中的 GILDATA_MCP_URL 与 GILDATA_MCP_TOKEN；填写自己的 HTTPS 服务地址和 token |
| 其他数据源 | agent/.env 中对应的 API key/token；按需启用 |
| 外部 MCP 服务 | 默认 ~/.vibe-trading/agent.json；从 config/agent.example.json 的空模板开始配置 |
| MCP 客户端 | config/mcp-client.example.json 仅提供本项目 MCP 服务的无凭据模板 |
| API 访问认证 | agent/.env 中的 API_AUTH_KEY |

模型研究和授权数据查询需要对应的 API key。恒生聚源（Glidata）MCP 查询通过 `agent/scripts/probe_gildata_shadow.py` 手动触发，费用按服务授权计。

默认网页端口仅绑定本机。密钥、MCP 私有配置、运行数据库和研究会话不随源码发布。`daily_stock_analysis` 是可选适配模块，需自行准备外部服务镜像和数据库。

## 开发与许可

Python 3.11+，Node.js 20+。后端开发依赖：pip install -e ".[dev]"；前端在 frontend 目录执行 npm ci 与 npm run dev。开发前端的 API 目标通过 VITE_API_URL 配置。

上游说明见 [README.upstream.md](README.upstream.md) 与 [README_zh.upstream.md](README_zh.upstream.md)。部署配置以本仓库的示例为准。Wiki 部署需手动触发，并在 GitHub 配置自己的 Cloudflare secrets 和 CLOUDFLARE_PAGES_PROJECT 变量。

本项目沿用 [MIT License](LICENSE)，第三方声明见 [NOTICE](NOTICE)。本仓库的开源许可不授予第三方数据服务的访问权限或数据再分发授权。
