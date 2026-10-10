# Changelog

StockSignalForge updates appear first; older entries retain Vibe-Trading upstream history.
This project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

### Changed

### Fixed

## [2026-10-10] Experimental Swing Research and Reliability Fixes

Incremental public source sync of completed October 9–10 work. No production
database, private annotations, credentials, supplier datasets or personal logs
are included. The experimental model defaults to disabled in this public copy.

### Added
- **v-swing research**: 17 causal OHLCV features, independent buy/sell random forests, stock-group RF/logistic comparison and fixed monthly walk-forward audits. Pattern matching scores are not profitability probabilities; the board retains its existing return calibration.
- **Board/chart integration**: latest completed-session experimental buy points first, original return-calibrated probability within each group; daily buy/sell markers, frozen entry/stop/+2R target, next-bar +1R break-even and risk-position references. Existing liquidity/oversold filters and chart indicators remain.
- **Durable lifecycle**: additive SQLite signal/adaptation tables, conflict and concentration observations retained, at most five open research buy references, bounded weekly adaptation based only on mature forward observations. Replay does not become forward evidence. Page reads do not train or download market data.
- **Company context**: industry/annual consensus integrated into the company section; dated same-industry market-cap references; clickable verified US identities and external references for other markets. On-demand supply-chain graph includes cached PE, market cap and three-month raw-price change where available.
- **News and disclosures**: one compact summary of recent news, explicitly distinguishing titles vs supplied summaries; official-document relationship extraction reuses DeepSeek with source quotations. Listing identity, source support and business-relation verification remain separate.
- **Long-call scenario research**: manual 60–90 DTE contract comparison, cached snapshots, bid/ask qualification, fees/slippage, earnings-window checks and explicit per-request cost cap. Scenarios do not claim optimal exit dates, EV or option profitability probabilities. Cboe's non-seven active-stock research list expands to 20, not a whole-US liquidity ranking.
- Two user-supplied screenshots: experimental swing chart and dated company-network references. Full methodology and rollback switches are documented in `wiki/V_SWING_RESEARCH_ZH.md` and `wiki/CALL_PLAN_RESEARCH_ZH.md`.

### Fixed
- **Slow stock search / missing Chinese names**: search no longer refreshes the directory synchronously or acquires the shared writer lock. Local SQLite prefix/alias indexes, background maintenance, request cancellation and short-lived frontend cache support names such as 亚马逊 while keeping GOOG/GOOGL and HPQ/HPE distinct.
- **Daily batch stopping on one empty cache**: reject empty/invalid history indexes before timestamp comparison and isolate each symbol's planning failure. GilData gap repair still requires matching consecutive historical anchors, currency, OHLC bounds and volume units; no silent historical overwrites.
- **Premature paid downloads / opaque batch failure**: check dataset publication before estimating/downloading, distinguish `waiting_data` from scan failure, retain dated partial-pool results and resume eligible work. Ambiguous download costs stay reserved; unavailable data is not stamped as fresh.
- **Wrong news protagonist / missing metadata**: product promotions at a retailer no longer automatically become retailer earnings news (Mac on Amazon → Apple primary, Amazon secondary). Bounded background enrichment fills available dated quote/sector metadata without inventing missing ATR, original links or opening impact.
- **Empty company relationships**: differentiate background updating, fetch failure and no disclosed information; retry company data independently of price scans. Generic supplier/customer categories do not masquerade as companies, and a FactSet/SIC industry match is not automatically a direct competitor.
- **Creator radar stuck on old videos**: discover all configured channel metadata before bounded transcript/LLM jobs, restore seven default creators, add subprocess timeout/progress/cooldown, and use a compatible yt-dlp/EJS/Node runtime. Missing subtitles or login checks remain visible; titles are not used to fabricate video opinions.
- **Chart / ranking errors**: include experimental target lines in the vertical domain, prevent stale signals from being called today's buy point, retain all conflict/concentration triggers and sort the full cached board before applying the display limit. Cached daily charts do not make unnecessary external calls.
- **Option data boundaries**: use the exchange calendar for delayed quote windows; retries count against the budget; missing OI/volume remain unknown rather than zero.

### Verification and Limits
- Production-side targeted swing regressions passed 69 tests and TS/Vite/Docker builds; this is not a whole-repository CI claim. Public-copy results are recorded separately in `PUBLIC_RELEASE.md`.
- Pattern-recognition audits are not independent proof of profitability. Profit-calibration samples remain insufficient; hindsight labels, surviving stocks, unverified historical corporate actions and provider availability remain limitations.
- The six failures from the October 9 full CI are kept in the public audit record. The cached-chart fixture was corrected in this update; other failures are not silently ignored or declared resolved.
- No broker integration, automatic orders, production restart or paid download is performed by this publication.

## [2026-10-09] Public Source Sync and Research Evidence

This release publishes completed October 8 changes and October 9 branding/docs.

### Added
- Dated GilData company/industry references, EPS/revenue revisions and dispersion, cache-only DeepSeek evidence, and additive SQLite evidence archives.
- Bounded optional reference/research background refresh and supplemental Chinese news within the existing 15-minute workflow. Unverified summaries remain unreviewed; links and opening-return estimates are not fabricated.
- Immutable first predictions, evaluation-version-separated forward reconciliation, time-purged parameter experiments, parameter registry, candidate reports and promotion gates. Public defaults disable automatic training and activation; private historical datasets are not distributed.
- Run artifact reading, backtest summaries, context budgets, and regression tests for provider recovery, freshness, evidence provenance and statistical boundaries.

### Changed
- Unified project title: 金融机构开发者 × 聚源数据地图 MCP：开源 AI 量化与投研系统。 Retained StockSignalForge as repository/compact interface name and upstream attribution.
- Explicit private HTTPS MCP endpoint configuration remains required; endpoints containing authentication/query information are rejected before network access.
- Clarified research probabilities vs forward results, data dates vs collection times, annual vs quarterly estimates, and incomplete source provenance.

### Fixed
- Move CI runner-temporary cache configuration from job environment to step environment; the previous workflow was rejected before any test job could start. Keep full-suite execution rather than hiding test failures.
- Respect provider Retry-After and count retry requests against quota; retain ambiguous cost reservations while releasing confirmed pre-download failures.
- Reject old board dates for new prediction logging; align future daily gap repair to raw-price cache basis without claiming old caches are audited.
- Prevent partial/late reference refreshes overwriting valid newer evidence; preserve news source links and feedback.
- Correct negative-CI interpretation and historical baseline timing; preserve old settled results rather than cosmetically rewriting history.
- Account initial-cost drawdown, degenerate backtest ratio handling, and Qlib rolling directional counts.

### Verification and Limits
- October 8 evidence integration passed 119 related main tests; this is not a full-suite claim. Public-copy verification is recorded in PUBLIC_RELEASE.md.
- Parameter candidates did not outperform the fixed baseline and were not activated. Historical replay was previously examined; no clean out-of-sample profitability or improved win rate is asserted.
- API keys, private MCP settings/endpoints, cookies, databases, cache, reports, personal handoff logs and supplier datasets remain excluded. Existing screenshot galleries are historical illustrations, not proof of latest strategy performance.

## [0.1.8] — 2026-05-17

### Added — Alpha Zoo (450+ pre-built quant alphas)
- `agent/src/factors/` — base operators (`rank`, `scale`, `ts_*`, `delta`,
  `decay_linear`, `signed_power`, `safe_div`, market-aware `vwap`) and a
  registry that AST-extracts metadata from each alpha module without
  importing it. Lookahead is enforced at the operator level
  (`delta(d>=1)`), and registry sanity checks reject `+/-inf` and
  outputs that are more than 95 % NaN.
- 4 zoos shipping 452 alphas total:
  - **qlib158** (154 alphas) — port of Microsoft Qlib's `Alpha158`
    feature handler under Apache-2.0, with pinned commit SHA per file.
  - **alpha101** (101 alphas) — implementation of Kakushadze (2015)
    *"101 Formulaic Alphas"* (arXiv:1601.00991), written from the paper
    appendix; the relevant trademarked string is intentionally absent.
  - **gtja191** (191 alphas) — implementation of Guotai Junan's 2014
    *"191 Short-period Trading Alpha Factors"* research report.
  - **academic** (6 factors) — Fama-French 5 + Carhart momentum, shipped
    as honest price-based proxies (not the canonical FF series).
- `vibe-trading alpha {list,show,bench,compare,export-manifest}` CLI
  subcommand. `show` and `export-manifest` enforce path-traversal guards.
- New agent tools: `AlphaZooTool` (browse) and `AlphaBenchTool`
  (orchestrator with Jinja2 autoescape + strict CSP HTML report).
- `ZooSignalEngine.from_zoo(...)` — composite multi-factor signal engine
  with cross-sectional standardisation, weighting, and optional top-N /
  bottom-N long-short conversion.
- `wiki/scripts/build_alpha_library.py` — Alpha Library renderer.
  Reads `manifest.json` produced by `vibe-trading alpha export-manifest`
  and emits 452 per-alpha HTML pages plus 4 per-zoo overviews, each with
  `script-src 'none'` CSP. The landing page hydrates per-zoo counts
  from `content/index.json`.
- New blog post: *"Which of the 191 GTJA alphas still work in 2026?"*
  with aggregate IC statistics, theme breakdown, and the top alphas
  that survive eight years of out-of-sample data.

### Added — Web UI for Alpha Zoo
- New page at `/alpha-zoo` in the Vite + React frontend with three
  views: browse (4 zoo cards + filter bar + paginated table), detail
  (formula, metadata, collapsible source code), and bench-runner
  (form → SSE-streamed progress + Alive/Reversed/Dead stat cards +
  Top-5-by-IR table + by-theme breakdown chart). "Alpha Zoo" nav
  entry added to the layout.
- Four new REST routes in the FastAPI server:
  - `GET /alpha/list` — filterable alpha catalogue
  - `GET /alpha/{alpha_id}` — meta + source code
  - `POST /alpha/bench` — kicks off a background bench job and
    returns a `job_id`
  - `GET /alpha/bench/{job_id}/stream` — Server-Sent Events with
    `progress`, `result`, `done`, and `error` event types. In-memory
    job state with a 1-hour TTL; no Redis/Celery dependency.
- Bench math is refactored into `agent/src/factors/bench_runner.py`
  so the CLI driver (`agent/scripts/w4a_run_benches.py`) and the new
  API worker share a single implementation.

### Added — Safety floor
- `agent/tests/factors/test_alpha_purity.py` — AST allowlist scan over
  every `zoo/**/*.py` module (whitelist: pandas, numpy, scipy.\*,
  `src.factors.base`, `__future__`, `typing`, `math`, `dataclasses`;
  banned: `os`, `sys`, `subprocess`, `socket`, `urllib`, `requests`,
  `httpx`, `pathlib`, `Path`, `open`, `eval`, `exec`, `compile`,
  `__import__`, and `getattr(_, "__*")`).
- `agent/tests/factors/test_lookahead.py` — sentinel future-row
  injection on a 300-row synthetic panel; corrupting rows after the
  probe must leave the probe value unchanged within 1e-9.
- `tools/ci_grep_gates.sh` — CI gate that rejects `yaml.load(` without
  `safe_load`, any trademarked-name leak in shipped artifacts, and any
  per-stock-code data leak in `wiki/**/*.{json,csv,html}`.
- `agent/tests/factors/conftest.py` — opt-in `pytest-socket` integration
  that hard-fails any test attempting outbound network during the
  factors test suite.

### Added — Community governance
- `CONTRIBUTING.md` — Developer Certificate of Origin sign-off
  requirement and a contributor checklist for new alpha PRs (purity,
  lookahead, `__alpha_meta__` shape, LaTeX-matches-code, per-zoo
  LICENSE.md, DCO).
- `NOTICE` (repo root) — Apache-2.0 attribution for Qlib and a
  declaration that the bundled formulas from Kakushadze, GTJA, and the
  academic baselines are mathematical content (paper prose, tables, and
  figures are not reproduced here).
- Per-zoo `LICENSE.md` for each of `qlib158/`, `alpha101/`, `gtja191/`,
  and `academic/`, plus an upstream `NOTICE` for `qlib158/`.

### Changed
- `agent/src/tools/factor_analysis_tool.py` extracted its IC/IR and
  layered-backtest helpers to `agent/src/factors/factor_analysis_core.py`
  so the new `alpha_bench_tool` reuses the same maths. Public tool
  signature is unchanged; `_compute_ic_series` and `_compute_group_equity`
  remain importable as backward-compatible aliases.
- `agent/cli.py` grew by 7 lines to register the `alpha` subcommand;
  all handler logic lives in `agent/src/factors/cli_handlers.py`.
- Packaging: `pyproject.toml` now ships `zoo/**/*.yaml`, `zoo/**/*.md`,
  and `zoo/**/NOTICE` as package data; `MANIFEST.in` recursively
  includes `agent/src/factors`.

### Known limitations
- The `btc-usdt` universe is single-asset; cross-sectional IC requires
  ≥2 instruments, so the bundled `alpha101_btc` bench run returns
  alive/reversed/dead = 0/0/0 by construction. Use a multi-symbol crypto
  basket (e.g. BTC + ETH + SOL + the top-N perpetuals) for meaningful
  cross-sectional results; a curated `crypto-majors` universe is planned
  for 0.2.

### Internal
- `wiki/alpha-library/manifest.json` and `wiki/alpha-library/content/`
  are generated artifacts and gitignored. Run
  `vibe-trading alpha export-manifest --out wiki/alpha-library/manifest.json
  --force` followed by `python wiki/scripts/build_alpha_library.py` to
  regenerate the static site.
