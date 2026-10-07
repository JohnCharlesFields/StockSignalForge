# GilData MCP 接入说明

StockSignalForge 是基于 GilData MCP 研究数据接入打造的 AI 股票研究工作台。GilData（恒生聚源）提供外部金融数据服务，本仓库公开的是适配、规范化与研究应用代码。

## 接入路径

```mermaid
flowchart LR
    A[本地 MCP 地址与 token] --> B[GilData MCP · FinQuery]
    B --> C[结构化表格解析]
    C --> D[股票／日期／币种与口径校验]
    D --> E[规范化研究缓存]
    E --> F[个股研究补充上下文]
    G[其他行情与事件来源] --> H[信号扫描与图表]
    H --> I[回测与结果复核]
```

实现入口是 agent/gildata_shadow_service.py；显式探测脚本为 agent/scripts/probe_gildata_shadow.py。当前支持规范化的研究字段包括市值、估值、目标价与评级样本，以及按日期读取的可选 VIX 缓存。具体可用字段取决于服务返回内容。

## 配置

从 agent/.env.example 复制生成本地 agent/.env，并填入自己的配置：

```dotenv
GILDATA_MCP_URL=
GILDATA_MCP_TOKEN=
# 仅在需要时启用按日期匹配的缓存 VIX 回退
GILDATA_VIX_FALLBACK=0
```

MCP 地址须为 HTTPS。服务地址、token 和授权请通过 [恒生聚源数据地图](https://www.gildata.com/products/datamap) 自行获取。公开版本没有预置服务地址或 token，不应在 GitHub 提交自己的 agent/.env、带认证的 URL 或 MCP 客户端私有配置。

在后端 Python 环境中，从仓库根目录显式获取小样本：

```bash
python agent/scripts/probe_gildata_shadow.py --as-of YYYY-MM-DD --symbols AAPL MSFT
```

日期应填写真实的市场交易日。探测一次最多五只股票，会调用外部 MCP 服务；费用按自己的服务授权计。返回样本会写入运行环境的私有缓存，普通页面读取使用已验证缓存，不会自动调用该脚本。

## 来源边界

- 应用框架源自 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)，保留 MIT 许可和上游声明。
- GilData MCP 是本项目接入的外部研究数据服务，服务本身的源码和商用数据不在本仓库中。
- 信号、图表、回测和模拟还包含其他数据来源；并非每个页面或每个指标都由 GilData 提供。
- 当前 GilData 接入用于研究补充和缓存读取，规范化样本不会直接喂入历史校准模型。

有关每个页面的截图、功能与入口，请查看 [完整页面图册](SCREENSHOTS.md)。
