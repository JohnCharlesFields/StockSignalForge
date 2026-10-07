# StockSignalForge · 股讯工坊

**基于 GilData MCP 打造的 AI 股票研究工作台。**

AI 股票分析 · 信号扫描 · 个股研判 · 持仓复盘 · 策略回测

StockSignalForge（股讯工坊）以 GilData MCP（恒生聚源金融数据服务）的研究数据接入为特色，将金融数据查询、研究上下文、图表分析和策略验证组织在一个工作台中。

代码框架源自 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)，保留上游 MIT 许可与版权声明。GilData MCP 是本项目接入的外部数据服务；本仓库不包含该服务源码、凭据或商用数据，也不代表 GilData 官方产品。当前 GilData 适配用于显式触发的研究补充与缓存读取，并非全部行情与回测数据的唯一来源。

GilData MCP 服务与授权申请请参见 [恒生聚源数据地图](https://www.gildata.com/products/datamap)。使用者自行配置 MCP 与各数据源 API key。

## 界面预览

[完整页面图册：12 张桌面原图、6 张移动端截图与完整功能索引](SCREENSHOTS.md) · [GilData MCP 接入说明](GILDATA_MCP.md)

桌面端采用使用者提供的 12 张深色主题原图，原始 PNG 未裁切、重采样或改写，点击图片下方链接可查看原图。截图保留实际部署中的 easymoneysniper 名称；开源仓库名称为 StockSignalForge。画面里的报价、日期、研究指标与任务状态属于各自的历史快照，不代表当前行情。使用者选择公开的自选备注与博主观点在这些截图中保留；源码中的私有 MCP 地址、API key 与运行数据库仍不随仓库发布。移动端沿用已有 6 张截图，其中持仓为合成演示数据。

以下按“发现候选 → 个股研判 → 资讯与研究范围 → 任务执行”的顺序展示核心能力。GilData MCP 提供研究补充数据接入，各页面还结合其他行情来源、信号计算和模型解释。

### 回调买入榜：每日研究入口

从回调候选开始建立研究清单。顶部集中展示优先标的和相关候选，主体显示榜单口径、数据状态、高流动性过滤、生命周期监测及候选统计，让读者先了解数据是否准备完成，再比较研究对象。

画面中的优先级、校准胜率、相对大盘强度、波动趋势和择时折扣分别承担不同角色。校准胜率指历史口径下扣除成本后跑赢标的自身基线的概率；“已验证”标记与历史校准有关。截图明确提示候选批次已过期，并保留实盘验证期提醒，阅读时应先核对交易日与数据来源。

![回调买入榜：每日研究入口](assets/screenshots/desktop/01-priority-overview.png)

[查看原图](assets/screenshots/desktop/01-priority-overview.png) · [图册中的详细介绍](SCREENSHOTS.md#01-priority-overview)

### 个股研判：K 线、均线与价位结构

输入股票代码后，在同一页面查看报价、缓存截止日期与行情图表。截图以 DELL 为例，展示三个月日线、EMA 均线、历史买入标记、成交量相关开关，以及买入、停止买入、止损和当前价等价位线。

图表支持时间范围和周期切换，提供 VOL、MACD、RSI、KDJ、HV 等指标入口。绿色与红色背景带解释买卖主导区域，POC 线用于观察成交密集价位；这些图层可作为结构复核线索。画面标注“日线·非实时”和缓存收盘日期，BLOCK OPEN 状态也一并保留。

![个股研判：K 线、均线与价位结构](assets/screenshots/desktop/03-stock-chart.png)

[查看原图](assets/screenshots/desktop/03-stock-chart.png) · [图册中的详细介绍](SCREENSHOTS.md#03-stock-chart)

### 个股研判：新闻情绪、公司业务与产业链

将价格研究与公司背景放在一起。上方汇总近期新闻的正面、中性、负面标签、短期影响估计和原始来源；下方展示 Dell 的行业分类、市值、财报时间、主营业务与主要产品，帮助读者先理解公司靠什么经营。

公司卡片进一步列出上游供应商、下游客户、竞争对手、核心竞争力和赛道地位，适合梳理产业链与研究问题。新闻情绪是启发式标注，画面中的影响值也有估计口径；公司资料与新闻各自显示来源和日期，应与最新披露交叉核对。

![个股研判：新闻情绪、公司业务与产业链](assets/screenshots/desktop/04-stock-context.png)

[查看原图](assets/screenshots/desktop/04-stock-context.png) · [图册中的详细介绍](SCREENSHOTS.md#04-stock-context)

### 个股研判：信号判定、隔夜 Alpha 与行动区间

把研究信号拆成可检查的条件：校准胜率、回调状态、波动变化和相对大盘强度集中显示。截图保留“处于回调区，但波动尚未扩张”的解释，读者可以看到当前条件与进一步确认条件之间的关系。

左侧独立展示隔夜 Alpha 胜率、强隔夜胜率、平均超额、样本量和 Beta，并用归一化曲线比较标的与 SPY。右侧列出强势确认、回踩复核、停止追高、停止买入及止损止盈价位，用于盘中人工复核。隔夜口径与回调口径分开标注，画面也说明行动区间不构成自动下单指令。

![个股研判：信号判定、隔夜 Alpha 与行动区间](assets/screenshots/desktop/05-stock-signal.png)

[查看原图](assets/screenshots/desktop/05-stock-signal.png) · [图册中的详细介绍](SCREENSHOTS.md#05-stock-signal)

### 个股研判：AI 复核、支持因素与风险

AI 复核将模型结论展开为支持因素与反对因素，保留原始指标字段供追溯。截图同时列出趋势与相对强度、波动和 Gamma 环境、宏观代理及估值信息，并说明当前价格、校准胜率与买入区间之间存在的约束。

关注价位卡片汇总强势触发、回踩触发、停止买入、止损与止盈位置；下方指标对照解释校准胜率和 Beta 的参考范围。模型名、风险因素和限制条件都保留在画面中，方便把生成解释与结构化数据逐项核对；应结合条件和数据日期阅读整个结论。

![个股研判：AI 复核、支持因素与风险](assets/screenshots/desktop/06-stock-ai-review.png)

[查看原图](assets/screenshots/desktop/06-stock-ai-review.png) · [图册中的详细介绍](SCREENSHOTS.md#06-stock-ai-review)

### 盘前新闻：宏观事件卡片与阅读反馈

通过卡片逐条阅读盘前资讯。示例将美联储相关事件归为全市场宏观新闻，展示中文摘要、媒体来源、时间、主题、新闻方向、影响范围与冲击评分，使读者快速判断事件需要从大盘还是个股角度复核。

侧栏提供预计开盘影响、波动带宽和相关指数入口，原文与出处可展开查看；底部勾选与跳过按钮用于处理阅读队列和反馈。页面保留后台刷新状态与最新入库时间。冲击分、方向和影响带宽是不同维度，应配合正文与出处理解其含义。

![盘前新闻：宏观事件卡片与阅读反馈](assets/screenshots/desktop/08-news-cards.png)

[查看原图](assets/screenshots/desktop/08-news-cards.png) · [图册中的详细介绍](SCREENSHOTS.md#08-news-cards)

### 自选池：研究范围、备注与批量分析

维护个人研究股票池，支持单只添加、填写关注原因和批量导入，并提供自选池三层分析入口。截图展示实际自选列表及备注，说明研究范围可以由使用者主动维护，模型运行时也会明确标注来源池。

列表汇总现价、当日表现、市值与 PE、行业、20 日表现、60 日 Alpha、回调或启动状态、GEX、财报时间和 Alpha 胜率。页面注明 Alpha 基准为纳斯达克／QQQ，并显示后台补充进度；数据不完整、无信号和已有信号的状态可以分别辨认。

![自选池：研究范围、备注与批量分析](assets/screenshots/desktop/10-watchlist.png)

[查看原图](assets/screenshots/desktop/10-watchlist.png) · [图册中的详细介绍](SCREENSHOTS.md#10-watchlist)

### 任务中心：每日扫描进度与运行结果

查看每日三层扫描是否完成、当前处理进度、计划时间、启用状态和最近运行结果。截图保留本次 running 与上次 failed 两种状态，让读者了解任务中心如何区分正在执行的扫描与历史执行记录。

页面支持继续未完成池、强制重跑和恢复榜单，并按股票池显示复用或完成情况；新闻更新、事件补充和 AI 复核另有状态说明。飞书移动端区域在截图中显示隧道未运行，没有公开访问地址。这里适合定位数据准备或扫描任务的卡点，再选择相应恢复操作。

![任务中心：每日扫描进度与运行结果](assets/screenshots/desktop/12-task-center.png)

[查看原图](assets/screenshots/desktop/12-task-center.png) · [图册中的详细介绍](SCREENSHOTS.md#12-task-center)

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

### GilData MCP 在项目中的作用

- 调用 GilData MCP 的 FinQuery 接口获取研究补充数据。
- 对返回表格做结构化解析，并检查股票、日期、币种与数据口径。
- 将规范化样本缓存给个股研究上下文使用；普通页面读取不会自动触发 GilData 查询。
- 通过本地环境变量配置地址与 token，公开代码不包含私有 MCP 配置。

GilData 是外部金融数据服务；项目的代码框架、其他行情来源与策略验证能力有各自的来源和实现，详见 [接入说明](GILDATA_MCP.md)。

## 基础能力

- React 前端与 FastAPI 后端，Docker 单容器运行。
- 交易研究、策略回测、个股研判、持仓分析与研究会话。
- GilData MCP FinQuery 接入与数据规范化，以及可选的其他行情来源。
- 对外 MCP 服务，供支持 MCP 的客户端调用。

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
| GilData MCP | agent/.env 中的 GILDATA_MCP_URL 与 GILDATA_MCP_TOKEN；填写自己的 HTTPS 服务地址和 token |
| 其他数据源 | agent/.env 中对应的 API key/token；按需启用 |
| 外部 MCP 服务 | 默认 ~/.vibe-trading/agent.json；从 config/agent.example.json 的空模板开始配置 |
| MCP 客户端 | config/mcp-client.example.json 仅提供本项目 MCP 服务的无凭据模板 |
| API 访问认证 | agent/.env 中的 API_AUTH_KEY |

未配置 GilData MCP 时，不会自动调用该服务。没有填入真实 key 的示例配置无法运行模型研究或受授权限制的数据查询。GilData 查询由 agent/scripts/probe_gildata_shadow.py 显式触发，可能产生服务费用。

默认网页端口仅绑定本机。公开源码不包含个人数据、历史研究会话、浏览器 cookies、行情数据库、内部维护日志或迁移备份。附带的 daily_stock_analysis 为可选适配代码与补丁，其外部服务镜像和历史数据库由使用者自行提供。

## 开发与许可

Python 3.11+，Node.js 20+。后端开发依赖：pip install -e ".[dev]"；前端在 frontend 目录执行 npm ci 与 npm run dev。开发前端的 API 目标通过 VITE_API_URL 配置。

保留的上游使用说明见 README.upstream.md 与 README_zh.upstream.md。请结合本仓库的配置变更使用，不要沿用他人的服务地址或密钥。Wiki 部署需手动触发，并在 GitHub 配置自己的 Cloudflare secrets 和 CLOUDFLARE_PAGES_PROJECT 变量。

本项目沿用 [MIT License](LICENSE)，第三方声明见 [NOTICE](NOTICE)。本仓库的开源许可不授予第三方数据服务的访问权限或数据再分发授权。
