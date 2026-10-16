# 配置与测试说明

## 2026-10-16 因果验证与前向版本对账发布

- 限定同步已完成的时间因果修复、缓存离线重验证、版本账本及必要GEX质量依赖；未将生产目录整包覆盖。最新功能与Bug说明见[中文更新](wiki/RELEASE_20261016_ZH.md)。
- 公开默认值未变：自动训练/参数激活关闭，波段opt-in，不分发私人标注，GilData仍要求自己配置私有HTTPS地址与独立token，OPRA自动预算不提高。
- **公开源码独立禁网验收：125项通过、另9子测试通过，27.94秒**，包括时间因果、校准、版本不可覆盖、模式隔离、GEX消费端、行情缓存和公开配置边界；4条既有Pandas弃用警告。不是全仓CI通过声明，下面的历史失败清单保留。
- 独立公开Docker镜像通过TypeScript/Vite及现有Dockerfile构建（2701模块）；旧Browserslist与大图表chunk警告保留。只合并回调榜需要的前端DTO，未带入无关日历/持仓接口。
- 无网络、无密钥、自动任务关闭的公开镜像启动验收通过：health、market-data/status、v-swing/status、首页及3入口资源、只读Call计划均200；波段默认关闭，不连接生产数据卷。
- 桌面1440/手机390的完整、空、503六组页面验收通过，旧请求隔离、默认新口径、缺版本与待结记录、无横向溢出均验证，0JS异常/0业务写请求；业务API使用合成数据，不是盈利验证。
- 初次公开包构建暴露无关DTO依赖，后收窄合并；首次GEX回归暴露漏同步消费端，补齐已有实现后复测通过；市场数据测试相对路径改用隔离内存运行目录。未删除失败测试或改成本让结果变绿。
- 私密扫描覆盖1544份候选公开文件与10个本地私有值，另检查令牌、认证URL、绝对私有路径、Cookie/数据库/缓存/私人交接规则，真实命中0；完整扫描在提交前再次核验，不输出凭据。
- 历史重验证未通过候选启用门槛，不宣传胜率提升、无过拟合或完整PIT认证；没有在公开副本启用候选，旧记录/真实数据不发布。此次GitHub操作不重启生产服务、不跑全池或收费数据。

## 2026-10-10 增量发布范围

- 以现有 GitHub main 为基准同步10月9–10日已经完成的波段、公司图谱/资料、新闻、Call情景、搜索及日批修复；不是直接推送整个生产目录。保留原许可、署名、数据库文件名、私有MCP配置校验及历史截图。
- 新增两张用户明确授权发布的截图，分别展示日线实验买卖点与公司关系。全部为历史研究快照，不是交易收益证明。
- `agent/config/v_swing.json` 默认 `enabled=false`；仅发布空 `v_swing_annotations.example.json`。私人标注文件被忽略，测试使用合成标注，不分发私人训练数据。无标注时明确拒绝训练；标注数量按真实输入统计，不硬编码132/66。
- 主榜仍沿用原收益校准概率；RF匹配强度不作盈利概率。股票分组与逐月回放不能消除人工后视标注、幸存者和复权限制。真实期权情景也没有概率权重，不宣传EV或最佳卖点。
- 公开GilData模块保留“私有HTTPS地址与token独立配置”的原安全修复；不从生产版本覆盖默认供应商地址。自动OPRA预算默认0，手动Call预算需用户确认。Cookies、数据库、供应商样本、运行产物及个人交接日志不发布。
- **公开包实际验证**：25个相关测试文件在禁网临时容器中 **294项通过、另20个子测试通过**（76.88秒）；数据库/运行目录独立临时挂载，不挂载生产数据。12项既有Pydantic/FastAPI警告保留，非全仓CI通过声明。
- TypeScript检查与Vite生产构建通过（2700模块）；仍有Browserslist过期和大图表chunk提示，不为发布升级无关依赖。首次Vite被本机沙箱禁止启动esbuild，授权本地构建后成功；首次隔离容器的空目录挂载失败发生在测试开始前，修正后才取得上述结果。
- 继续保留10月9日全量CI失败清单；本次修正并验证了缓存图表测试，其余旧问题不冒称解决。生产Docker不在本次GitHub操作中重启。
- 独立公开镜像 `stocksignalforge:public-20261010` 构建成功；在无网络、无密钥、自动任务关闭的临时容器启动后，`/health`、`/market-data/status`、`/v-swing/status`、首页及其3个入口资源、Call计划只读接口均200，波段保持默认关闭。该容器已结束，不替换线上镜像。
- 对1534份候选公开文件检查10个本地私有值、密钥前缀、带认证URL、私人绝对路径和临时隧道地址，真实命中0；仅两个精确匹配的既有合成测试字串作为例外，不跳过整文件。暂存差异没有数据库、Cookies、运行目录、私人标注或个人交接文档。

## 2026-10-09 增量发布验证

- 本次以GitHub已有公开版本为基础增量同步，不推送生产目录的原始Git历史。保留已发布截图、上游许可、空配置模板和私有文件排除规则。
- 主README/中文README、About简介、页签与紧凑品牌统一；10月8日已完成的聚源研究证据、新闻补充、日批恢复、前向验证和参数研究在10月9日发布说明中逐项列出。
- 公开版无默认MCP服务地址；必须填写私有HTTPS地址及独立token，地址含认证信息/查询参数/片段时在发请求前拒绝。特性开关默认关闭。训练数据路径为私有占位，`training_enabled=false`、`auto_activate=false`；数据自行准备后再启用。
- 保留原SQLite文件名与环境变量，不因品牌改名切换数据库。运行数据、校准曲线、训练样本、供应商返回样本、浏览器Cookies和个人交接日志不发布。
- 对1493份候选公开文件检查敏感文件路径、10个本地私有值与token/认证URL模式，无真实命中；扫描不打印秘密值。虚构的`.invalid`测试URL及上游`xxxxx`隧道示例为明确例外，不跳过整文件。最终提交前重复检查。
- 本次公开版在禁网独立容器中：178项聚源/数据库/预期/新闻/DeepSeek缓存/参数与日批测试、92项账户统计/因子/日期完整性测试通过，合计270项、另17个子测试。公开配置/品牌/数据库兼容/CI作用域4项回归也通过，合计 **274项定向测试**。前端TypeScript/Vite生产构建通过。不是全仓套件通过声明。
- 测试起初遇到显式VIX开关污染和只读临时数据库/runs路径问题，使用独立临时数据库与tmpfs修正隔离后重跑通过；没有挂载生产数据库或请求真实服务。公开VIX测试也明确隔离开关，不依赖操作者配置。
- 前端仍有图表chunk大小和Browserslist过期提示；后端保留已有FastAPI生命周期/Pydantic字段弃用提示。不是构建失败，本次未做无关依赖升级。
- 未重启生产Docker、触发扫描/重新校准/真实模型分析或付费请求。GitHub完整CI以远端执行结果为准，下文首次全量CI历史记录保留，不冒称全部已修复。
- 首次增量提交的远端CI没有执行任何job：既有工作流job级env引用`runner.temp`被GitHub判无效。后续将该变量移入测试step的env并增加回归，恢复工作流执行；未删除、忽略或改成允许失败来掩盖测试问题。

### 全量 GitHub CI · 2026-10-09

提交`53c4708`的 [真实CI](https://github.com/JohnCharlesFields/StockSignalForge/actions/runs/37870287664) 已通过依赖安装和语法检查，测试 **2703项通过、6项失败、2项跳过，另20个子测试通过**。该远端job因测试失败未运行后面的前端步骤，本地公开版前端构建已另行通过。

尚待处理的测试如下，不能将这些失败藏进“验证通过”声明：

- `test_chart_fallback_unittest.py::ChartFallbackTests::test_daily_chart_uses_existing_history_without_leaking_key`：预期`massive_http_429`诊断字段为空。
- `test_global_equity_engine.py::TestSlippage::test_us_slippage_rate`：引擎滑点100.03与测试预期100.05不一致，需要核对统一成本契约，不为过测试改变成本。
- `test_mcp_client_adapter.py::test_build_mcp_tool_wrappers_retries_transient_discovery_failure`：当前SDK的`MCPError`构造需要message参数。
- `test_peer_earnings_signal_service.py::test_calendar_override_and_history_archive_are_auditable`：历史归档返回空。
- `test_peer_earnings_signal_service.py::test_peer_earnings_history_summary_tracks_realized_followups`：历史跟进统计为空。
- `test_pre_earnings_signal.py::TestEligibility::test_eligible_when_all_conditions_met`：资格条件未通过。

以上需分别复现并核查，不宣称已全部修复。本次没有重写策略或屏蔽这些测试来让CI变绿。

## 本地配置

真实 `.env`、MCP 私有配置、API key、OAuth 凭据、浏览器 cookies、数据库、缓存和研究会话均已加入 `.gitignore`。仓库只提供空配置与示例，不包含服务授权。

恒生聚源（Glidata）MCP 的地址和 token 分别通过 `GILDATA_MCP_URL` 与 `GILDATA_MCP_TOKEN` 配置，详见 [接入说明](GILDATA_MCP.md)。

## 测试

`agent/tests/factors/fixtures/goldens/*.csv` 是固定随机种子生成的因子测试样例。移动端持仓截图使用演示数据。

五组因子 golden 样例与恒生聚源（Glidata）MCP 配置的定向验证共 35 项，全部通过；前端生产构建通过。

首次全量 CI 为 2587 项通过、35 项失败。随后补齐了合成 CSV 与 Databento 依赖，并修正了 CI 缓存目录。行情适配、历史记录、滑点和 MCP SDK 兼容性等测试仍需核对，全量测试尚未全部通过。
