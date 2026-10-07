# 配置与测试说明

## 本地配置

真实 `.env`、MCP 私有配置、API key、OAuth 凭据、浏览器 cookies、数据库、缓存和研究会话均已加入 `.gitignore`。仓库只提供空配置与示例，不包含服务授权。

恒生聚源（Glidata）MCP 的地址和 token 分别通过 `GILDATA_MCP_URL` 与 `GILDATA_MCP_TOKEN` 配置，详见 [接入说明](GILDATA_MCP.md)。

## 测试

`agent/tests/factors/fixtures/goldens/*.csv` 是固定随机种子生成的因子测试样例。移动端持仓截图使用演示数据。

五组因子 golden 样例与恒生聚源（Glidata）MCP 配置的定向验证共 35 项，全部通过；前端生产构建通过。

首次全量 CI 为 2587 项通过、35 项失败。随后补齐了合成 CSV 与 Databento 依赖，并修正了 CI 缓存目录。行情适配、历史记录、滑点和 MCP SDK 兼容性等测试仍需核对，全量测试尚未全部通过。
