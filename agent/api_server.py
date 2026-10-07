#!/usr/bin/env python3
"""easymoneysniper API Server - RESTful API for finance research and backtesting.

V5: ReAct Agent + async /run + CORS env + SSE tool events.
"""

from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
import hashlib
import html
import hmac
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import traceback
import urllib.request
import time
import csv
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist, stdev
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Security, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from rich.console import Console

from src.ui_services import build_run_analysis, load_run_context
from daily_stock_adapter import get_analysis as get_daily_stock_analysis
from daily_stock_adapter import recent_symbols as get_daily_stock_recent_symbols
from daily_stock_adapter import search_symbols as search_daily_stock_symbols
from research_universe_service import (
    RESEARCH_UNIVERSE_CATALOG,
    list_research_universe_groups,
    list_research_universes,
    resolve_research_universe,
)
from research_signal_sorting import (
    research_context_sort_key as _shared_research_context_sort_key,
    win_rate_sort_metrics as _shared_win_rate_sort_metrics,
)
from signal_dashboard_page import signal_dashboard_html
from macro_panic_service import get_vix_regime, panic_evidence_for_opportunity
from portfolio_timing_service import portfolio_timing_gate
from stock_panic_service import stock_panic_evidence, stock_panic_proxy
from launch_signal_service import scan_launch_signals
from batch_runtime import check_scan_budget as _check_scan_budget, watch_process
from technical_signal_metrics import compute_daily_tunnel_score
from peer_earnings_signal_service import PEER_MATCH_VERSION, list_peer_groups, load_peer_earnings_history, peer_catalog_summary, summarize_peer_earnings_history
from stock_signal_candidate_service import (
    STOCK_SIGNAL_BULLISH_STRATEGIES as _STOCK_SIGNAL_BULLISH_STRATEGIES,
    stock_signal_candidate_pool as _shared_stock_signal_candidate_pool,
    stock_signal_item as _shared_stock_signal_item,
)
from trade_risk_rules import (
    BLOCK_OPEN,
    evaluate_exit_risk,
    validate_open_risk,
)
from deepseek_decision_assistant import get_cached_review, review_top_contexts
from overnight_alpha_service import validate_overnight_alpha_summary
from overnight_alpha_service import validate_symbol_overnight_alpha, benchmark_for_universes
from market_data_service import data_source_status, external_data_allowed, external_data_scope, get_daily_history, get_databento_options_snapshot, get_massive_options_snapshot, get_next_earnings
import soxl_quant_service
from creator_opinion_service import (
    creator_opinion_tags_for_symbol,
    list_channels as list_creator_channels,
    list_opinion_feed,
    list_sector_signals as list_creator_sector_signals,
    refresh_creator_opinions,
    save_sector_signals_from_opinions,
    seed_default_channels,
)
from premarket_news_service import (
    google_finance_source_audit,
    list_premarket_news,
    mark_premarket_news_feedback,
    refresh_premarket_news,
)
from app_database import (
    cache_get,
    cache_set,
    database_status,
    ensure_database,
    latest_home_dashboard_snapshot,
    portfolio_account_get,
    portfolio_account_set,
    portfolio_holding_delete,
    portfolio_holding_upsert,
    save_home_dashboard_snapshot,
    signal_calibration_list,
    signal_events_count,
    symbol_directory_count,
    symbol_directory_search,
    symbol_directory_upsert_many,
    user_watchlist_delete,
    user_watchlist_get,
    user_watchlist_list,
    user_watchlist_set_enabled,
    user_watchlist_upsert,
)

# UTF-8 on Windows
import sys as _sys
for _s in ("stdout", "stderr"):
    _r = getattr(getattr(_sys, _s, None), "reconfigure", None)
    if callable(_r):
        _r(encoding="utf-8", errors="replace")

RUNS_DIR = Path(__file__).resolve().parent / "runs"
SESSIONS_DIR = Path(__file__).resolve().parent / "sessions"
UPLOADS_DIR = Path(__file__).resolve().parent / "uploads"
AGENT_DIR = Path(__file__).resolve().parent
ENV_PATH = AGENT_DIR / ".env"
ENV_EXAMPLE_PATH = AGENT_DIR / ".env.example"

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MB

# Rich console for colored logs
console = Console()


# ============================================================================
# Pydantic Models
# ============================================================================

class Artifact(BaseModel):
    """Artifact file metadata."""
    name: str = Field(..., description="File name")
    path: str = Field(..., description="File path")
    type: str = Field(..., description="File type: csv, json, txt, etc.")
    size: int = Field(..., description="Size in bytes")
    exists: bool = Field(..., description="Whether the file exists")


class BacktestMetrics(BaseModel):
    """Backtest summary metrics."""
    model_config = {"extra": "allow"}

    final_value: float = Field(..., description="Ending portfolio value")
    total_return: float = Field(..., description="Total return")
    annual_return: float = Field(..., description="Annualized return")
    max_drawdown: float = Field(..., description="Max drawdown")
    sharpe: float = Field(..., description="Sharpe ratio")
    win_rate: float = Field(..., description="Win rate")
    trade_count: int = Field(..., description="Number of trades")



class RAGSelection(BaseModel):
    """RAG routing result."""
    selected_api: str = Field(..., description="Selected API code")
    selected_name: str = Field(..., description="Selected API name")
    selected_score: float = Field(..., description="Match score")


class RunInfo(BaseModel):
    """Compact run row for list views."""
    run_id: str
    status: str
    created_at: str
    prompt: Optional[str] = None
    total_return: Optional[float] = None
    sharpe: Optional[float] = None
    codes: List[str] = Field(default_factory=list)
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class RunResponse(BaseModel):
    """API response payload for a single run."""

    status: str = Field(..., description="Run status: success, failed, aborted")
    run_id: str = Field(..., description="Run identifier")
    elapsed_seconds: float = Field(..., description="Execution time in seconds")
    reason: Optional[str] = Field(None, description="Failure reason when available")

    planner_output: Optional[Dict[str, Any]] = Field(None, description="Planner output")
    strategy_spec: Optional[Dict[str, Any]] = Field(None, description="Strategy specification")
    rag_selection: Optional[RAGSelection] = Field(None, description="Selected RAG metadata")

    metrics: Optional[BacktestMetrics] = Field(None, description="Backtest metrics")
    artifacts: List[Artifact] = Field(default_factory=list, description="Run artifacts")
    run_card: Optional[Dict[str, Any]] = Field(None, description="Trust Layer run card payload")

    equity_curve: Optional[List[Dict[str, Any]]] = Field(None, description="Equity preview")
    trade_log: Optional[List[Dict[str, Any]]] = Field(None, description="Trade preview")

    artifacts_equity_csv: Optional[List[Dict[str, Any]]] = Field(None, description="Full equity rows")
    artifacts_metrics_csv: Optional[List[Dict[str, Any]]] = Field(None, description="Full metrics rows")
    artifacts_trades_csv: Optional[List[Dict[str, Any]]] = Field(None, description="Full trade rows")
    validation: Optional[Dict[str, Any]] = Field(None, description="Statistical validation results")

    run_directory: str = Field(..., description="Run directory path")
    run_stage: Optional[str] = Field(None, description="UI-facing run stage")
    run_context: Optional[Dict[str, Any]] = Field(None, description="Normalized request context")
    generated_files: List[Artifact] = Field(default_factory=list, description="Generated run files outside standard artifacts")
    file_previews: Dict[str, str] = Field(default_factory=dict, description="Preview text for generated run files")
    price_series: Optional[Dict[str, List[Dict[str, Any]]]] = Field(None, description="Grouped OHLC series")
    indicator_series: Optional[Dict[str, Dict[str, List[Dict[str, Any]]]]] = Field(
        None,
        description="Grouped indicator overlays",
    )
    trade_markers: Optional[List[Dict[str, Any]]] = Field(None, description="Trade markers for charts")
    run_logs: Optional[List[Dict[str, Any]]] = Field(None, description="Structured stdout/stderr lines")


class EventDrivenScanRequest(BaseModel):
    """Request for event-driven launch signal scanning."""

    universe: str = Field("spx", description="Shared research universe name.")
    tickers: Optional[List[str]] = Field(
        None,
        description="Optional explicit ticker list. Empty means the selected shared universe.",
    )
    limit: int = Field(0, ge=0, le=1000, description="Limit selected universe size for testing.")
    top: int = Field(30, ge=1, le=100, description="Number of candidates to return.")
    news_per_ticker: int = Field(5, ge=1, le=20, description="Yahoo RSS items per ticker.")
    lookback_hours: int = Field(96, ge=1, le=720, description="News lookback window.")
    min_relevance: float = Field(0.45, ge=0.0, le=1.0, description="Minimum news relevance score.")
    cache_ttl_minutes: int = Field(30, ge=0, le=1440, description="RSS cache TTL. 0 disables cache.")
    sleep_seconds: float = Field(0.2, ge=0.0, le=5.0, description="Delay between ticker RSS requests.")
    workers: int = Field(8, ge=1, le=32, description="Concurrent RSS fetch workers.")
    include_prices: bool = Field(True, description="Whether to fetch yfinance price confirmation.")
    use_llm_scoring: bool = Field(False, description="Use configured DeepSeek/OpenAI-compatible LLM to refine event scoring.")
    calibration_run_id: Optional[str] = Field(None, description="Run id containing artifacts/event_impact_summary.json.")
    calibration_json: Optional[str] = Field(None, description="Optional calibration json path under agent directory.")
    auto_calibration: bool = Field(False, description="Automatically use the latest usable event-impact calibration.")
    min_calibration_events: int = Field(20, ge=1, le=10000, description="Minimum historical events required for auto calibration.")
    timeout_seconds: int = Field(900, ge=30, le=3600, description="Scanner subprocess timeout.")


class EventDrivenScanResponse(BaseModel):
    """Response payload for an event-driven scan run."""

    status: str
    run_id: str
    elapsed_seconds: float
    event_count: int
    signal_count: int
    signals: List[Dict[str, Any]]
    report_markdown: Optional[str] = None
    run_directory: str
    artifacts: List[Artifact] = Field(default_factory=list)
    report_pdf_url: str
    detail_url: str
    calibration: Optional[Dict[str, Any]] = Field(None, description="Selected calibration metadata when used.")


class EventCalibrationInfo(BaseModel):
    """Historical event calibration summary."""

    run_id: str
    summary_path: str
    event_count: int
    usable: bool
    primary_horizon: Optional[str] = None
    primary_beta: Optional[float] = None
    primary_ic: Optional[float] = None
    updated_at: str


class EventCalibrationListResponse(BaseModel):
    """Available event-driven calibration models."""

    calibrations: List[EventCalibrationInfo]


class LaunchSignalScanRequest(BaseModel):
    """Request for the shared-universe technical launch scanner."""

    universe: str = Field("spx", description="Shared research universe name.")
    symbols: Optional[List[str]] = Field(None, description="Optional explicit ticker list.")
    model_path: Optional[str] = Field(None, description="Reserved for a restored calibrated model.")
    threshold: float = Field(0.5, ge=0.1, le=0.95)
    top: int = Field(30, ge=1, le=200)
    use_stock_observation_pool: bool = Field(True, description="Scan the latest stock-signal observation pool first.")
    scan_mode: str = Field("technical", description="technical, peer_earnings, or combined.")
    peer_group: str = Field("all", description="Peer earnings group id, or all.")


class StockSignalUpdateRequest(BaseModel):
    """Request a fresh options-evidence report for one shared research universe."""

    universe: str = Field("spx", description="Shared research universe name.")


class ResearchSignalHubRunRequest(BaseModel):
    """Run the three-layer equity research workflow for one shared universe."""

    universe: str = Field("spx", description="Shared research universe name.")
    refresh_evidence: bool = Field(True, description="Refresh first-layer evidence before timing analysis.")
    top: int = Field(100, ge=1, le=200)
    enable_llm_review: bool = Field(True, description="Run DeepSeek decision-assistance review for top candidates.")
    llm_review_top: int = Field(10, ge=0, le=30, description="Number of top decision cards to review with DeepSeek.")
    enable_event_llm_review: bool = Field(True, description="Use platform DeepSeek semantic scoring when refreshing event radar evidence.")
    reuse_session_results: bool = Field(False, description="Reuse same-session ticker screening results across universes.")
    option_chain_cache_minutes: Optional[float] = Field(None, ge=0, le=1440, description="Optional research-run option-chain cache age limit.")


class OvernightAlphaRow(BaseModel):
    symbol: str
    source_universe_ids: List[str] = Field(default_factory=list)
    source_pools: List[str] = Field(default_factory=list)
    pullback_rejection_status: Optional[str] = None
    pullback_confirmation_status: Optional[str] = None


class OvernightAlphaSummaryRequest(BaseModel):
    rows: List[OvernightAlphaRow] = Field(default_factory=list)
    period: str = Field("2y", max_length=16)
    min_edge: float = Field(0.001, ge=0.0, le=0.05)
    limit: int = Field(12, ge=1, le=30)


class HomeDashboardSnapshotRequest(BaseModel):
    payload: Dict[str, Any] = Field(default_factory=dict)
    source: str = Field("home", max_length=64)


class DailyThreeLayerAutoRunRequest(BaseModel):
    force: bool = Field(False, description="Run even if today's auto scan already completed.")
    universes: Optional[List[str]] = Field(None, description="Optional explicit universe ids.")
    top: int = Field(100, ge=1, le=200)
    llm_review_top: int = Field(10, ge=0, le=30)
    refresh_evidence: bool = Field(True)


class WatchlistItemRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    name: str = Field("", max_length=200)
    note: str = Field("", max_length=1000)
    enabled: bool = Field(True)


class WatchlistBatchRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list)
    note: str = Field("", max_length=1000)
    enabled: bool = Field(True)


class WatchlistEnabledRequest(BaseModel):
    enabled: bool = Field(True)


class CreatorOpinionRefreshRequest(BaseModel):
    """Refresh creator video metadata, transcripts, opinions and sector signals."""

    handles: Optional[List[str]] = Field(None, description="Optional creator handles. Empty means all enabled defaults.")
    limit_per_channel: int = Field(1, ge=1, le=5)
    use_llm: bool = Field(True, description="Use configured DeepSeek/OpenAI-compatible LLM when transcript is available.")
    force: bool = Field(False, description="Re-fetch transcript and re-run opinion extraction even if cached.")


class PremarketNewsRefreshRequest(BaseModel):
    """Refresh the premarket human-review news queue."""

    universes: Optional[List[str]] = Field(None, description="Research universes. Empty means all configured pools.")
    start_utc: Optional[str] = Field(None, description="Optional ISO UTC start. Default: previous regular US close.")
    end_utc: Optional[str] = Field(None, description="Optional ISO UTC end. Default: now.")
    translate_top: int = Field(80, ge=0, le=200, description="Translate top-ranked display items.")
    max_pages: int = Field(3, ge=1, le=10, description="Massive/Polygon news pages to scan.")
    refresh_universe: bool = Field(False, description="Refresh universe constituents before scanning.")


class PremarketNewsFeedbackRequest(BaseModel):
    """Human feedback on one premarket news item."""

    decision: str = Field(..., description="important, not_important, or skip.")
    note: str = Field("", max_length=1000)
    weight_multiplier: Optional[float] = Field(None, ge=0.0, le=5.0)
    add_to_watchlist: Optional[bool] = Field(None, description="Default: true for important.")


class PortfolioHoldingRequest(BaseModel):
    """Upsert one manually-tracked holding (keyed by symbol)."""

    symbol: str = Field(..., min_length=1, max_length=32, description="Ticker, e.g. AAPL.")
    shares: float = Field(..., ge=0, description="Number of shares held.")
    avg_cost: float = Field(..., ge=0, description="Average cost per share.")
    note: str = Field("", max_length=500, description="Optional note.")


class PortfolioAccountRequest(BaseModel):
    """Set account-level available cash."""

    available_cash: float = Field(..., ge=0, description="Investable cash on hand.")
    currency: str = Field("USD", max_length=8)


class ResearchChainRunRequest(BaseModel):
    """Launch a five-layer research chain for a ticker or theme."""

    symbol: str = Field(..., min_length=1, max_length=32, description="Ticker or target symbol.")
    universe: str = Field("spx", max_length=128, description="Source research universe.")
    market: str = Field("US equities", max_length=128, description="Market description.")
    goal: Optional[str] = Field(None, max_length=2000, description="Optional upstream context and research goal.")
    preset_name: str = Field("five_layer_research_chain", max_length=128, description="Swarm preset to run.")


class EventImpactValidationRequest(BaseModel):
    """Request for validating event scores against forward returns."""

    run_id: Optional[str] = Field(None, description="Existing run id containing data/events.csv.")
    events_csv: Optional[str] = Field(None, description="Optional explicit event CSV path under agent directory.")
    benchmark: str = Field("SPY", description="Benchmark ticker for abnormal returns.")
    horizons: str = Field("1,3,5,10", description="Forward return horizons in trading days.")
    timeout_seconds: int = Field(900, ge=30, le=3600, description="Validator subprocess timeout.")


class HistoricalEventCollectRequest(BaseModel):
    """Collect historical events through GDELT for quantitative validation."""

    tickers: Optional[List[str]] = Field(None, description="Optional explicit ticker list.")
    limit: int = Field(0, ge=0, le=505, description="Limit S&P 500 universe size.")
    start_date: str = Field(..., description="Historical news start date, YYYY-MM-DD.")
    end_date: str = Field(..., description="Historical news end date, YYYY-MM-DD.")
    max_records_per_ticker: int = Field(20, ge=1, le=100, description="GDELT articles per ticker.")
    min_relevance: float = Field(0.45, ge=0.0, le=1.0)
    workers: int = Field(1, ge=1, le=16)
    sleep_seconds: float = Field(1.0, ge=0.0, le=10.0)
    cache_ttl_minutes: int = Field(1440, ge=0, le=10080)
    timeout_seconds: int = Field(1800, ge=60, le=7200)
    validate: bool = Field(True, description="Run event impact validation after collection.")


class HistoricalEventCollectResponse(BaseModel):
    """Historical event collection response."""

    status: str
    run_id: str
    elapsed_seconds: float
    event_count: int
    run_directory: str
    events_csv: str
    validation: Optional[Dict[str, Any]] = None
    artifacts: List[Artifact] = Field(default_factory=list)


class HistoricalBatchCollectRequest(BaseModel):
    """Batch/resumable GDELT historical event collection."""

    tickers: Optional[List[str]] = Field(None, description="Optional explicit ticker list.")
    limit: int = Field(0, ge=0, le=505, description="Limit S&P 500 universe size.")
    start_date: str = Field(..., description="Historical news start date, YYYY-MM-DD.")
    end_date: str = Field(..., description="Historical news end date, YYYY-MM-DD.")
    batch_size: int = Field(5, ge=1, le=50)
    max_batches: int = Field(0, ge=0, le=200, description="0 means run all batches.")
    max_records_per_ticker: int = Field(20, ge=1, le=100)
    min_relevance: float = Field(0.45, ge=0.0, le=1.0)
    workers: int = Field(1, ge=1, le=8)
    sleep_seconds: float = Field(1.0, ge=0.0, le=10.0)
    batch_sleep_seconds: float = Field(10.0, ge=0.0, le=120.0)
    cache_ttl_minutes: int = Field(10080, ge=0, le=43200)
    resume_run_id: Optional[str] = Field(None, description="Existing batch run to resume.")
    validate: bool = Field(True)
    timeout_seconds: int = Field(3600, ge=60, le=14400)


class HistoricalBatchCollectResponse(BaseModel):
    """Batch historical collection response."""

    status: str
    run_id: str
    elapsed_seconds: float
    event_count: int
    processed_this_run: int
    successful_batches: int = 0
    failed_batches: int = 0
    empty_batches: int = 0
    run_directory: str
    events_csv: str
    state_json: str
    artifacts: List[Artifact] = Field(default_factory=list)


class EventImpactValidationResponse(BaseModel):
    """Event impact validation output."""

    status: str
    run_id: str
    elapsed_seconds: float
    validated_events: int
    summary: Dict[str, Any]
    report_markdown: Optional[str] = None
    artifacts: List[Artifact] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Health check payload."""
    status: str = Field(..., description="Service status")
    service: str = Field(..., description="Service name")
    timestamp: str = Field(..., description="Server timestamp")


class LLMProviderOption(BaseModel):
    """Supported LLM provider metadata for the settings UI."""

    name: str
    label: str
    api_key_env: Optional[str] = None
    base_url_env: str
    default_model: str
    default_base_url: str
    api_key_required: bool = True
    auth_type: str = "api_key"
    login_command: Optional[str] = None


class LLMSettingsResponse(BaseModel):
    """Current LLM runtime settings."""

    provider: str
    model_name: str
    base_url: str
    api_key_env: Optional[str] = None
    api_key_configured: bool
    api_key_hint: Optional[str] = None
    api_key_required: bool
    temperature: float
    timeout_seconds: int
    max_retries: int
    reasoning_effort: str
    env_path: str
    providers: List[LLMProviderOption]


class UpdateLLMSettingsRequest(BaseModel):
    """Update LLM settings persisted to agent/.env."""

    provider: str = Field(..., min_length=1)
    model_name: str = Field(..., min_length=1)
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    clear_api_key: bool = False
    temperature: float = 0.0
    timeout_seconds: int = Field(120, ge=1, le=3600)
    max_retries: int = Field(2, ge=0, le=20)
    reasoning_effort: Optional[str] = None


class DataSourceSettingsResponse(BaseModel):
    """Current data source credential settings."""

    tushare_token_configured: bool
    tushare_token_hint: Optional[str] = None
    twelve_data_configured: bool
    fmp_configured: bool
    baostock_supported: bool
    baostock_installed: bool
    baostock_message: str
    env_path: str


class UpdateDataSourceSettingsRequest(BaseModel):
    """Update project-local data source credentials."""

    tushare_token: Optional[str] = None
    clear_tushare_token: bool = False
    twelve_data_api_key: Optional[str] = None
    clear_twelve_data_api_key: bool = False
    fmp_api_key: Optional[str] = None
    clear_fmp_api_key: bool = False


# ---- V4 Session Models ----

class CreateSessionRequest(BaseModel):
    """Create session request body."""
    title: str = Field("", description="Session title")
    config: Optional[Dict[str, Any]] = Field(None, description="Session config")


class SessionResponse(BaseModel):
    """Session record."""
    session_id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    last_attempt_id: Optional[str] = None


class SendMessageRequest(BaseModel):
    """Send chat message: natural-language strategy description."""
    content: str = Field(..., description="Natural language strategy description", min_length=1, max_length=5000)


class MessageResponse(BaseModel):
    """Stored chat message."""
    message_id: str
    session_id: str
    role: str
    content: str
    created_at: str
    linked_attempt_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None



# ============================================================================
# FastAPI Application
# ============================================================================

app = FastAPI(
    title="easymoneysniper API",
    description="easymoneysniper API: evidence-first finance research, backtesting, and swarm workflows",
    version="5.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)
try:
    _APP_DB_PATH = ensure_database()
    console.log(f"SQLite app database ready: {_APP_DB_PATH}")
except Exception as _db_exc:
    console.log(f"SQLite app database initialization failed: {_db_exc}")

_AUTO_SCAN_ENABLED = os.getenv("DAILY_THREE_LAYER_AUTO_ENABLED", "1").lower() not in {"0", "false", "no"}
_AUTO_SCAN_TIME = os.getenv("DAILY_THREE_LAYER_AUTO_TIME", "09:30")
_AUTO_SCAN_TZ = os.getenv("DAILY_THREE_LAYER_AUTO_TZ", "Asia/Shanghai")
_AUTO_SCAN_STARTUP_DELAY_SECONDS = max(0, int(os.getenv("DAILY_THREE_LAYER_AUTO_STARTUP_DELAY_SECONDS", "30") or "30"))
_AUTO_SCAN_POLL_SECONDS = max(30, int(os.getenv("DAILY_THREE_LAYER_AUTO_POLL_SECONDS", "60") or "60"))
_AUTO_SCAN_TOP = max(1, min(200, int(os.getenv("DAILY_THREE_LAYER_AUTO_TOP", "30") or "30")))
_AUTO_SCAN_LLM_REVIEW_TOP = max(0, min(30, int(os.getenv("DAILY_THREE_LAYER_AUTO_LLM_REVIEW_TOP", "3") or "3")))
_AUTO_SCAN_MAX_UNIVERSES = max(0, int(os.getenv("DAILY_THREE_LAYER_AUTO_MAX_UNIVERSES", "0") or "0"))
_AUTO_SCAN_REFRESH_EVIDENCE = os.getenv("DAILY_THREE_LAYER_AUTO_REFRESH_EVIDENCE", "0").lower() in {"1", "true", "yes"}
_AUTO_SCAN_CHILD_TIMEOUT_SECONDS = max(60, int(os.getenv("DAILY_THREE_LAYER_AUTO_CHILD_TIMEOUT_SECONDS", "1200") or "1200"))
_AUTO_SCAN_STATE_KEY = "daily_three_layer_auto:last"
_BACKFILL_STATUS_KEY = "predictions:backfill:status"

# Shared pool for fanning out the single-stock detail page's independent data
# sections concurrently. Stragglers (slow external fetches) keep running on the
# pool after the page returns, warming the cache for the next visit.
_SSO_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="sso")
_SSO_ENRICH_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sso-enrich")
_SSO_REFRESH_LOCK = threading.Lock()
_SSO_REFRESH_ACTIVE: set = set()
_SSO_REFRESH_SLOTS = threading.BoundedSemaphore(4)
_SSO_SUMMARY_ACTIVE: set = set()


def _sso_gather(tasks: Dict[str, Any], timeout: float = 14.0) -> Dict[str, Any]:
    """Run ``{name: callable}`` concurrently, return results for whatever
    finishes within ``timeout``; anything still running is marked 'loading' and
    left to complete in the background (its result caches for next time)."""
    futures = {_SSO_POOL.submit(fn): name for name, fn in tasks.items()}
    out: Dict[str, Any] = {}
    try:
        for fut in as_completed(list(futures), timeout=timeout):
            name = futures[fut]
            try:
                out[name] = fut.result()
            except Exception as exc:
                out[name] = {"available": False, "error": str(exc)[-200:]}
    except FuturesTimeout:
        pass
    for name in futures.values():
        out.setdefault(name, {"available": False, "status": "loading",
                              "note": "数据仍在后台加载，稍后刷新即可显示。"})
    return out
# Hard ceiling for the post-loop finalize (snapshot + prewarm + predictions). The
# per-universe loop has its own child timeout; without this the finalize could
# hang forever on a no-timeout external socket and freeze the whole daily scan.
_AUTO_SCAN_FINALIZE_TIMEOUT_SECONDS = max(120, int(os.getenv("DAILY_THREE_LAYER_AUTO_FINALIZE_TIMEOUT_SECONDS", "900") or "900"))
_AUTO_SCAN_LOCK = threading.RLock()
_DAILY_ACTIVE_WORKERS: Dict[str, threading.Thread] = {}
_AUTO_SCAN_JOB: Dict[str, Any] = {
    "status": "idle",
    "message": "daily auto scan has not started",
    "progress": 0.0,
    "current_universe": "",
    "started_at": None,
    "finished_at": None,
    "last_result": None,
}
_PREMARKET_NEWS_AUTO_ENABLED = os.getenv("PREMARKET_NEWS_AUTO_ENABLED", "1").lower() not in {"0", "false", "no"}
_PREMARKET_NEWS_AUTO_INTERVAL_SECONDS = max(300, int(os.getenv("PREMARKET_NEWS_AUTO_INTERVAL_SECONDS", "900") or "900"))
_PREMARKET_NEWS_AUTO_STARTUP_DELAY_SECONDS = max(5, int(os.getenv("PREMARKET_NEWS_AUTO_STARTUP_DELAY_SECONDS", "45") or "45"))
_PREMARKET_NEWS_AUTO_MAX_PAGES = max(1, min(10, int(os.getenv("PREMARKET_NEWS_AUTO_MAX_PAGES", "1") or "1")))
_PREMARKET_NEWS_AUTO_TRANSLATE_TOP = max(0, min(200, int(os.getenv("PREMARKET_NEWS_AUTO_TRANSLATE_TOP", "80") or "80")))
_PREMARKET_NEWS_AUTO_LOCK = threading.Lock()
_PREMARKET_NEWS_REFRESH_LOCK = threading.Lock()
_DAILY_NEWS_LOCK = threading.Lock()
_DAILY_EVIDENCE_LOCK = threading.Lock()
_PREMARKET_NEWS_AUTO_STARTED = False
_PREMARKET_NEWS_AUTO_STATUS: Dict[str, Any] = {
    "enabled": _PREMARKET_NEWS_AUTO_ENABLED,
    "interval_seconds": _PREMARKET_NEWS_AUTO_INTERVAL_SECONDS,
    "status": "not_started",
    "started_at": None,
    "last_run_at": None,
    "last_success_at": None,
    "last_error": "",
    "last_result": None,
}

_DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8000",
]


def _parse_cors_origins(raw: Optional[str]) -> List[str]:
    """Parse CORS origins and reject credentialed wildcard configuration.

    Args:
        raw: Comma-separated CORS origins from ``CORS_ORIGINS``. ``None`` or a
            blank value uses the loopback development defaults.

    Returns:
        Explicit CORS origins accepted by the API server.

    Raises:
        RuntimeError: If a wildcard origin is configured while credentials are
            enabled.
    """
    if raw is None or not raw.strip():
        return list(_DEFAULT_CORS_ORIGINS)
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if "*" in origins:
        raise RuntimeError(
            "CORS_ORIGINS='*' is not allowed while credentials are enabled; "
            "configure explicit Web UI origins instead."
        )
    return origins


# CORS: override with CORS_ORIGINS (comma-separated explicit origins)
_CORS_ORIGINS = _parse_cors_origins(os.getenv("CORS_ORIGINS"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------------
# SPA deep-link fallback
# ----------------------------------------------------------------------------
# A handful of API routes share their path with frontend SPA routes (e.g.
# ``/runs/{id}`` and ``/correlation``). Because FastAPI matches registered
# routes before the static SPA mount, a browser that refreshes or bookmarks
# one of these URLs would receive JSON (or 401/422) instead of the SPA shell.
# The middleware below serves ``frontend/dist/index.html`` when the request
# clearly came from a browser (``Accept`` contains ``text/html``); programmatic
# clients are routed to the real API handler as before.
#
# Patterns are written narrowly so the SPA shell only shadows paths that
# actually correspond to frontend pages. In particular ``/runs/{id}`` is
# the RunDetail page, but ``/runs/{id}/code`` and ``/runs/{id}/pine`` are
# API-only endpoints with no SPA route — using a broad ``/runs/`` prefix
# here would incorrectly hijack those when the browser sets ``Accept:
# text/html`` (e.g. a user pasting the URL into the address bar).

_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
_SPA_HTML_EXACT_PATHS: frozenset[str] = frozenset({"/correlation", "/premarket-news"})
# Each regex matches a complete request path. Trailing slash optional.
_SPA_HTML_PATH_REGEX: tuple[re.Pattern[str], ...] = (
    # ``/runs/{run_id}`` — RunDetail page. Excludes ``/runs/{id}/code``,
    # ``/runs/{id}/pine`` (API only) and ``/runs`` (collection endpoint).
    re.compile(r"^/runs/[^/]+/?$"),
)


def _is_spa_html_route(path: str) -> bool:
    """Return True when ``path`` corresponds to a frontend SPA page that
    shadows an API endpoint and should fall back to ``index.html`` on
    browser navigation."""
    if path in _SPA_HTML_EXACT_PATHS:
        return True
    return any(pattern.match(path) for pattern in _SPA_HTML_PATH_REGEX)


@app.middleware("http")
async def _spa_html_deep_link_fallback(request: Request, call_next):
    """Serve ``frontend/dist/index.html`` when a browser navigates directly to
    an SPA path that also exists as an API endpoint.

    Conflicts: ``/runs/{id}`` (RunDetail page vs API) and ``/correlation``
    (Correlation page vs API). Programmatic clients (``Accept: */*`` or
    ``application/json``) still hit the real API handler.
    """
    if request.method == "GET":
        accept = request.headers.get("accept", "")
        if "text/html" in accept and _is_spa_html_route(request.url.path):
            index = _FRONTEND_DIST / "index.html"
            if index.exists():
                return FileResponse(str(index))
    return await call_next(request)


@app.on_event("startup")
async def _run_startup_preflight() -> None:
    """Run preflight checks on server startup."""
    from src.preflight import run_preflight

    run_preflight(console)
    _start_premarket_news_auto_refresh()


def _refresh_premarket_news_bounded(**options) -> Dict[str, Any]:
    """Share a hard, killable refresh budget across manual/auto/daily callers."""
    if not _PREMARKET_NEWS_REFRESH_LOCK.acquire(blocking=False):
        return {"status": "busy", "written": 0, "error": "新闻刷新已在执行，未重复采集"}
    started = datetime.now(timezone.utc).isoformat()
    try:
        import sys
        budget = max(10, min(600, int(os.getenv("PREMARKET_NEWS_REFRESH_TIMEOUT", "180"))))
        result = subprocess.run(
            [sys.executable, "-m", "scripts.refresh_premarket_news"], cwd=str(AGENT_DIR),
            input=json.dumps(options), capture_output=True, text=True, timeout=budget,
        )
        if result.returncode:
            return {"status": "unavailable", "written": 0, "error": "新闻刷新暂不可用，已有新闻保留"}
        return json.loads(result.stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        from app_database import connection
        written = 0
        try:
            with connection() as conn:
                written = conn.execute("SELECT COUNT(*) FROM premarket_news_items WHERE updated_at >= ?", (started,)).fetchone()[0]
        except Exception:
            pass
        return {"status": "partial" if written else "timeout", "written": written,
                "error": "新闻刷新已限时结束；已采集内容保留，未完成的翻译/资讯待补充"}
    except Exception as exc:
        return {"status": "unavailable", "written": 0, "error": f"新闻刷新不可用：{type(exc).__name__}"}
    finally:
        _PREMARKET_NEWS_REFRESH_LOCK.release()


def _start_daily_news_update() -> None:
    if not _DAILY_NEWS_LOCK.acquire(blocking=False):
        return
    today = _auto_scan_today_key()
    key = "daily_three_layer_auto:news"
    previous = cache_get(key) or {}
    if previous.get("date") == today and previous.get("status") == "completed":
        _DAILY_NEWS_LOCK.release()
        return
    cache_set(key, {"date": today, "status": "running", "started_at": datetime.now(timezone.utc).isoformat()})

    def worker():
        started = time.monotonic()
        try:
            result = _refresh_premarket_news_bounded(
                translate_top=5, max_pages=_PREMARKET_NEWS_AUTO_MAX_PAGES,
                refresh_universe=False, enrich_metadata=False,
            )
            record = {**result, "date": today, "finished_at": datetime.now(timezone.utc).isoformat(),
                      "elapsed_seconds": round(time.monotonic() - started, 2)}
            cache_set(key, record)
            console.log(f"[daily-timing] daily_news_refresh: {record['elapsed_seconds']}s status={record.get('status')}")
        finally:
            _DAILY_NEWS_LOCK.release()

    threading.Thread(target=worker, daemon=True, name="daily-news-refresh").start()


def _start_premarket_news_auto_refresh() -> None:
    """Start the 15-minute premarket news refresher once per process."""
    global _PREMARKET_NEWS_AUTO_STARTED
    if not _PREMARKET_NEWS_AUTO_ENABLED:
        _PREMARKET_NEWS_AUTO_STATUS.update({"status": "disabled", "enabled": False})
        return
    with _PREMARKET_NEWS_AUTO_LOCK:
        if _PREMARKET_NEWS_AUTO_STARTED:
            return
        _PREMARKET_NEWS_AUTO_STARTED = True
        _PREMARKET_NEWS_AUTO_STATUS.update({
            "status": "scheduled",
            "enabled": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
        })

    def _loop() -> None:
        time.sleep(_PREMARKET_NEWS_AUTO_STARTUP_DELAY_SECONDS)
        while True:
            started = datetime.now(timezone.utc).isoformat()
            _PREMARKET_NEWS_AUTO_STATUS.update({"status": "running", "last_run_at": started, "last_error": ""})
            try:
                result = _refresh_premarket_news_bounded(
                    translate_top=_PREMARKET_NEWS_AUTO_TRANSLATE_TOP,
                    max_pages=_PREMARKET_NEWS_AUTO_MAX_PAGES,
                    refresh_universe=False,
                    enrich_metadata=False,
                )
                _PREMARKET_NEWS_AUTO_STATUS.update({
                    "status": "idle" if result.get("status") == "completed" else result.get("status", "error"),
                    "last_error": result.get("error", ""),
                    **({"last_success_at": datetime.now(timezone.utc).isoformat()} if result.get("status") == "completed" else {}),
                    "last_result": {
                        "matched_item_count": result.get("matched_item_count"),
                        "raw_news_count": result.get("raw_news_count"),
                        "written": result.get("written"),
                        "universe_symbol_count": result.get("universe_symbol_count"),
                        "window": result.get("window"),
                    },
                })
            except Exception as exc:
                _PREMARKET_NEWS_AUTO_STATUS.update({
                    "status": "error",
                    "last_error": f"{type(exc).__name__}: {str(exc)[-500:]}",
                })
                console.log(f"premarket news auto refresh failed: {str(exc)[-500:]}")
            time.sleep(_PREMARKET_NEWS_AUTO_INTERVAL_SECONDS)

    threading.Thread(target=_loop, daemon=True, name="premarket-news-auto-refresh").start()


# ============================================================================
# API Key Authentication
# ============================================================================

_security = HTTPBearer(auto_error=False)
_API_KEY = os.getenv("API_AUTH_KEY")
_SHELL_TOOLS_ENV = "VIBE_TRADING_ENABLE_SHELL_TOOLS"
_DOCKER_LOOPBACK_ENV = "VIBE_TRADING_TRUST_DOCKER_LOOPBACK"
# Opt-in: treat private-network peers (i.e. the cloudflared tunnel container on
# the Docker network) as local, so a Quick Tunnel works without an API key.
# Safe only because the published host port is bound to 127.0.0.1, so the only
# non-loopback path to the backend is the tunnel itself.
_TUNNEL_TRUST_ENV = "VIBE_TRADING_TRUST_TUNNEL"


def _configured_api_key() -> str:
    """Return the current API auth key, if configured."""
    return os.getenv("API_AUTH_KEY") or _API_KEY or ""


async def require_auth(
    request: Request,
    cred: Optional[HTTPAuthorizationCredentials] = Security(_security),
) -> None:
    """Validate Bearer token for sensitive API endpoints.

    Args:
        request: Incoming HTTP request.
        cred: HTTP Bearer credentials extracted from the Authorization header.

    Raises:
        HTTPException: 403 when dev-mode auth is reached from a non-local client.
        HTTPException: 401 when API_AUTH_KEY is set but the token is missing or wrong.
    """
    _validate_api_auth(request=request, cred=cred)


async def require_event_stream_auth(
    request: Request,
    api_key: Optional[str] = Query(None),
    cred: Optional[HTTPAuthorizationCredentials] = Security(_security),
) -> None:
    """Validate auth for browser EventSource streams.

    Native EventSource cannot send custom Authorization headers, so event
    stream endpoints may accept the API key from the query string. Normal JSON
    endpoints must continue to use Bearer auth only.

    Args:
        request: Incoming HTTP request.
        api_key: Optional query-string API key for EventSource clients.
        cred: HTTP Bearer credentials extracted from the Authorization header.
    """
    _validate_api_auth(request=request, cred=cred, query_api_key=api_key, allow_query=True)


def _auth_credential_from_header_or_query(
    cred: Optional[HTTPAuthorizationCredentials],
    query_api_key: Optional[str],
    *,
    allow_query: bool,
) -> str:
    """Return the supplied API credential from the permitted source."""
    if cred and cred.credentials:
        return cred.credentials
    if allow_query and query_api_key:
        return query_api_key
    return ""


def _validate_api_auth(
    *,
    request: Request,
    cred: Optional[HTTPAuthorizationCredentials],
    query_api_key: Optional[str] = None,
    allow_query: bool = False,
) -> None:
    """Validate configured auth, preserving loopback-only dev mode."""
    api_key = _configured_api_key()
    if not api_key:
        if _is_local_client(request):
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API_AUTH_KEY is required for non-local API access",
        )

    token = _auth_credential_from_header_or_query(cred, query_api_key, allow_query=allow_query)
    if not token or not hmac.compare_digest(token, api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _is_local_client(request: Request) -> bool:
    """Return whether the request originates from a loopback client."""
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    # Trust the Cloudflare tunnel (private Docker-network peer) when opted in.
    if _env_flag_enabled(_TUNNEL_TRUST_ENV) and ip.is_private:
        return True
    return _trusted_docker_loopback_ip(ip)


def _env_flag_enabled(name: str) -> bool:
    """Return whether a boolean environment flag is enabled."""
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _default_gateway_ips() -> set[ipaddress.IPv4Address]:
    """Return IPv4 default gateway addresses from Linux procfs."""
    gateways: set[ipaddress.IPv4Address] = set()
    try:
        lines = Path("/proc/net/route").read_text(encoding="utf-8").splitlines()
    except OSError:
        return gateways

    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        try:
            raw = int(fields[2], 16).to_bytes(4, byteorder="little")
            gateways.add(ipaddress.IPv4Address(raw))
        except ValueError:
            continue
    return gateways


def _trusted_docker_loopback_ip(ip: ipaddress._BaseAddress) -> bool:
    """Return whether an IP is the trusted Docker host gateway.

    Docker Desktop presents host requests to a container as the bridge gateway
    instead of 127.0.0.1. This escape hatch is safe only when the published
    port is bound to host loopback, so the official compose file enables it
    together with a 127.0.0.1 port binding.
    """
    if not isinstance(ip, ipaddress.IPv4Address):
        return False
    if not _env_flag_enabled(_DOCKER_LOOPBACK_ENV):
        return False
    return ip in _default_gateway_ips()


def _env_shell_tools_enabled() -> bool:
    """Return whether server-side shell tools are explicitly enabled."""
    return _env_flag_enabled(_SHELL_TOOLS_ENV)


def _shell_tools_enabled_for_request(request: Request) -> bool:
    """Return whether this API request may expose shell tools to the agent."""
    return _is_local_client(request) or _env_shell_tools_enabled()


async def require_local_or_auth(
    request: Request,
    cred: Optional[HTTPAuthorizationCredentials] = Security(_security),
) -> None:
    """Protect settings access when dev-mode auth is disabled.

    If API_AUTH_KEY is configured, require the bearer token. If not, allow only
    loopback clients so an API server bound to 0.0.0.0 cannot accept remote
    credential reads or writes in dev mode.
    """
    if _configured_api_key():
        await require_auth(request, cred)
        return
    if not _is_local_client(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Settings access requires API_AUTH_KEY or a local loopback client",
        )


# ============================================================================
# Workflow Factory
# ============================================================================

# ============================================================================
# Helper Functions
# ============================================================================

LLM_PROVIDER_CONFIG_PATH = AGENT_DIR / "src" / "providers" / "llm_providers.json"


def _load_llm_providers() -> List[LLMProviderOption]:
    """Load provider metadata from JSON so additions stay data-driven."""
    try:
        raw = json.loads(LLM_PROVIDER_CONFIG_PATH.read_text(encoding="utf-8"))
        providers = [LLMProviderOption(**item) for item in raw]
    except Exception as exc:
        raise RuntimeError(f"Failed to load LLM provider config: {LLM_PROVIDER_CONFIG_PATH}") from exc

    seen: set[str] = set()
    for provider in providers:
        if provider.name in seen:
            raise RuntimeError(f"Duplicate LLM provider name: {provider.name}")
        seen.add(provider.name)
    if not providers:
        raise RuntimeError("LLM provider config must not be empty")
    return providers


LLM_PROVIDERS = _load_llm_providers()
LLM_PROVIDER_BY_NAME = {provider.name: provider for provider in LLM_PROVIDERS}
LLM_REASONING_EFFORTS = {"", "low", "medium", "high", "max"}
LLM_API_KEY_PLACEHOLDERS = {"", "sk-or-v1-your-key-here", "sk-xxx", "xxx", "gsk_xxx"}
TUSHARE_TOKEN_PLACEHOLDERS = {"", "your-tushare-token"}
MARKET_DATA_KEY_PLACEHOLDERS = {"", "your-twelve-data-api-key", "your-fmp-api-key"}


def _ensure_agent_env_file() -> Path:
    """Ensure the project-local agent/.env exists."""
    if not ENV_PATH.exists():
        ENV_PATH.write_text("# Created by easymoneysniper Web UI settings.\n", encoding="utf-8")
    return ENV_PATH


def _strip_env_value(value: str) -> str:
    """Remove basic dotenv quotes and inline comments."""
    value = value.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def _read_env_values(path: Path) -> Dict[str, str]:
    """Read active KEY=value entries from a dotenv file."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = _strip_env_value(value)
    return values


def _read_settings_env_values() -> Dict[str, str]:
    """Read settings without creating agent/.env.

    Prefer the user's active agent/.env. If it does not exist yet, fall back to
    agent/.env.example for display defaults only.
    """
    if ENV_PATH.exists():
        return _read_env_values(ENV_PATH)
    values = _read_env_values(ENV_EXAMPLE_PATH) if ENV_EXAMPLE_PATH.exists() else {}
    # Docker injects agent/.env through env_file while excluding it from the image.
    # In that case the process environment, not the example file, is authoritative.
    if os.environ.get("LANGCHAIN_PROVIDER"):
        for key in (
            "LANGCHAIN_PROVIDER", "LANGCHAIN_MODEL_NAME", "LANGCHAIN_TEMPERATURE",
            "LANGCHAIN_REASONING_EFFORT", "TIMEOUT_SECONDS", "MAX_RETRIES",
            "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
            "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
        ):
            if os.environ.get(key):
                values[key] = os.environ[key]
    return values


def _project_relative_path(path: Path) -> str:
    """Return a project-relative display path without leaking an absolute path."""
    try:
        return path.resolve().relative_to(AGENT_DIR.parent.resolve()).as_posix()
    except ValueError:
        return path.name


def _format_env_value(value: str) -> str:
    """Format a dotenv value without allowing multiline injection."""
    if "\n" in value or "\r" in value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Environment values cannot contain newlines")
    value = value.strip()
    if not value:
        return ""
    if any(ch.isspace() for ch in value) or "#" in value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _write_env_values(path: Path, updates: Dict[str, str]) -> None:
    """Upsert active dotenv values while preserving comments and ordering."""
    _ensure_agent_env_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    for index, raw in enumerate(lines):
        stripped = raw.lstrip()
        is_comment = stripped.startswith("#")
        candidate = stripped[1:].lstrip() if is_comment else stripped
        if "=" not in candidate:
            continue
        key = candidate.split("=", 1)[0].strip()
        if key in updates and key not in seen:
            lines[index] = f"{key}={_format_env_value(updates[key])}"
            seen.add(key)
    missing = [key for key in updates if key not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Updated from Web UI")
        for key in missing:
            lines.append(f"{key}={_format_env_value(updates[key])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _is_configured_secret(value: str, placeholders: set[str]) -> bool:
    """Return True when a secret is set and not a documented placeholder."""
    normalized = value.strip().strip('"').strip("'")
    if not normalized:
        return False
    return normalized.lower() not in {placeholder.lower() for placeholder in placeholders}


def _coerce_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_llm_settings_response(values: Optional[Dict[str, str]] = None) -> LLMSettingsResponse:
    """Build the public settings payload from dotenv values."""
    env_values = values if values is not None else _read_settings_env_values()
    provider_name = env_values.get("LANGCHAIN_PROVIDER", "openai").strip().lower()
    provider = LLM_PROVIDER_BY_NAME.get(provider_name, LLM_PROVIDER_BY_NAME["openai"])
    api_key = env_values.get(provider.api_key_env or "", "") if provider.api_key_env else ""
    api_key_configured = _is_configured_secret(api_key, LLM_API_KEY_PLACEHOLDERS)
    api_key_hint = None
    if provider.auth_type == "oauth":
        try:
            from src.providers.openai_codex import get_openai_codex_login_status

            token = get_openai_codex_login_status()
        except Exception:
            token = None
        api_key_configured = bool(token)
        api_key_hint = None
    return LLMSettingsResponse(
        provider=provider.name,
        model_name=env_values.get("LANGCHAIN_MODEL_NAME", provider.default_model),
        base_url=env_values.get(provider.base_url_env, provider.default_base_url),
        api_key_env=provider.api_key_env,
        api_key_configured=api_key_configured,
        api_key_hint=api_key_hint,
        api_key_required=provider.api_key_required,
        temperature=_coerce_float(env_values.get("LANGCHAIN_TEMPERATURE", "0.0"), 0.0),
        timeout_seconds=_coerce_int(env_values.get("TIMEOUT_SECONDS", "120"), 120),
        max_retries=_coerce_int(env_values.get("MAX_RETRIES", "2"), 2),
        reasoning_effort=env_values.get("LANGCHAIN_REASONING_EFFORT", "").strip().lower(),
        env_path=_project_relative_path(ENV_PATH),
        providers=LLM_PROVIDERS,
    )


def _baostock_supported() -> bool:
    """Check whether the project has a BaoStock loader implementation."""
    loader_dir = AGENT_DIR / "backtest" / "loaders"
    return any((loader_dir / name).exists() for name in ("baostock.py", "baostock_loader.py"))


def _baostock_installed() -> bool:
    """Check whether the optional BaoStock package is importable."""
    import importlib.util

    return importlib.util.find_spec("baostock") is not None


def _build_data_source_settings_response(values: Optional[Dict[str, str]] = None) -> DataSourceSettingsResponse:
    """Build the public data source settings payload."""
    env_values = dict(values if values is not None else _read_settings_env_values())
    env_secret_specs = {
        "TUSHARE_TOKEN": TUSHARE_TOKEN_PLACEHOLDERS,
        "TWELVE_DATA_API_KEY": MARKET_DATA_KEY_PLACEHOLDERS,
        "FMP_API_KEY": MARKET_DATA_KEY_PLACEHOLDERS,
    }
    for key, placeholders in env_secret_specs.items():
        runtime_value = os.environ.get(key, "")
        if _is_configured_secret(runtime_value, placeholders):
            env_values[key] = runtime_value
    token = env_values.get("TUSHARE_TOKEN", "")
    token_configured = _is_configured_secret(token, TUSHARE_TOKEN_PLACEHOLDERS)
    supported = _baostock_supported()
    installed = _baostock_installed()
    if supported:
        baostock_message = "BaoStock loader is available."
    elif installed:
        baostock_message = "BaoStock package is installed, but this project has no BaoStock loader."
    else:
        baostock_message = "No BaoStock loader is registered in this project."
    return DataSourceSettingsResponse(
        tushare_token_configured=token_configured,
        tushare_token_hint=None,
        twelve_data_configured=_is_configured_secret(env_values.get("TWELVE_DATA_API_KEY", ""), MARKET_DATA_KEY_PLACEHOLDERS),
        fmp_configured=_is_configured_secret(env_values.get("FMP_API_KEY", ""), MARKET_DATA_KEY_PLACEHOLDERS),
        baostock_supported=supported,
        baostock_installed=installed,
        baostock_message=baostock_message,
        env_path=_project_relative_path(ENV_PATH),
    )


def _sync_runtime_env(provider: LLMProviderOption, updates: Dict[str, str]) -> None:
    """Apply saved LLM settings to the running API process."""
    for key, value in updates.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)

    if provider.api_key_env:
        key_value = os.environ.get(provider.api_key_env, "")
        if _is_configured_secret(key_value, LLM_API_KEY_PLACEHOLDERS):
            os.environ["OPENAI_API_KEY"] = key_value
        else:
            os.environ.pop("OPENAI_API_KEY", None)
    elif provider.auth_type == "oauth":
        os.environ.pop("OPENAI_API_KEY", None)
    else:
        os.environ["OPENAI_API_KEY"] = "ollama"

    base_url = os.environ.get(provider.base_url_env, "")
    if base_url:
        os.environ["OPENAI_API_BASE"] = base_url
        os.environ["OPENAI_BASE_URL"] = base_url
    else:
        os.environ.pop("OPENAI_API_BASE", None)
        os.environ.pop("OPENAI_BASE_URL", None)


def _load_json_file(path: Path) -> Optional[Dict[str, Any]]:
    """Load JSON from disk if present."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _load_csv_to_dict(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Load CSV rows into a list of dictionaries."""
    try:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
        if limit is not None:
            rows = rows[:limit]
        return rows
    except Exception:
        return []



def _build_response_from_run_dir(run_dir: Path, elapsed: float, *, include_analysis: bool = False) -> RunResponse:
    """Build a run response from a persisted run directory."""
    run_id = run_dir.name

    response = RunResponse(
        status="unknown",
        run_id=run_id,
        elapsed_seconds=elapsed,
        run_directory=str(run_dir),
    )

    state_data = _load_json_file(run_dir / "state.json")
    if state_data:
        state_status = str(state_data.get("status") or "").lower()
        if state_status == "success":
            response.status = "success"
        elif state_status == "failed":
            response.status = "failed"
            response.reason = state_data.get("reason", "")
        else:
            response.status = state_status or "unknown"
    else:
        response.status = "unknown"

    planner_path = run_dir / "planner_output.json"
    response.planner_output = _load_json_file(planner_path)

    design_path = run_dir / "design_spec.json"
    response.strategy_spec = _load_json_file(design_path)

    rag_path = run_dir / "rag_metadata.json"
    rag_data = _load_json_file(rag_path)
    if rag_data:
        response.rag_selection = RAGSelection(
            selected_api=rag_data.get("selected_api") or rag_data.get("api_code", ""),
            selected_name=rag_data.get("selected_name") or rag_data.get("api_name", ""),
            selected_score=float(rag_data.get("selected_score") or rag_data.get("score", 0.0)),
        )

    metrics_path = run_dir / "artifacts" / "metrics.csv"
    if metrics_path.exists():
        metrics_dict_list = _load_csv_to_dict(metrics_path, limit=1)
        if metrics_dict_list:
            row = metrics_dict_list[0]
            try:
                # Pass ALL CSV columns to BacktestMetrics (extra="allow")
                parsed: dict = {}
                for k, v in row.items():
                    if not k or not v:
                        continue
                    try:
                        parsed[k] = int(float(v)) if k == "trade_count" or k == "max_consecutive_loss" else float(v)
                    except (ValueError, TypeError):
                        continue
                if "final_value" in parsed:
                    response.metrics = BacktestMetrics(**parsed)
            except (ValueError, TypeError):
                pass


    artifacts_dir = run_dir / "artifacts"
    if artifacts_dir.exists():
        for file_path in artifacts_dir.iterdir():
            if file_path.is_file():
                file_type = file_path.suffix.lstrip(".")
                response.artifacts.append(
                    Artifact(
                        name=file_path.name,
                        path=str(file_path),
                        type=file_type if file_type else "unknown",
                        size=file_path.stat().st_size,
                        exists=True,
                    )
                )

    preview_suffixes = {".md", ".json", ".py", ".txt", ".log", ".csv"}
    generated_roots = [run_dir, run_dir / "code"]
    seen_generated: set[Path] = set()
    for root in generated_roots:
        if not root.exists():
            continue
        for file_path in sorted(root.iterdir()):
            if not file_path.is_file() or file_path in seen_generated:
                continue
            seen_generated.add(file_path)
            relative_path = file_path.relative_to(run_dir).as_posix()
            response.generated_files.append(
                Artifact(
                    name=relative_path,
                    path=relative_path,
                    type=file_path.suffix.lstrip(".") or "unknown",
                    size=file_path.stat().st_size,
                    exists=True,
                )
            )
            if file_path.suffix.lower() in preview_suffixes and file_path.stat().st_size <= 200_000:
                try:
                    response.file_previews[relative_path] = file_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass

    equity_path = run_dir / "artifacts" / "equity.csv"
    if equity_path.exists():
        response.artifacts_equity_csv = _load_csv_to_dict(equity_path)

    metrics_csv_path = run_dir / "artifacts" / "metrics.csv"
    if metrics_csv_path.exists():
        response.artifacts_metrics_csv = _load_csv_to_dict(metrics_csv_path)

    run_card_path = run_dir / "run_card.json"
    if run_card_path.exists():
        try:
            response.run_card = json.loads(run_card_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    trades_path = run_dir / "artifacts" / "trades.csv"
    if trades_path.exists():
        response.artifacts_trades_csv = _load_csv_to_dict(trades_path)

    validation_path = run_dir / "artifacts" / "validation.json"
    if validation_path.exists():
        try:
            response.validation = json.loads(validation_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    if response.artifacts_equity_csv:
        filtered_equity = []
        for row in response.artifacts_equity_csv[:1000]:
            filtered_row: Dict[str, Any] = {}
            if "timestamp" in row:
                filtered_row["time"] = row["timestamp"]
            if "equity" in row:
                filtered_row["equity"] = row["equity"]
            if "drawdown" in row:
                filtered_row["drawdown"] = row["drawdown"]
            filtered_equity.append(filtered_row)
        response.equity_curve = filtered_equity

    if response.artifacts_trades_csv:
        response.trade_log = response.artifacts_trades_csv[:500]

    if include_analysis:
        analysis = build_run_analysis(run_dir)
        response.run_stage = analysis.get("run_stage")
        response.run_context = analysis.get("run_context")
        response.price_series = analysis.get("price_series")
        response.indicator_series = analysis.get("indicator_series")
        response.trade_markers = analysis.get("trade_markers")
        response.run_logs = analysis.get("run_logs")

    return response


def _select_report_sources(run_dir: Path) -> list[Path]:
    """Pick the most useful text artifacts to include in an on-demand PDF."""
    preferred_names = [
        "final_report.md",
        "report.md",
        "batch_analysis_report.md",
        "sample_analysis_report.md",
        "run_card.md",
    ]
    selected: list[Path] = []
    for name in preferred_names:
        path = run_dir / name
        if path.exists() and path.is_file():
            selected.append(path)

    if not selected:
        selected.extend(sorted(run_dir.glob("*report*.md")))
    if not selected:
        selected.extend(sorted(run_dir.glob("*.md")))
    if not selected:
        selected.extend(sorted(run_dir.glob("*results*.json")))
        selected.extend(sorted(run_dir.glob("*screen*.json")))
    if not selected and (run_dir / "artifacts" / "metrics.csv").exists():
        selected.append(run_dir / "artifacts" / "metrics.csv")

    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in selected:
        if path in seen or not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped[:6]


def _find_options_screening_json(run_dir: Path) -> Path | None:
    candidates = sorted(
        run_dir.glob("options_screening_results*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return path
    return None


def _find_options_screening_pdf(run_dir: Path) -> Path | None:
    """Return an existing options-screening PDF for this run, if present."""
    candidates: list[Path] = []
    for base in (run_dir, run_dir / "artifacts"):
        if base.exists():
            candidates.extend(base.glob("options_screening_results*.pdf"))
            candidates.extend(base.glob("*options*screening*.pdf"))
    candidates = sorted(
        {p for p in candidates if p.is_file() and p.stat().st_size > 1024},
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _find_options_screening_pdf_from_trace(run_dir: Path) -> Path | None:
    """Find the options PDF if an agent accidentally ran in another run dir."""
    trace_path = run_dir / "trace.jsonl"
    if not trace_path.exists():
        return None
    pattern = __import__("re").compile(r"/app/agent/runs/[A-Za-z0-9_-]+")
    candidates: list[Path] = []
    try:
        for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except Exception:
                continue
            blob = json.dumps(record.get("args", {}), ensure_ascii=False)
            for match in pattern.findall(blob):
                other = Path(match)
                if other == run_dir or not other.exists():
                    continue
                found = _find_options_screening_pdf(other)
                if found is not None:
                    candidates.append(found)
    except OSError:
        return None
    candidates = sorted(
        {p for p in candidates if p.is_file() and p.stat().st_size > 1024},
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _options_order_report_html(run_id: str, data: dict[str, Any]) -> str:
    results = data.get("results") or []
    portfolio = data.get("portfolio") or []
    tradable = [r for r in results if r.get("tradable")]
    generated_at = html.escape(str(data.get("timestamp") or datetime.utcnow().isoformat()))
    expiry = html.escape(str(data.get("target_expiry") or ""))

    def money(value: Any) -> str:
        try:
            return f"${float(value):,.0f}"
        except Exception:
            return "-"

    def pct(value: Any) -> str:
        try:
            return f"{float(value) * 100:.1f}%"
        except Exception:
            return "-"

    def result_row(r: dict[str, Any]) -> str:
        return (
            "<tr>"
            f"<td>{html.escape(str(r.get('ticker', '')))}</td>"
            f"<td>{html.escape(str(r.get('primary_strategy', '')))}</td>"
            f"<td>{html.escape(str(r.get('strategy_desc', '-')))}</td>"
            f"<td>{pct(r.get('pop', 0))}</td>"
            f"<td>{float(r.get('roc', 0) or 0) * 100:.1f}%</td>"
            f"<td>{money(r.get('expected_value', 0))}</td>"
            f"<td>{money(r.get('max_loss', 0))}</td>"
            f"<td>{float(r.get('final_score', 0) or 0):.1f}</td>"
            f"<td>{html.escape(str(r.get('quote_quality', '-')))}</td>"
            "</tr>"
        )

    def order_row(r: dict[str, Any]) -> str:
        credit = float(r.get("credit", 0) or 0)
        debit = float(r.get("debit", 0) or 0)
        action = "SELL combo for net credit" if credit > 0 else "BUY combo for net debit"
        limit_rule = f"Limit >= ${credit:.2f} credit" if credit > 0 else f"Limit <= ${debit:.2f} debit"
        return (
            "<tr>"
            f"<td>{html.escape(str(r.get('ticker', '')))}</td>"
            f"<td>{html.escape(str(r.get('strategy_desc', '-')))}</td>"
            f"<td>{action}</td>"
            f"<td>{limit_rule}</td>"
            "<td>Use one multi-leg order. Verify live bid/ask. Do not market order.</td>"
            "</tr>"
        )

    portfolio_rows = "".join(result_row(r) for r in portfolio) or '<tr><td colspan="9">No portfolio candidate passed constraints.</td></tr>'
    order_rows = "".join(order_row(r) for r in portfolio) or '<tr><td colspan="5">No order plan available.</td></tr>'
    top_rows = "".join(result_row(r) for r in results[:12])
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Options Order Ranking {html.escape(run_id)}</title>
  <style>
    @page {{ size: A4 landscape; margin: 11mm; }}
    body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", Arial, sans-serif; color: #102033; font-size: 9.5px; }}
    .cover {{ background: #0f172a; color: white; border-radius: 10px; padding: 16px 20px; margin-bottom: 10px; }}
    h1 {{ margin: 0 0 5px; font-size: 23px; }}
    h2 {{ font-size: 13px; margin: 12px 0 6px; }}
    .sub {{ color: #cbd5e1; }}
    .cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 9px 0 10px; }}
    .card {{ border: 1px solid #d8e2ef; border-radius: 7px; padding: 8px; background: #f8fafc; }}
    .label {{ color: #64748b; font-size: 8px; text-transform: uppercase; }}
    .value {{ font-size: 15px; font-weight: 700; margin-top: 2px; }}
    .note {{ background: #fff7ed; border: 1px solid #fed7aa; border-radius: 7px; color: #7c2d12; padding: 7px 9px; margin: 8px 0 10px; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; page-break-inside: avoid; }}
    th {{ background: #164e63; color: white; font-size: 8px; padding: 5px; text-align: left; }}
    td {{ border-bottom: 1px solid #e2e8f0; padding: 4px 5px; vertical-align: top; word-break: break-word; }}
    tr:nth-child(even) td {{ background: #f8fafc; }}
  </style>
</head>
<body>
  <section class="cover">
    <h1>Options Combo Ranking & Order Plan</h1>
    <div class="sub">Run ID: {html.escape(run_id)} | Generated: {generated_at} | Expiry: {expiry}</div>
  </section>
  <section class="cards">
    <div class="card"><div class="label">Universe</div><div class="value">{data.get('ticker_count', len(results))}</div></div>
    <div class="card"><div class="label">Valid</div><div class="value">{len(results)}</div></div>
    <div class="card"><div class="label">Tradable</div><div class="value">{len(tradable)}</div></div>
    <div class="card"><div class="label">Portfolio</div><div class="value">{len(portfolio)}</div></div>
  </section>
  <div class="note">只保留下单相关信息：组合排名、POP/ROC/EV、最大风险、限价下单方法。若 Quote 为 estimated_from_yfinance，请在券商端用实时 bid/ask 二次确认。</div>
  <h2>Recommended Portfolio</h2>
  <table><thead><tr><th>Ticker</th><th>Strategy</th><th>Combo Legs</th><th>POP</th><th>ROC</th><th>EV</th><th>Max Loss</th><th>Score</th><th>Quote</th></tr></thead><tbody>{portfolio_rows}</tbody></table>
  <h2>Order Method</h2>
  <table><thead><tr><th>Ticker</th><th>Combo Legs</th><th>Action</th><th>Limit Rule</th><th>Execution Note</th></tr></thead><tbody>{order_rows}</tbody></table>
  <h2>Top 12 Ranking</h2>
  <table><thead><tr><th>Ticker</th><th>Strategy</th><th>Combo Legs</th><th>POP</th><th>ROC</th><th>EV</th><th>Max Loss</th><th>Score</th><th>Quote</th></tr></thead><tbody>{top_rows}</tbody></table>
</body>
</html>"""


def _options_order_report_html_v2(run_id: str, data: dict[str, Any]) -> str:
    results = data.get("results") or []
    portfolio = data.get("portfolio") or []
    tradable = [r for r in results if r.get("tradable")]
    generated_at = html.escape(str(data.get("timestamp") or datetime.utcnow().isoformat()))
    expiry = html.escape(str(data.get("target_expiry") or ""))

    def money(value: Any) -> str:
        try:
            return f"${float(value):,.0f}"
        except Exception:
            return "-"

    def pct(value: Any) -> str:
        try:
            return f"{float(value) * 100:.1f}%"
        except Exception:
            return "-"

    def strategy_cn(name: str) -> str:
        return {
            "bull_put_spread": "卖方 Put Spread",
            "bear_call_spread": "卖方 Call Spread",
            "bull_call_spread": "买方 Bull Call Spread",
            "bear_put_spread": "买方 Bear Put Spread",
            "iron_condor": "Iron Condor",
            "long_strangle": "Long Strangle",
        }.get(name, name)

    def price_text(r: dict[str, Any]) -> str:
        credit = float(r.get("credit", 0) or 0)
        debit = float(r.get("debit", 0) or 0)
        return f"${credit:.2f} Cr" if credit > 0 else f"${debit:.2f} Dr"

    def order_action(r: dict[str, Any]) -> str:
        credit = float(r.get("credit", 0) or 0)
        debit = float(r.get("debit", 0) or 0)
        if credit > 0:
            return f"SELL combo @ Limit >= ${credit:.2f} Credit"
        return f"BUY combo @ Limit <= ${debit:.2f} Debit"

    def knockout_html(r: dict[str, Any]) -> str:
        strategy = str(r.get("primary_strategy", ""))
        spot = float(r.get("spot", 0) or 0)
        max_loss = money(r.get("max_loss", 0))
        if spot <= 0:
            return '<div class="knockout">敲出价位：缺少现价，无法计算距离比例。</div>'

        def dist(price: float, direction: str) -> str:
            if direction == "down":
                return f"{max((spot - price) / spot, 0) * 100:.1f}%"
            return f"{max((price - spot) / spot, 0) * 100:.1f}%"

        if strategy == "bull_put_spread":
            warn = float(r.get("short_strike", 0) or 0)
            ko = float(r.get("breakeven", warn) or warn)
            long = float(r.get("long_strike", 0) or 0)
            return f'<div class="knockout">敲出/风控：跌破 ${warn:.2f} 进入风险区；硬敲出参考 ${ko:.2f}（距现价 {dist(ko, "down")}）。到期在该价附近约盈亏平衡；跌破保护腿 ${long:.2f} 接近最大亏损 {max_loss}。</div>'
        if strategy == "bear_call_spread":
            warn = float(r.get("short_strike", 0) or 0)
            ko = float(r.get("breakeven", warn) or warn)
            long = float(r.get("long_strike", 0) or 0)
            return f'<div class="knockout">敲出/风控：涨破 ${warn:.2f} 进入风险区；硬敲出参考 ${ko:.2f}（距现价 {dist(ko, "up")}）。到期在该价附近约盈亏平衡；涨破保护腿 ${long:.2f} 接近最大亏损 {max_loss}。</div>'
        if strategy == "iron_condor":
            lower = float(r.get("lower_breakeven", r.get("put_short", 0)) or 0)
            upper = float(r.get("upper_breakeven", r.get("call_short", 0)) or 0)
            pl = float(r.get("put_long", 0) or 0)
            cl = float(r.get("call_long", 0) or 0)
            return f'<div class="knockout">敲出/风控区间：下破 ${lower:.2f} 或上破 ${upper:.2f} 视为敲出预警（距现价分别 {dist(lower, "down")} / {dist(upper, "up")}）。到期在边界约盈亏平衡；跌破 ${pl:.2f} 或涨破 ${cl:.2f} 接近最大亏损 {max_loss}。</div>'
        if strategy == "bull_call_spread":
            invalid = float(r.get("long_strike", 0) or 0)
            be = float(r.get("breakeven", invalid) or invalid)
            short = float(r.get("short_strike", 0) or 0)
            return f'<div class="knockout">失效/风控：若价格回落并持续低于买入腿 ${invalid:.2f}，动量失效；盈亏平衡 ${be:.2f}（距现价 {dist(be, "up")}）。到期低于买入腿接近最大亏损 {max_loss}，涨至 ${short:.2f} 附近接近最大盈利。</div>'
        if strategy == "bear_put_spread":
            invalid = float(r.get("long_strike", 0) or 0)
            be = float(r.get("breakeven", invalid) or invalid)
            short = float(r.get("short_strike", 0) or 0)
            return f'<div class="knockout">失效/风控：若价格反弹并持续高于买入腿 ${invalid:.2f}，下跌逻辑失效；盈亏平衡 ${be:.2f}（距现价 {dist(be, "down")}）。到期高于买入腿接近最大亏损 {max_loss}，跌至 ${short:.2f} 附近接近最大盈利。</div>'
        return '<div class="knockout">敲出价位：该策略暂无自动风控边界，请以权利金止损和盈亏平衡点管理。</div>'

    def result_row(r: dict[str, Any], rank: int) -> str:
        return (
            "<tr>"
            f"<td>{rank}</td>"
            f"<td>{html.escape(str(r.get('ticker', '')))}</td>"
            f"<td>{html.escape(strategy_cn(str(r.get('primary_strategy', ''))))}</td>"
            f"<td>{pct(r.get('pop', 0))}</td>"
            f"<td>{float(r.get('roc', 0) or 0) * 100:.1f}%</td>"
            f"<td>{money(r.get('expected_value', 0))}</td>"
            f"<td>{money(r.get('max_loss', 0))}</td>"
            f"<td>{float(r.get('final_score', 0) or 0):.1f}</td>"
            "</tr>"
        )

    def trade_card(r: dict[str, Any], idx: int) -> str:
        return f"""
        <div class="trade">
          <div class="trade-title">Trade {idx} — {html.escape(str(r.get('ticker', '')))} · {html.escape(strategy_cn(str(r.get('primary_strategy', ''))))}</div>
          <div class="legs">{html.escape(str(r.get('strategy_desc', '-')))}</div>
          <div class="trade-grid">
            <span>Price: <b>{price_text(r)}</b></span>
            <span>POP: <b>{pct(r.get('pop', 0))}</b></span>
            <span>Max Profit: <b>{money(r.get('max_profit', 0))}</b></span>
            <span>Max Loss: <b>{money(r.get('max_loss', 0))}</b></span>
            <span>ROC: <b>{float(r.get('roc', 0) or 0) * 100:.1f}%</b></span>
            <span>Score: <b>{float(r.get('final_score', 0) or 0):.1f}</b></span>
          </div>
          {knockout_html(r)}
          <div class="order-line">{order_action(r)}；使用组合限价单一次性下单，不拆腿，不用市价单。</div>
        </div>"""

    def order_row(r: dict[str, Any]) -> str:
        return (
            "<tr>"
            f"<td>{html.escape(str(r.get('ticker', '')))}</td>"
            f"<td>{html.escape(str(r.get('strategy_desc', '-')))}</td>"
            f"<td>{order_action(r)}</td>"
            "<td>止盈：赚到权利金/最大盈利 70-80% 平仓；止损：亏损达最大亏损 50-60% 评估平仓；到期前 5-7 天不扛 Gamma。</td>"
            "</tr>"
        )

    top_rows = "".join(result_row(r, i) for i, r in enumerate(results[:10], 1))
    trade_cards = "".join(trade_card(r, i) for i, r in enumerate(portfolio, 1)) or '<div class="note">没有组合候选通过资金、EV、ROC 与相关性约束。</div>'
    order_rows = "".join(order_row(r) for r in portfolio) or '<tr><td colspan="4">没有可执行组合。</td></tr>'
    total_credit = sum(float(r.get("credit", 0) or 0) * 100 for r in portfolio)
    total_debit = sum(float(r.get("debit", 0) or 0) * 100 for r in portfolio)
    total_max_profit = sum(float(r.get("max_profit", 0) or 0) for r in portfolio)
    total_max_loss = sum(float(r.get("max_loss", 0) or 0) for r in portfolio)
    any_win = 1.0
    for r in portfolio:
        any_win *= 1.0 - float(r.get("pop", 0) or 0)
    combo_any_win = 1.0 - any_win if portfolio else 0.0
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>期权组合下单胜率排名 {html.escape(run_id)}</title>
  <style>
    @page {{ size: A4; margin: 12mm; }}
    body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", Arial, sans-serif; color: #102033; font-size: 10.5px; line-height: 1.45; }}
    .cover {{ background: #0f172a; color: white; border-radius: 10px; padding: 18px 20px; margin-bottom: 12px; }}
    h1 {{ margin: 0 0 6px; font-size: 23px; }}
    h2 {{ color: #0f172a; font-size: 14px; margin: 14px 0 7px; border-left: 4px solid #0f766e; padding-left: 7px; }}
    .sub {{ color: #dbeafe; }}
    .cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 10px 0 12px; }}
    .card {{ border: 1px solid #d8e2ef; border-radius: 7px; padding: 8px; background: #f8fafc; }}
    .label {{ color: #64748b; font-size: 8px; text-transform: uppercase; }}
    .value {{ font-size: 14px; font-weight: 700; margin-top: 3px; }}
    .note {{ background: #fff7ed; border: 1px solid #fed7aa; color: #7c2d12; border-radius: 8px; padding: 8px 10px; margin: 8px 0 12px; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th {{ background: #164e63; color: white; font-size: 8px; padding: 6px 5px; text-align: left; }}
    td {{ border-bottom: 1px solid #e2e8f0; padding: 5px; vertical-align: top; word-break: break-word; }}
    tr:nth-child(even) td {{ background: #f8fafc; }}
    .trade {{ border: 1px solid #d8e2ef; border-radius: 8px; padding: 9px 10px; margin: 8px 0; break-inside: avoid; }}
    .trade-title {{ font-size: 12px; font-weight: 700; color: #0f172a; margin-bottom: 4px; }}
    .legs {{ font-family: Consolas, "Microsoft YaHei", monospace; color: #334155; margin-bottom: 6px; }}
    .trade-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px 10px; }}
    .knockout {{ background: #fef2f2; border: 1px solid #fecaca; border-radius: 7px; color: #7f1d1d; margin-top: 7px; padding: 7px 8px; }}
    .order-line {{ margin-top: 6px; color: #0f766e; font-weight: 700; }}
    .rules {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }}
    .rule {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 7px; padding: 8px; }}
  </style>
</head>
<body>
  <section class="cover">
    <h1>期权组合下单胜率排名与执行方案</h1>
    <div class="sub">Run ID: {html.escape(run_id)} | 到期日: {expiry} | 生成时间: {generated_at}</div>
  </section>
  <section class="cards">
    <div class="card"><div class="label">标的数</div><div class="value">{data.get('ticker_count', len(results))}</div></div>
    <div class="card"><div class="label">有效策略</div><div class="value">{len(results)}</div></div>
    <div class="card"><div class="label">可构建组合</div><div class="value">{len(tradable)}</div></div>
    <div class="card"><div class="label">推荐下单</div><div class="value">{len(portfolio)}</div></div>
  </section>
  <div class="note">核心口径：POP 为模型胜率/历史障碍胜率融合值；权利金若来自 yfinance 估算，实盘必须在券商端用实时 bid/ask 复核后再挂限价组合单。</div>
  <h2>策略评分排名 Top 10</h2>
  <table><thead><tr><th>排名</th><th>标的</th><th>最佳策略</th><th>胜率</th><th>ROC</th><th>期望值</th><th>最大亏损</th><th>评分</th></tr></thead><tbody>{top_rows}</tbody></table>
  <h2>具体交易构建</h2>
  {trade_cards}
  <h2>组合推荐</h2>
  <section class="cards">
    <div class="card"><div class="label">净收/付</div><div class="value">${total_credit - total_debit:,.0f}</div></div>
    <div class="card"><div class="label">最大盈利</div><div class="value">${total_max_profit:,.0f}</div></div>
    <div class="card"><div class="label">最大亏损</div><div class="value">${total_max_loss:,.0f}</div></div>
    <div class="card"><div class="label">至少一单盈利</div><div class="value">{combo_any_win * 100:.1f}%</div></div>
  </section>
  <h2>一键下单速查</h2>
  <table><thead><tr><th>标的</th><th>具体组合</th><th>下单指令</th><th>管理规则</th></tr></thead><tbody>{order_rows}</tbody></table>
  <h2>日频交易执行守则</h2>
  <div class="rules">
    <div class="rule"><b>每日检查</b><br>价格 vs short strike/盈亏平衡点；IV 是否快速扩张；5日趋势是否反向。</div>
    <div class="rule"><b>止盈</b><br>卖方价差赚到权利金 70-80% 平仓；买方价差达到最大盈利 70-80% 分批止盈。</div>
    <div class="rule"><b>止损</b><br>亏损达到最大亏损 50-60% 或价格触及 short strike 附近，立即评估减仓/平仓。</div>
    <div class="rule"><b>到期管理</b><br>到期前 5-7 天处理，避免最后一周 Gamma 风险；不要裸扛到期。</div>
  </div>
  <h2>核心建议</h2>
  <div class="note">优先执行推荐组合中的前 3-5 单；全部使用组合限价单。若实时价格低于报告估算的 Credit 或高于报告估算的 Debit，取消等待，不追价。</div>
</body>
</html>"""


def _text_to_report_html(run_id: str, sources: list[Path], run_dir: Path) -> str:
    """Render selected run artifacts into printable research-report HTML."""
    options_json = _find_options_screening_json(run_dir)
    if options_json is not None:
        try:
            return _options_order_report_html_v2(run_id, json.loads(options_json.read_text(encoding="utf-8")))
        except Exception:
            pass

    sections: list[str] = []
    for idx, source in enumerate(sources, start=1):
        rel = source.relative_to(run_dir).as_posix()
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > 180_000:
            text = text[:180_000] + "\n\n[内容过长，PDF 预览已截断。完整文件请在报告文件页签查看。]"
        line_count = text.count("\n") + 1 if text else 0
        sections.append(
            '<section class="report-section">'
            '<div class="section-head">'
            f'<div><span class="section-index">{idx:02d}</span><h2>{html.escape(rel)}</h2></div>'
            f'<span class="section-meta">{line_count:,} lines</span>'
            "</div>"
            f'<pre class="report-pre">{html.escape(text)}</pre>'
            "</section>"
        )

    if not sections:
        sections.append(
            '<section class="report-section empty-state">'
            "<h2>暂无可导出的文本结果</h2>"
            "<p>没有找到可生成 PDF 的文本结果文件。请确认本次运行是否产出了 Markdown、JSON、CSV、日志或报告文件。</p>"
            "</section>"
        )

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    source_count = len(sources)
    generated_files = [
        p.relative_to(run_dir).as_posix()
        for p in sorted(run_dir.rglob("*"))
        if p.is_file() and not p.name.startswith(".") and p.stat().st_size > 0
    ]
    file_count = len(generated_files)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>easymoneysniper Run Report {html.escape(run_id)}</title>
  <style>
    @page {{
      size: A4;
      margin: 15mm 14mm 18mm;
      @bottom-right {{
        color: #64748b;
        content: "easymoneysniper · " counter(page);
        font-size: 9px;
      }}
    }}
    body {{
      font-family: "Noto Sans CJK SC", "Microsoft YaHei", "SimSun", "Arial Unicode MS", sans-serif;
      background: #f8fafc;
      color: #0f172a;
      font-size: 11px;
      line-height: 1.55;
      margin: 0;
    }}
    .cover {{
      background: linear-gradient(135deg, #0f172a 0%, #164e63 55%, #0f766e 100%);
      border-radius: 14px;
      color: #ffffff;
      margin-bottom: 14px;
      padding: 22px 24px;
    }}
    .brand {{
      color: #a7f3d0;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 1.6px;
      text-transform: uppercase;
    }}
    h1 {{
      font-size: 27px;
      line-height: 1.18;
      margin: 9px 0 8px;
    }}
    .subtitle {{
      color: #dbeafe;
      font-size: 11px;
      margin: 0 0 16px;
    }}
    .meta-grid {{
      display: grid;
      gap: 8px;
      grid-template-columns: repeat(3, 1fr);
    }}
    .metric {{
      background: rgba(255, 255, 255, 0.12);
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 10px;
      padding: 10px 11px;
    }}
    .metric-label {{
      color: #bae6fd;
      font-size: 9px;
      margin-bottom: 4px;
    }}
    .metric-value {{
      font-size: 13px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }}
    .report-section {{
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: 12px;
      box-shadow: 0 7px 20px rgba(15, 23, 42, 0.05);
      margin: 12px 0;
      padding: 14px;
    }}
    .section-head {{
      align-items: flex-start;
      border-bottom: 1px solid #e2e8f0;
      display: flex;
      justify-content: space-between;
      margin-bottom: 10px;
      padding-bottom: 8px;
    }}
    .section-index {{
      background: #ccfbf1;
      border-radius: 999px;
      color: #0f766e;
      display: inline-block;
      font-size: 9px;
      font-weight: 800;
      margin-right: 8px;
      padding: 2px 7px;
      vertical-align: 2px;
    }}
    h2 {{
      color: #0f172a;
      display: inline;
      font-size: 13px;
      margin: 0;
      overflow-wrap: anywhere;
    }}
    .section-meta {{
      color: #64748b;
      font-size: 9px;
      white-space: nowrap;
    }}
    .report-pre {{
      white-space: pre-wrap;
      word-break: break-word;
      overflow-wrap: anywhere;
      font-family: "Consolas", "Microsoft YaHei", monospace;
      background: #f8fafc;
      border-left: 4px solid #14b8a6;
      border-radius: 8px;
      color: #111827;
      font-size: 9.5px;
      line-height: 1.48;
      margin: 0;
      padding: 11px 12px;
    }}
    .empty-state p {{
      color: #475569;
      margin-bottom: 0;
    }}
    section {{ break-inside: auto; }}
  </style>
</head>
<body>
  <header class="cover">
    <div class="brand">easymoneysniper Research · #35</div>
    <h1>策略运行报告</h1>
    <p class="subtitle">自动汇总本次策略运行产物、核心文本结果与可审计记录。</p>
    <div class="meta-grid">
      <div class="metric"><div class="metric-label">Run ID</div><div class="metric-value">{html.escape(run_id)}</div></div>
      <div class="metric"><div class="metric-label">Generated</div><div class="metric-value">{generated_at}</div></div>
      <div class="metric"><div class="metric-label">Files / Sources</div><div class="metric-value">{file_count} / {source_count}</div></div>
    </div>
  </header>
  {''.join(sections)}
</body>
</html>"""


def _ensure_run_report_pdf(run_dir: Path) -> Path:
    """Create or refresh a downloadable PDF report for a run."""
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(exist_ok=True)
    options_pdf = _find_options_screening_pdf(run_dir)
    if options_pdf is not None:
        return options_pdf
    traced_options_pdf = _find_options_screening_pdf_from_trace(run_dir)
    if traced_options_pdf is not None:
        target = artifacts_dir / traced_options_pdf.name
        try:
            if not target.exists() or target.stat().st_mtime < traced_options_pdf.stat().st_mtime:
                __import__("shutil").copy2(traced_options_pdf, target)
            return target
        except OSError:
            return traced_options_pdf
    pdf_path = artifacts_dir / "report.pdf"

    sources = _select_report_sources(run_dir)
    source_mtime = max((p.stat().st_mtime for p in sources), default=0)
    renderer_mtime = Path(__file__).stat().st_mtime
    if pdf_path.exists() and pdf_path.stat().st_mtime >= max(source_mtime, renderer_mtime):
        return pdf_path

    report_html = _text_to_report_html(run_dir.name, sources, run_dir)
    try:
        from weasyprint import HTML  # type: ignore[import-not-found]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF renderer unavailable: {exc}") from exc

    try:
        HTML(string=report_html, base_url=str(run_dir)).write_pdf(str(pdf_path))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}") from exc
    return pdf_path


# ============================================================================
# Path-parameter validation
# ============================================================================

# ``run_id`` and ``session_id`` flow directly into filesystem paths
# (``RUNS_DIR / run_id`` etc.). Restrict to a safe character class so that
# values like ``..`` or ``foo/../bar`` cannot escape the parent directory.
_SAFE_PATH_PARAM_RE = __import__("re").compile(r"^[A-Za-z0-9_-]{1,128}$")


def _validate_path_param(value: str, kind: str) -> None:
    """Reject path parameters that could escape the parent directory.

    Args:
        value: User-supplied path-parameter value.
        kind: Parameter name, used in the error detail.

    Raises:
        HTTPException: 400 when ``value`` does not match the safe character
            class, mirroring the existing ``_SHADOW_ID_RE`` check.
    """
    if not _SAFE_PATH_PARAM_RE.fullmatch(value or ""):
        raise HTTPException(status_code=400, detail=f"invalid {kind}")


def _safe_event_ticker(raw: str) -> str:
    ticker = str(raw or "").strip().upper().replace(".", "-")
    if not re.fullmatch(r"[A-Z0-9-]{1,12}", ticker):
        raise HTTPException(status_code=400, detail=f"invalid ticker: {raw}")
    return ticker


def _event_scan_artifacts(run_dir: Path) -> List[Artifact]:
    artifacts: List[Artifact] = []
    for base in (run_dir / "data", run_dir / "artifacts"):
        if not base.exists():
            continue
        for path in sorted(base.iterdir()):
            if not path.is_file():
                continue
            artifacts.append(
                Artifact(
                    name=path.name,
                    path=str(path),
                    type=path.suffix.lstrip(".") or "file",
                    size=path.stat().st_size,
                    exists=True,
                )
            )
    return artifacts


def _summarize_event_calibration(run_dir: Path) -> Optional[EventCalibrationInfo]:
    summary_path = run_dir / "artifacts" / "event_impact_summary.json"
    payload = _load_json_file(summary_path)
    if not payload:
        return None
    model = payload.get("model") or {}
    try:
        event_count = int(payload.get("event_count", 0) or 0)
    except Exception:
        event_count = 0
    try:
        primary_beta = float(model.get("primary_beta"))
    except Exception:
        primary_beta = None
    try:
        primary_ic = float(model.get("primary_ic"))
    except Exception:
        primary_ic = None
    try:
        updated_at = datetime.utcfromtimestamp(summary_path.stat().st_mtime).isoformat() + "Z"
    except Exception:
        updated_at = ""
    return EventCalibrationInfo(
        run_id=run_dir.name,
        summary_path=str(summary_path),
        event_count=event_count,
        usable=bool(model.get("usable")),
        primary_horizon=model.get("primary_horizon"),
        primary_beta=primary_beta,
        primary_ic=primary_ic,
        updated_at=updated_at,
    )


def _list_event_calibrations(
    usable_only: bool = False,
    min_events: int = 1,
) -> List[EventCalibrationInfo]:
    calibrations: List[EventCalibrationInfo] = []
    if not RUNS_DIR.exists():
        return calibrations
    for run_dir in RUNS_DIR.iterdir():
        if not run_dir.is_dir():
            continue
        item = _summarize_event_calibration(run_dir)
        if item is None:
            continue
        if usable_only and not item.usable:
            continue
        if item.event_count < min_events:
            continue
        calibrations.append(item)
    calibrations.sort(key=lambda item: item.updated_at, reverse=True)
    return calibrations


def _select_event_calibration(min_events: int = 20) -> Optional[EventCalibrationInfo]:
    candidates = _list_event_calibrations(usable_only=True, min_events=min_events)
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            abs(item.primary_ic or 0.0),
            item.event_count,
            item.updated_at,
        ),
        reverse=True,
    )[0]


def _run_event_driven_scan_sync(payload: EventDrivenScanRequest) -> EventDrivenScanResponse:
    """Create a run directory and execute the event scanner."""
    started = time.time()
    run_id = f"event_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    run_dir = RUNS_DIR / run_id
    (run_dir / "code").mkdir(parents=True, exist_ok=True)
    (run_dir / "data").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    script_dir = AGENT_DIR / "scripts" / "event_driven_sp500"
    scanner_path = script_dir / "sp500_event_scanner.py"
    signal_path = script_dir / "signal_engine.py"
    config_path = script_dir / "config.example.json"
    if not scanner_path.exists() or not signal_path.exists():
        raise HTTPException(status_code=500, detail="event-driven scanner files are missing")

    shutil.copy2(signal_path, run_dir / "code" / "signal_engine.py")
    if config_path.exists():
        shutil.copy2(config_path, run_dir / "config.json")

    tickers = [_safe_event_ticker(t) for t in (payload.tickers or []) if str(t).strip()]
    resolved_universe = None
    if not tickers:
        try:
            resolved_universe = resolve_research_universe(payload.universe)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tickers = [_safe_event_ticker(t) for t in resolved_universe["tickers"]]
        if payload.limit:
            tickers = tickers[: payload.limit]
    req_record = payload.model_dump()
    req_record["prompt"] = "Shared-universe event-driven launch signal scan"
    if resolved_universe:
        req_record["resolved_universe"] = {
            "universe": resolved_universe["universe"],
            "source": resolved_universe["source"],
            "ticker_count": len(tickers),
        }
    req_record["created_at"] = datetime.utcnow().isoformat() + "Z"
    (run_dir / "req.json").write_text(json.dumps(req_record, ensure_ascii=False, indent=2), encoding="utf-8")

    cmd = [
        _sys.executable,
        str(scanner_path),
        "--output-dir",
        str(run_dir),
        "--top",
        str(payload.top),
        "--news-per-ticker",
        str(payload.news_per_ticker),
        "--lookback-hours",
        str(payload.lookback_hours),
        "--sleep",
        str(payload.sleep_seconds),
        "--min-relevance",
        str(payload.min_relevance),
        "--cache-ttl-minutes",
        str(payload.cache_ttl_minutes),
        "--workers",
        str(payload.workers),
    ]
    if tickers:
        cmd.extend(["--tickers", ",".join(tickers)])
    if not payload.include_prices:
        cmd.append("--no-price")
    if payload.use_llm_scoring:
        cmd.append("--llm-score")
    calibration_path = None
    calibration_meta = None
    if payload.calibration_run_id:
        _validate_path_param(payload.calibration_run_id, "calibration_run_id")
        candidate = RUNS_DIR / payload.calibration_run_id / "artifacts" / "event_impact_summary.json"
        if not candidate.exists():
            raise HTTPException(status_code=404, detail=f"Calibration summary not found for run {payload.calibration_run_id}")
        calibration_path = candidate
        calibration_meta = _summarize_event_calibration(RUNS_DIR / payload.calibration_run_id)
    elif payload.calibration_json:
        candidate = (AGENT_DIR / payload.calibration_json).resolve() if not Path(payload.calibration_json).is_absolute() else Path(payload.calibration_json).resolve()
        try:
            candidate.relative_to(AGENT_DIR.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="calibration_json must be under agent directory") from exc
        if not candidate.exists():
            raise HTTPException(status_code=404, detail="calibration_json not found")
        calibration_path = candidate
        calibration_meta = {
            "run_id": None,
            "summary_path": str(candidate),
            "event_count": 0,
            "usable": None,
            "primary_horizon": None,
            "primary_beta": None,
            "primary_ic": None,
        }
    elif payload.auto_calibration:
        calibration_meta = _select_event_calibration(payload.min_calibration_events)
        if calibration_meta:
            calibration_path = Path(calibration_meta.summary_path)
    if calibration_path is not None:
        cmd.extend(["--calibration-json", str(calibration_path)])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(AGENT_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=payload.timeout_seconds,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        (run_dir / "state.json").write_text(
            json.dumps({"status": "failed", "reason": "event scan timeout"}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise HTTPException(status_code=504, detail=f"event scan timeout after {payload.timeout_seconds}s") from exc

    (run_dir / "logs").mkdir(exist_ok=True)
    (run_dir / "logs" / "event_scan_stdout.log").write_text(proc.stdout or "", encoding="utf-8")
    (run_dir / "logs" / "event_scan_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "event scanner failed")[-2000:]
        (run_dir / "state.json").write_text(
            json.dumps({"status": "failed", "reason": reason}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise HTTPException(status_code=500, detail=reason)

    summary = {}
    try:
        summary = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception:
        summary = {}

    signals = _load_csv_to_dict(run_dir / "artifacts" / "signals.csv", limit=payload.top)
    try:
        opportunity_map = {
            str(item.get("ticker") or "").upper(): item
            for item in _stock_signal_observation_pool(payload.universe, limit=max(payload.top, payload.limit or payload.top))
        }
        for item in signals:
            symbol = str(item.get("symbol") or "").upper()
            opportunity = opportunity_map.get(symbol) or {}
            if opportunity.get("pullback_rejection"):
                item["pullback_rejection"] = opportunity.get("pullback_rejection")
            if opportunity.get("pullback_confirmation"):
                item["pullback_confirmation"] = opportunity.get("pullback_confirmation")
    except Exception:
        pass
    scanned_at = datetime.utcnow().isoformat() + "Z"
    with _research_signal_state_lock:
        for symbol in tickers:
            _latest_event_signals.pop(symbol, None)
        for item in signals:
            symbol = str(item.get("symbol") or "").upper()
            if symbol:
                _latest_event_signals[symbol] = {**item, "scan_time": scanned_at}
        _latest_event_scans[payload.universe.strip().lower()] = {
            "scan_time": scanned_at, "run_id": run_id, "symbols": tickers,
        }
        _save_research_signal_state()
    report_path = run_dir / "artifacts" / "event_driven_report.md"
    report_markdown = report_path.read_text(encoding="utf-8", errors="replace") if report_path.exists() else None
    elapsed = time.time() - started

    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "status": "success",
                "elapsed_seconds": round(elapsed, 3),
                "event_count": int(summary.get("events", 0) or 0),
                "signal_count": int(summary.get("signals", len(signals)) or 0),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return EventDrivenScanResponse(
        status="success",
        run_id=run_id,
        elapsed_seconds=elapsed,
        event_count=int(summary.get("events", 0) or 0),
        signal_count=int(summary.get("signals", len(signals)) or 0),
        signals=signals,
        report_markdown=report_markdown,
        run_directory=str(run_dir),
        artifacts=_event_scan_artifacts(run_dir),
        report_pdf_url=f"/runs/{run_id}/report.pdf",
        detail_url=f"/runs/{run_id}",
        calibration=calibration_meta.model_dump() if hasattr(calibration_meta, "model_dump") else calibration_meta,
    )


def _resolve_event_csv(payload: EventImpactValidationRequest) -> tuple[str, Path]:
    if payload.run_id:
        _validate_path_param(payload.run_id, "run_id")
        run_dir = RUNS_DIR / payload.run_id
        if not run_dir.exists():
            raise HTTPException(status_code=404, detail=f"Run {payload.run_id} not found")
        events_csv = run_dir / "data" / "events.csv"
        if not events_csv.exists():
            legacy = run_dir / "data" / "event_data.csv"
            events_csv = legacy if legacy.exists() else events_csv
        if not events_csv.exists():
            historical = run_dir / "data" / "historical_events.csv"
            events_csv = historical if historical.exists() else events_csv
        if not events_csv.exists():
            raise HTTPException(status_code=404, detail=f"No events csv found for run {payload.run_id}")
        return payload.run_id, events_csv

    if payload.events_csv:
        candidate = (AGENT_DIR / payload.events_csv).resolve() if not Path(payload.events_csv).is_absolute() else Path(payload.events_csv).resolve()
        try:
            candidate.relative_to(AGENT_DIR.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="events_csv must be under agent directory") from exc
        if not candidate.exists():
            raise HTTPException(status_code=404, detail="events_csv not found")
        run_id = f"event_validation_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        return run_id, candidate

    raise HTTPException(status_code=400, detail="run_id or events_csv is required")


def _run_event_impact_validation_sync(payload: EventImpactValidationRequest) -> EventImpactValidationResponse:
    started = time.time()
    run_id, events_csv = _resolve_event_csv(payload)
    run_dir = RUNS_DIR / run_id
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    validator_path = AGENT_DIR / "scripts" / "event_driven_sp500" / "event_impact_validator.py"
    if not validator_path.exists():
        raise HTTPException(status_code=500, detail="event impact validator is missing")

    cmd = [
        _sys.executable,
        str(validator_path),
        "--events-csv",
        str(events_csv),
        "--output-dir",
        str(artifact_dir),
        "--benchmark",
        payload.benchmark,
        "--horizons",
        payload.horizons,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(AGENT_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=payload.timeout_seconds,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail=f"event validation timeout after {payload.timeout_seconds}s") from exc

    (run_dir / "logs").mkdir(exist_ok=True)
    (run_dir / "logs" / "event_validation_stdout.log").write_text(proc.stdout or "", encoding="utf-8")
    (run_dir / "logs" / "event_validation_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "event impact validation failed")[-2000:]
        raise HTTPException(status_code=500, detail=reason)

    summary = _load_json_file(artifact_dir / "event_impact_summary.json") or {}
    report_path = artifact_dir / "event_impact_report.md"
    report_markdown = report_path.read_text(encoding="utf-8", errors="replace") if report_path.exists() else None
    validated = int(summary.get("event_count", 0) or 0)
    elapsed = time.time() - started
    state = _load_json_file(run_dir / "state.json") or {}
    state.update(
        {
            "status": state.get("status", "success"),
            "event_impact_validated": True,
            "event_impact_elapsed_seconds": round(elapsed, 3),
            "validated_events": validated,
        }
    )
    (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return EventImpactValidationResponse(
        status="success",
        run_id=run_id,
        elapsed_seconds=elapsed,
        validated_events=validated,
        summary=summary,
        report_markdown=report_markdown,
        artifacts=_event_scan_artifacts(run_dir),
    )


def _run_historical_event_collect_sync(payload: HistoricalEventCollectRequest) -> HistoricalEventCollectResponse:
    started = time.time()
    run_id = f"event_history_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    run_dir = RUNS_DIR / run_id
    (run_dir / "data").mkdir(parents=True, exist_ok=True)
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)

    collector_path = AGENT_DIR / "scripts" / "event_driven_sp500" / "gdelt_historical_collector.py"
    if not collector_path.exists():
        raise HTTPException(status_code=500, detail="GDELT historical collector is missing")

    tickers = [_safe_event_ticker(t) for t in (payload.tickers or []) if str(t).strip()]
    req_record = payload.model_dump()
    req_record["prompt"] = "GDELT historical event collection for quantitative validation"
    req_record["created_at"] = datetime.utcnow().isoformat() + "Z"
    (run_dir / "req.json").write_text(json.dumps(req_record, ensure_ascii=False, indent=2), encoding="utf-8")

    cmd = [
        _sys.executable,
        str(collector_path),
        "--output-dir",
        str(run_dir),
        "--start-date",
        payload.start_date,
        "--end-date",
        payload.end_date,
        "--max-records-per-ticker",
        str(payload.max_records_per_ticker),
        "--min-relevance",
        str(payload.min_relevance),
        "--workers",
        str(payload.workers),
        "--sleep",
        str(payload.sleep_seconds),
        "--cache-ttl-minutes",
        str(payload.cache_ttl_minutes),
    ]
    if tickers:
        cmd.extend(["--tickers", ",".join(tickers)])
    elif payload.limit:
        cmd.extend(["--limit", str(payload.limit)])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(AGENT_DIR / "scripts" / "event_driven_sp500"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=payload.timeout_seconds,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail=f"historical event collection timeout after {payload.timeout_seconds}s") from exc

    (run_dir / "logs" / "historical_collect_stdout.log").write_text(proc.stdout or "", encoding="utf-8")
    (run_dir / "logs" / "historical_collect_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "historical event collection failed")[-2000:]
        raise HTTPException(status_code=500, detail=reason)

    summary = {}
    try:
        summary = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception:
        summary = {}
    events_csv = run_dir / "data" / "historical_events.csv"
    validation = None
    if payload.validate and events_csv.exists() and int(summary.get("events", 0) or 0) > 0:
        validation = _run_event_impact_validation_sync(
            EventImpactValidationRequest(
                run_id=None,
                events_csv=str(events_csv.relative_to(AGENT_DIR)),
                benchmark="SPY",
                horizons="1,3,5,10",
                timeout_seconds=payload.timeout_seconds,
            )
        )
        validation_artifacts = RUNS_DIR / validation.run_id / "artifacts"
        if validation_artifacts.exists():
            for path in validation_artifacts.glob("event_impact_*"):
                if path.is_file():
                    shutil.copy2(path, artifact_dir / path.name)

    elapsed = time.time() - started
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "status": "success",
                "elapsed_seconds": round(elapsed, 3),
                "historical_event_count": int(summary.get("events", 0) or 0),
                "validated": bool(validation),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return HistoricalEventCollectResponse(
        status="success",
        run_id=run_id,
        elapsed_seconds=elapsed,
        event_count=int(summary.get("events", 0) or 0),
        run_directory=str(run_dir),
        events_csv=str(events_csv),
        validation=validation.model_dump() if validation is not None else None,
        artifacts=_event_scan_artifacts(run_dir),
    )


def _run_historical_batch_collect_sync(payload: HistoricalBatchCollectRequest) -> HistoricalBatchCollectResponse:
    started = time.time()
    if payload.resume_run_id:
        _validate_path_param(payload.resume_run_id, "resume_run_id")
        run_id = payload.resume_run_id
        run_dir = RUNS_DIR / run_id
        if not run_dir.exists():
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        resume = True
    else:
        run_id = f"event_history_batch_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        run_dir = RUNS_DIR / run_id
        resume = False

    (run_dir / "data").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    batch_path = AGENT_DIR / "scripts" / "event_driven_sp500" / "historical_batch_runner.py"
    if not batch_path.exists():
        raise HTTPException(status_code=500, detail="historical batch runner is missing")

    tickers = [_safe_event_ticker(t) for t in (payload.tickers or []) if str(t).strip()]
    req_record = payload.model_dump()
    req_record["prompt"] = "Batch GDELT historical event collection for quantitative validation"
    req_record["created_at"] = datetime.utcnow().isoformat() + "Z"
    (run_dir / "req.json").write_text(json.dumps(req_record, ensure_ascii=False, indent=2), encoding="utf-8")

    cmd = [
        _sys.executable,
        str(batch_path),
        "--output-dir",
        str(run_dir),
        "--start-date",
        payload.start_date,
        "--end-date",
        payload.end_date,
        "--batch-size",
        str(payload.batch_size),
        "--max-batches",
        str(payload.max_batches),
        "--max-records-per-ticker",
        str(payload.max_records_per_ticker),
        "--min-relevance",
        str(payload.min_relevance),
        "--workers",
        str(payload.workers),
        "--sleep",
        str(payload.sleep_seconds),
        "--batch-sleep",
        str(payload.batch_sleep_seconds),
        "--cache-ttl-minutes",
        str(payload.cache_ttl_minutes),
    ]
    if tickers:
        cmd.extend(["--tickers", ",".join(tickers)])
    elif payload.limit:
        cmd.extend(["--limit", str(payload.limit)])
    if resume:
        cmd.append("--resume")
    if payload.validate:
        cmd.append("--validate")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(AGENT_DIR / "scripts" / "event_driven_sp500"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=payload.timeout_seconds,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail=f"historical batch collection timeout after {payload.timeout_seconds}s") from exc

    (run_dir / "logs" / "historical_batch_stdout.log").write_text(proc.stdout or "", encoding="utf-8")
    (run_dir / "logs" / "historical_batch_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "historical batch collection failed")[-2000:]
        raise HTTPException(status_code=500, detail=reason)

    summary = {}
    try:
        summary = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception:
        summary = {}
    elapsed = time.time() - started
    state = _load_json_file(run_dir / "state.json") or {}
    failed_batches = int(summary.get("failed_batches", 0) or 0)
    response_status = "partial" if failed_batches else "success"
    state.update(
        {
            "status": response_status,
            "elapsed_seconds": round(elapsed, 3),
            "historical_batch_events": int(summary.get("events", 0) or 0),
            "processed_this_run": int(summary.get("processed_this_run", 0) or 0),
            "successful_batches": int(summary.get("successful_batches", 0) or 0),
            "failed_batches": failed_batches,
            "empty_batches": int(summary.get("empty_batches", 0) or 0),
        }
    )
    (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    events_csv = run_dir / "data" / "historical_events.csv"
    batch_state = run_dir / "artifacts" / "historical_batch_state.json"
    return HistoricalBatchCollectResponse(
        status=response_status,
        run_id=run_id,
        elapsed_seconds=elapsed,
        event_count=int(summary.get("events", 0) or 0),
        processed_this_run=int(summary.get("processed_this_run", 0) or 0),
        successful_batches=int(summary.get("successful_batches", 0) or 0),
        failed_batches=failed_batches,
        empty_batches=int(summary.get("empty_batches", 0) or 0),
        run_directory=str(run_dir),
        events_csv=str(events_csv),
        state_json=str(batch_state),
        artifacts=_event_scan_artifacts(run_dir),
    )


# ============================================================================
# API Endpoints
# ============================================================================

_launch_signal_jobs: Dict[str, Dict[str, Any]] = {}
_RESEARCH_SIGNAL_STATE_PATH = RUNS_DIR / "_research_signal_state.json"
_research_signal_state_lock = threading.RLock()


def _load_research_signal_state() -> Dict[str, Dict[str, Dict[str, Any]]]:
    try:
        data = json.loads(_RESEARCH_SIGNAL_STATE_PATH.read_text(encoding="utf-8"))
        return {
            "launch": dict(data.get("launch") or {}),
            "event": dict(data.get("event") or {}),
            "event_scans": dict(data.get("event_scans") or {}),
        }
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return {"launch": {}, "event": {}, "event_scans": {}}


_research_signal_state = _load_research_signal_state()
_latest_launch_signals: Dict[str, Dict[str, Any]] = _research_signal_state["launch"]
_latest_event_signals: Dict[str, Dict[str, Any]] = _research_signal_state["event"]
_latest_event_scans: Dict[str, Dict[str, Any]] = _research_signal_state.get("event_scans", {})


def _save_research_signal_state() -> None:
    with _research_signal_state_lock:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = _RESEARCH_SIGNAL_STATE_PATH.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(
                {
                    "launch": _latest_launch_signals,
                    "event": _latest_event_signals,
                    "event_scans": _latest_event_scans,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temp_path.replace(_RESEARCH_SIGNAL_STATE_PATH)


def _store_launch_result(universe, result, symbols=None, opportunity_map=None, replace=False):
    # All writers share the serialization lock; concurrent JSON iteration is unsafe.
    with _research_signal_state_lock:
        if replace:
            for symbol, item in list(_latest_launch_signals.items()):
                if item.get("scan_universe") == universe:
                    _latest_launch_signals.pop(symbol, None)
        for item in result.get("signals", []):
            _latest_launch_signals[str(item.get("symbol") or "")] = {
                **item, "scan_time": result.get("scan_time"), "scan_universe": universe,
            }
        _persist_launch_peer_mappings(
            universe=universe, result=result, symbols=symbols,
            opportunity_map=opportunity_map or {},
        )
        _save_research_signal_state()


def _launch_signal_items():
    with _research_signal_state_lock:
        return list(copy.deepcopy(_latest_launch_signals).items())


def _persist_launch_peer_mappings(
    *,
    universe: str,
    result: Dict[str, Any],
    symbols: Optional[List[str]],
    opportunity_map: Dict[str, Dict[str, Any]],
) -> None:
    """Persist peer mappings even when the symbol has no active launch signal."""
    target_symbols = {str(symbol or "").upper() for symbol in (symbols or []) if str(symbol or "").strip()}
    scan_time = result.get("scan_time")
    for symbol, mapping in dict(((result.get("peer_earnings") or {}).get("target_mapping") or {})).items():
        symbol = str(symbol or "").upper()
        if target_symbols and symbol not in target_symbols:
            continue
        existing = _latest_launch_signals.get(symbol)
        if existing and existing.get("scan_universe") == universe:
            existing["peer_mapping"] = mapping
            existing["scan_time"] = scan_time
            continue
        _latest_launch_signals[symbol] = {
            "symbol": symbol,
            "signal": "neutral",
            "signal_cn": "等待启动",
            "launch_score": 0.0,
            "last_price": (opportunity_map.get(symbol) or {}).get("spot", 0),
            "ret_5d": 0.0,
            "ret_10d": 0.0,
            "ret_20d": 0.0,
            "vol_ratio": 0.0,
            "rsi_14": 50.0,
            "bb_position": 0.5,
            "roc_10": 0.0,
            "breakout_20": 0.0,
            "reason": "已建立指数内同行接力映射，尚未出现主动启动信号。",
            "peer_mapping": mapping,
            "scan_time": scan_time,
            "scan_universe": universe,
        }


def _run_launch_signal_job(job_id: str, payload: LaunchSignalScanRequest) -> None:
    job = _launch_signal_jobs[job_id]

    def progress(phase: str, pct: float) -> None:
        job["events"].append({"event": "progress", "data": {"phase": phase, "pct": pct}})

    try:
        symbols = payload.symbols
        opportunity_map: Dict[str, Dict[str, Any]] = {}
        if not symbols and payload.use_stock_observation_pool:
            observation = _stock_signal_observation_pool(payload.universe, limit=300)
            symbols = [item["ticker"] for item in observation]
            opportunity_map = {item["ticker"]: item for item in observation}
            progress(f"读取实股观察池：{len(symbols)} 只候选", 0.04)
        result = scan_launch_signals(
            universe=payload.universe,
            symbols=symbols,
            threshold=payload.threshold,
            top=payload.top,
            progress=progress,
            opportunity_map=opportunity_map,
            scan_mode=payload.scan_mode,
            peer_group=payload.peer_group,
            peer_cache_path=RUNS_DIR / "_peer_earnings_signal_cache.json",
        )
        result["input_source"] = "stock_observation_pool" if opportunity_map else ("custom" if symbols else "research_universe")
        _store_launch_result(payload.universe, result, symbols, opportunity_map)
        job["events"].append({"event": "result", "data": result})
    except Exception as exc:
        job["events"].append({"event": "error", "data": {"error": str(exc)}})
    finally:
        job["events"].append({"event": "done", "data": {"status": "done"}})
        job["done"] = True


@app.post("/launch-signal/scan", dependencies=[Depends(require_auth)])
async def scan_launch_signal(payload: LaunchSignalScanRequest):
    """Run the technical launch scanner synchronously."""
    symbols = payload.symbols
    opportunity_map: Dict[str, Dict[str, Any]] = {}
    if not symbols and payload.use_stock_observation_pool:
        observation = _stock_signal_observation_pool(payload.universe, limit=300)
        symbols = [item["ticker"] for item in observation]
        opportunity_map = {item["ticker"]: item for item in observation}
    result = await asyncio.to_thread(
        scan_launch_signals,
        payload.universe,
        symbols,
        payload.threshold,
        payload.top,
        None,
        opportunity_map,
        payload.scan_mode,
        payload.peer_group,
        RUNS_DIR / "_peer_earnings_signal_cache.json",
    )
    result["input_source"] = "stock_observation_pool" if opportunity_map else ("custom" if symbols else "research_universe")
    _store_launch_result(payload.universe, result, symbols, opportunity_map)
    return result


@app.post("/launch-signal/scan-async", dependencies=[Depends(require_auth)])
async def scan_launch_signal_async(payload: LaunchSignalScanRequest):
    """Start the technical launch scanner and stream progress separately."""
    job_id = f"launch_job_{uuid.uuid4().hex[:10]}"
    _launch_signal_jobs[job_id] = {"events": [], "done": False}
    threading.Thread(target=_run_launch_signal_job, args=(job_id, payload), daemon=True).start()
    return {"job_id": job_id}


@app.get("/launch-signal/scan/{job_id}/stream", dependencies=[Depends(require_auth)])
async def stream_launch_signal(job_id: str):
    """Stream launch-scanner progress for the React page."""
    job = _launch_signal_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Launch signal job {job_id} not found")

    async def event_stream():
        cursor = 0
        while True:
            while cursor < len(job["events"]):
                item = job["events"][cursor]
                cursor += 1
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
            if job["done"] and cursor >= len(job["events"]):
                break
            await asyncio.sleep(0.35)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/launch-signal/models", dependencies=[Depends(require_auth)])
async def list_launch_signal_models():
    """Return restored launch-signal models. The current scanner is auditable heuristic mode."""
    return {"models": []}


@app.get("/launch-signal/peer-groups", dependencies=[Depends(require_auth)])
async def list_launch_signal_peer_groups():
    """Return curated narrow-business peer groups for earnings-lag research."""
    return {"catalog": peer_catalog_summary(), "groups": list_peer_groups()}


@app.get("/launch-signal/peer-history", dependencies=[Depends(require_auth)])
async def list_launch_signal_peer_history(
    limit: int = Query(30, ge=1, le=500),
):
    """Return archived peer-earnings snapshots for future calibration audits."""
    history_path = RUNS_DIR / "_peer_earnings_signal_history.jsonl"
    return {"snapshots": load_peer_earnings_history(history_path, limit=limit)}


@app.get("/launch-signal/peer-history/summary", dependencies=[Depends(require_auth)])
async def launch_signal_peer_history_summary(
    limit: int = Query(200, ge=2, le=2000),
):
    """Return a lightweight realized-outcome audit for archived peer relay signals."""
    history_path = RUNS_DIR / "_peer_earnings_signal_history.jsonl"
    return summarize_peer_earnings_history(history_path, limit=limit)



@app.get("/api/signals/pre-earnings", dependencies=[Depends(require_auth)])
async def get_pre_earnings_signals(
    group_id: str = Query("all", description="Peer group ID to scan"),
    limit: int = Query(20, ge=1, le=100),
):
    """Get the latest cached pre-earnings expectation revision signals.

    Page reads must not trigger a live scan.  Use
    ``POST /api/signals/pre-earnings/scan`` for an explicit refresh.
    """
    try:
        cache_path = RUNS_DIR / "_pre_earnings_signal_cache.json"
        result = _load_json_file(cache_path) or {
            "signals": [],
            "scan_time": None,
            "status": "cache_miss",
            "message": "no cached pre-earnings scan; trigger POST /api/signals/pre-earnings/scan to refresh",
        }
        if group_id and group_id != "all":
            result["signals"] = [
                item for item in result.get("signals", [])
                if str(item.get("group_id") or item.get("peer_group") or "all") == group_id
            ]
        _store_launch_result("pre_earnings_revision", result)
        result["signals"] = result.get("signals", [])[:limit]
        result["data_access_mode"] = "page_read_cache_only"
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"cache query failed: {str(e)[:200]}")


@app.get("/api/signals/pre-earnings/{symbol}", dependencies=[Depends(require_auth)])
async def get_pre_earnings_signal_detail(symbol: str):
    """Get pre-earnings signal detail for a symbol."""
    from pre_earnings_snapshot_repository import PreEarningsSnapshotRepository
    try:
        repo = PreEarningsSnapshotRepository()
        snapshot = repo.get_latest_snapshot(symbol.upper())
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"no snapshot for {symbol}")
        return snapshot
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"query failed: {str(e)[:200]}")


@app.post("/api/signals/pre-earnings/scan", dependencies=[Depends(require_auth)])
async def trigger_pre_earnings_scan(
    background_tasks: BackgroundTasks,
    group_id: str = Query("all", description="Peer group ID to scan"),
):
    """Trigger pre-earnings signal scan asynchronously."""
    import uuid as _uuid
    job_id = _uuid.uuid4().hex[:12]
    def _run_scan():
        from peer_earnings_signal_service import scan_pre_earnings_signals
        from pre_earnings_snapshot_repository import PreEarningsSnapshotRepository
        try:
            result = scan_pre_earnings_signals(
                group_id=group_id,
                cache_path=RUNS_DIR / "_pre_earnings_signal_cache.json",
            )
            repo = PreEarningsSnapshotRepository()
            for signal in result.get("signals", []):
                repo.save_snapshot(signal)
        except Exception:
            pass
    background_tasks.add_task(_run_scan)
    return {"job_id": job_id, "status": "started", "message": "scan started"}


@app.post("/launch-signal/calibrate-async", dependencies=[Depends(require_auth)])
async def calibrate_launch_signal_async():
    """Prevent the UI from implying that the missing historical trainer still exists."""
    raise HTTPException(status_code=501, detail="启动信号历史校准器尚未恢复；当前可使用可审计的量价启动扫描。")


@app.get("/research-universes", dependencies=[Depends(require_auth)])
async def get_research_universes():
    """Return the shared stock-pool catalog used by research modules."""
    started = time.perf_counter()
    cache_hit = bool(_SUMMARY_SNAPSHOT_ENABLED and _research_universe_rank_cache.get("expires_at", 0.0) > time.time())
    universes = _ranked_research_universes()
    serialize_started = time.perf_counter()
    response = {
        "groups": list_research_universe_groups(),
        "universes": universes,
        "ranking_note": "股票池按近期研究胜率口径排序：历史验证胜率优先，其次统一研究概率，再看风险收益比与可执行性。",
        "snapshot": {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "ttl_seconds": _SUMMARY_SNAPSHOT_TTL_SECONDS,
            "cache_hit": cache_hit,
        },
    }
    _perf_log(
        "research_universes",
        load_latest_snapshot_ms=round((serialize_started - started) * 1000, 2),
        query_candidates_ms=0 if cache_hit else round((serialize_started - started) * 1000, 2),
        aggregate_signals_ms=0,
        fetch_external_data_ms=0,
        serialize_response_ms=round((time.perf_counter() - serialize_started) * 1000, 2),
        candidate_count=sum(int((item.get("recent_win") or {}).get("candidate_count", 0) or 0) for item in universes),
        db_query_count=0,
        external_call_count=0,
        cache_hit=cache_hit,
        cache_miss=not cache_hit,
        total_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    return response


@app.get("/research-universes/{universe}", dependencies=[Depends(require_auth)])
async def get_research_universe(
    universe: str,
    refresh: bool = Query(False, description="Refresh live constituents instead of using the short-lived cache."),
    limit: int = Query(0, ge=0, le=1000, description="Optional preview limit."),
):
    """Resolve one research universe for inspection and audit."""
    try:
        resolved = await asyncio.to_thread(resolve_research_universe, universe, refresh)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if limit:
        resolved["tickers"] = resolved["tickers"][:limit]
        resolved["ticker_count"] = len(resolved["tickers"])
    return resolved


def _safe_watchlist_symbol(symbol: str) -> str:
    safe = str(symbol or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-]{1,16}", safe):
        raise HTTPException(status_code=400, detail=f"invalid symbol: {symbol}")
    return safe


def _watchlist_symbol_metrics(symbol: str, item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Best-effort compact metrics for the user-maintained watchlist page."""
    safe = _safe_watchlist_symbol(symbol)
    row = _single_stock_row_from_snapshot(safe) or {}
    out: Dict[str, Any] = {
        "symbol": safe,
        "name": (item or {}).get("name") or row.get("company_name") or row.get("name") or safe,
        "note": (item or {}).get("note") or "",
        "enabled": bool((item or {}).get("enabled", True)),
        "source_universe_ids": ["watchlist"],
        "source_universe_labels": ["自选池"],
        "benchmark": "QQQ",
        "benchmark_source_universe": "watchlist",
        "current_price": row.get("current_price") or row.get("spot") or row.get("last_price"),
        "market_cap": row.get("market_cap"),
        "pe": row.get("trailing_pe") or row.get("pe") or row.get("forward_pe"),
        "sector": row.get("sector"),
        "industry": row.get("industry"),
        "liquidity": row.get("liquidity") or {},
        "research_probability": row.get("unified_probability") or row.get("research_probability"),
        "overall_score": row.get("overall_score") or row.get("sort_score"),
        "pullback_rejection_status": row.get("pullback_rejection_status"),
        "pullback_confirmation_status": row.get("pullback_confirmation_status"),
        "gex_level": row.get("gex_level"),
        "gex_regime": row.get("gex_regime"),
        "detail_url": f"/single-stock-overnight?symbol={safe}",
    }
    try:
        earnings = get_next_earnings(safe) or {}
        out["earnings"] = earnings if earnings.get("available") else {"available": False}
    except Exception as exc:
        out["earnings"] = {"available": False, "reason": str(exc)[-200:]}
    try:
        stock, stock_source = get_daily_history(safe, period="1y")
        bench, bench_source = get_daily_history("QQQ", period="1y")
        if stock is not None and not stock.empty and "Close" in stock:
            close = pd.to_numeric(stock["Close"], errors="coerce").dropna()
            if not close.empty:
                current = float(close.iloc[-1])
                prev = float(close.iloc[-2]) if len(close) >= 2 else current
                out["current_price"] = current
                out["day_change_pct"] = current / prev - 1.0 if prev > 0 else None
                if len(close) >= 21:
                    out["return_20d"] = current / float(close.iloc[-21]) - 1.0
                if len(close) >= 61:
                    out["return_60d"] = current / float(close.iloc[-61]) - 1.0
                out["ma20"] = float(close.tail(20).mean()) if len(close) >= 20 else None
                out["ma50"] = float(close.tail(50).mean()) if len(close) >= 50 else None
                out["ma200"] = float(close.tail(200).mean()) if len(close) >= 200 else None
                out["trend_status"] = (
                    "uptrend" if out.get("ma50") and out.get("ma200") and current > out["ma50"] > out["ma200"]
                    else "watch"
                )
            out["distribution_risk"] = detect_distribution_risk(stock)
        if bench is not None and not bench.empty and "Close" in bench:
            bclose = pd.to_numeric(bench["Close"], errors="coerce").dropna()
            if len(bclose) >= 61 and out.get("return_60d") is not None:
                bench_60 = float(bclose.iloc[-1]) / float(bclose.iloc[-61]) - 1.0
                out["benchmark_return_60d"] = bench_60
                out["relative_to_qqq_60d"] = float(out["return_60d"]) - bench_60
        out["data_sources"] = {"stock": stock_source, "benchmark": bench_source}
    except Exception as exc:
        out["market_data_error"] = str(exc)[-300:]
    try:
        info = yf.Ticker(safe).info or {}
        out["name"] = out.get("name") if out.get("name") != safe else (info.get("shortName") or info.get("longName") or safe)
        out["market_cap"] = out.get("market_cap") or info.get("marketCap")
        out["pe"] = out.get("pe") or info.get("trailingPE") or info.get("forwardPE")
        out["sector"] = out.get("sector") or info.get("sector")
        out["industry"] = out.get("industry") or info.get("industry")
    except Exception:
        pass
    try:
        alpha = validate_symbol_overnight_alpha(
            {
                "symbol": safe,
                "source_universe_ids": ["watchlist"],
                "pullback_rejection_status": out.get("pullback_rejection_status"),
                "pullback_confirmation_status": out.get("pullback_confirmation_status"),
            },
            period="2y",
            min_edge=0.001,
        )
        out["overnight_alpha"] = alpha
        stats = alpha.get("stats") or {}
        out["alpha_win_rate"] = stats.get("alpha_win_rate")
        out["mean_beta_adjusted_alpha"] = stats.get("mean_beta_adjusted_alpha")
    except Exception as exc:
        out["overnight_alpha"] = {"available": False, "reason": str(exc)[-300:], "benchmark": "QQQ"}
    return out


_WATCHLIST_METRICS_TTL_SECONDS = 30 * 60
_watchlist_metrics_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="watchlist-metrics")
_watchlist_metrics_inflight: set[str] = set()
_watchlist_metrics_lock = threading.Lock()


def _refresh_watchlist_metrics(symbol: str, item: Dict[str, Any]) -> None:
    try:
        row = _watchlist_symbol_metrics(symbol, item)
        cache_set(f"watchlist:metrics:v1:{symbol}", row, ttl_seconds=_WATCHLIST_METRICS_TTL_SECONDS)
    except Exception as exc:
        console.log(f"watchlist metrics refresh failed for {symbol}: {type(exc).__name__}")
    finally:
        with _watchlist_metrics_lock:
            _watchlist_metrics_inflight.discard(symbol)


def _schedule_watchlist_metrics(symbol: str, item: Dict[str, Any]) -> None:
    with _watchlist_metrics_lock:
        if symbol in _watchlist_metrics_inflight:
            return
        _watchlist_metrics_inflight.add(symbol)
    try:
        _watchlist_metrics_executor.submit(_refresh_watchlist_metrics, symbol, item)
    except RuntimeError:
        with _watchlist_metrics_lock:
            _watchlist_metrics_inflight.discard(symbol)


@app.get("/api/watchlist", dependencies=[Depends(require_auth)])
def get_watchlist(enrich: bool = Query(True), include_disabled: bool = Query(True)):
    items = user_watchlist_list(include_disabled=include_disabled)
    if not enrich:
        return {"universe": "watchlist", "label": "自选池", "benchmark": "QQQ", "items": items, "count": len(items)}
    rows = []
    pending_count = 0
    for item in items:
        symbol = str(item["symbol"]).upper()
        cached = cache_get(f"watchlist:metrics:v1:{symbol}")
        if not isinstance(cached, dict):
            pending_count += 1
            _schedule_watchlist_metrics(symbol, item)
        row = dict(cached) if isinstance(cached, dict) else {"symbol": symbol}
        row.update({
            "symbol": symbol,
            "name": item.get("name") or row.get("name") or symbol,
            "note": item.get("note") or "",
            "enabled": bool(item.get("enabled", True)),
            "source_universe_ids": ["watchlist"],
            "source_universe_labels": ["自选池"],
            "benchmark": "QQQ",
            "detail_url": f"/single-stock-overnight?symbol={symbol}",
        })
        rows.append(row)
    rows.sort(key=lambda row: (
        0 if row.get("enabled", True) else 1,
        -float(row.get("alpha_win_rate") or row.get("research_probability") or 0),
        str(row.get("symbol") or ""),
    ))
    return {
        "universe": "watchlist",
        "label": "自选池",
        "benchmark": "QQQ",
        "benchmark_label": "纳斯达克 / QQQ",
        "source_universe_ids": ["watchlist"],
        "source_universe_labels": ["自选池"],
        "count": len(rows),
        "items": rows,
        "refreshing": pending_count > 0,
        "pending_count": pending_count,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


@app.get("/api/watchlist/{symbol}", dependencies=[Depends(require_auth)])
async def get_watchlist_item(symbol: str):
    safe = _safe_watchlist_symbol(symbol)
    item = user_watchlist_get(safe)
    return {"symbol": safe, "exists": bool(item), "item": item}


@app.post("/api/watchlist/items", dependencies=[Depends(require_auth)])
async def add_watchlist_item(payload: WatchlistItemRequest):
    safe = _safe_watchlist_symbol(payload.symbol)
    item = user_watchlist_upsert(
        safe,
        name=payload.name,
        note=payload.note,
        enabled=payload.enabled,
        source="manual",
    )
    return {"status": "ok", "item": item}


@app.post("/api/watchlist/items/batch", dependencies=[Depends(require_auth)])
async def add_watchlist_items(payload: WatchlistBatchRequest):
    symbols: List[str] = []
    for raw in payload.symbols:
        for part in re.split(r"[\s,;，；]+", str(raw or "")):
            if part.strip():
                symbols.append(_safe_watchlist_symbol(part))
    written = []
    for symbol in dict.fromkeys(symbols):
        written.append(user_watchlist_upsert(symbol, note=payload.note, enabled=payload.enabled, source="manual"))
    return {"status": "ok", "count": len(written), "items": written}


@app.patch("/api/watchlist/items/{symbol}", dependencies=[Depends(require_auth)])
async def set_watchlist_item_enabled(symbol: str, payload: WatchlistEnabledRequest):
    safe = _safe_watchlist_symbol(symbol)
    item = user_watchlist_set_enabled(safe, payload.enabled)
    if item is None:
        raise HTTPException(status_code=404, detail=f"not in watchlist: {safe}")
    return {"status": "ok", "item": item}


@app.delete("/api/watchlist/items/{symbol}", dependencies=[Depends(require_auth)])
async def delete_watchlist_item(symbol: str):
    safe = _safe_watchlist_symbol(symbol)
    deleted = user_watchlist_delete(safe)
    return {"status": "ok", "deleted": deleted, "symbol": safe}


@app.post("/event-driven/sp500/scan", response_model=EventDrivenScanResponse, dependencies=[Depends(require_auth)])
async def scan_event_driven_sp500(payload: EventDrivenScanRequest):
    """Scan selected-universe news and return event-driven short-term launch candidates."""
    return await asyncio.to_thread(_run_event_driven_scan_sync, payload)


@app.get("/event-driven/calibrations", response_model=EventCalibrationListResponse, dependencies=[Depends(require_auth)])
async def list_event_driven_calibrations(
    usable_only: bool = Query(False, description="Only return calibrations marked usable."),
    min_events: int = Query(1, ge=1, le=10000, description="Minimum historical event count."),
):
    """List historical event-impact calibration summaries that can be reused by scans."""
    return EventCalibrationListResponse(
        calibrations=_list_event_calibrations(usable_only=usable_only, min_events=min_events)
    )


@app.get("/event-radar-tool", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def event_radar_tool_page():
    """Standalone event-driven research UI that does not require rebuilding the SPA."""
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>事件驱动选股雷达 - easymoneysniper</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1117; --panel:#121b24; --panel2:#172331; --line:#253443; --text:#e8eef5; --muted:#9fb0c1; --accent:#64a3ff; --good:#30d158; --bad:#ff5c7a; --warn:#ffd166; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }
    main { max-width: 1320px; margin: 0 auto; padding: 28px; }
    header { display:flex; align-items:flex-end; justify-content:space-between; gap:16px; border-bottom:1px solid var(--line); padding-bottom:18px; margin-bottom:18px; }
    h1 { margin: 0; font-size: 26px; letter-spacing: .2px; }
    p { margin: 8px 0 0; color: var(--muted); font-size: 14px; line-height: 1.6; }
    .grid { display:grid; grid-template-columns: 1.2fr .8fr; gap:16px; }
    .card { background: var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; }
    label { display:block; color:var(--muted); font-size:12px; margin-bottom:6px; }
    input, select, textarea { width:100%; border:1px solid var(--line); background:#0d151e; color:var(--text); border-radius:6px; padding:10px 11px; font-size:14px; }
    textarea { min-height:72px; resize:vertical; }
    button, a.button { border:0; border-radius:6px; padding:10px 14px; background:var(--accent); color:#07101b; font-weight:700; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; gap:8px; }
    button.secondary { background:var(--panel2); color:var(--text); border:1px solid var(--line); }
    button:disabled { opacity:.55; cursor:not-allowed; }
    .controls { display:grid; grid-template-columns: repeat(4, 1fr); gap:12px; }
    .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    .stats { display:grid; grid-template-columns: repeat(4, 1fr); gap:10px; margin-top:14px; }
    .stat { background:#0d151e; border:1px solid var(--line); border-radius:8px; padding:12px; min-width:0; }
    .stat span { display:block; color:var(--muted); font-size:12px; }
    .stat b { display:block; margin-top:5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    table { width:100%; border-collapse:collapse; min-width:1120px; }
    th, td { padding:11px 12px; border-top:1px solid var(--line); text-align:left; vertical-align:top; font-size:13px; }
    th { color:var(--muted); font-size:12px; background:#0d151e; position:sticky; top:0; }
    .table-wrap { overflow:auto; max-height: 58vh; border:1px solid var(--line); border-radius:8px; }
    .badge { display:inline-flex; padding:4px 8px; border-radius:999px; font-size:12px; font-weight:700; background:#243244; color:#cfe0f5; }
    .strong_watch { background:rgba(48,209,88,.12); color:var(--good); }
    .watch { background:rgba(100,163,255,.14); color:#8fc0ff; }
    .avoid { background:rgba(255,92,122,.13); color:var(--bad); }
    .right { text-align:right; }
    .muted { color:var(--muted); }
    .good { color:var(--good); }
    .bad { color:var(--bad); }
    .notice { border-color:rgba(255,209,102,.35); background:rgba(255,209,102,.08); color:#ffe6a3; }
    @media (max-width: 900px) { main { padding:16px; } header, .grid { display:block; } .card { margin-bottom:14px; } .controls, .stats { grid-template-columns: repeat(2, 1fr); } }
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>事件驱动选股雷达</h1>
      <p>按统一研究股票池采集最新资讯，结合历史事件影响校准、价格趋势与成交量确认，生成短期启动候选。结果用于研究筛选，不等同于下单建议。</p>
    </div>
    <div class="row">
      <button class="secondary" id="refreshBtn">刷新校准</button>
      <button id="scanBtn">运行扫描</button>
    </div>
  </header>

  <section class="grid">
    <div class="card">
      <h3>扫描参数</h3>
      <div class="controls">
        <div><label>股票池</label><select id="universe"></select></div>
        <div><label>扫描数量上限</label><input id="limit" type="number" min="1" max="1000" value="50"></div>
        <div><label>Top 候选</label><input id="top" type="number" min="1" max="100" value="20"></div>
        <div><label>每票新闻数</label><input id="news" type="number" min="1" max="20" value="3"></div>
      </div>
      <div style="margin-top:12px"><label>自动校准</label><select id="auto"><option value="true">开启</option><option value="false">关闭</option></select></div>
      <div class="row" style="margin-top:12px">
        <label style="display:flex;gap:8px;align-items:center;margin:0;color:var(--muted)">
          <input id="llmScore" type="checkbox" style="width:auto"> 使用平台 DeepSeek 对新闻事件做语义复核
        </label>
      </div>
      <div style="margin-top:12px"><label>指定标的，可留空使用所选股票池</label><textarea id="tickers" placeholder="例如：AAPL, NVDA, MSFT"></textarea></div>
    </div>

    <div class="card">
      <h3>历史影响校准</h3>
      <label>校准模型</label>
      <select id="calibration"><option value="">自动选择最佳可用模型</option></select>
      <div class="stats">
        <div class="stat"><span>样本事件</span><b id="calEvents">--</b></div>
        <div class="stat"><span>主窗口</span><b id="calHorizon">--</b></div>
        <div class="stat"><span>IC</span><b id="calIc">--</b></div>
        <div class="stat"><span>Beta</span><b id="calBeta">--</b></div>
      </div>
    </div>
  </section>

  <section class="stats" id="runStats" style="display:none">
    <div class="stat"><span>Run ID</span><b id="runId">--</b></div>
    <div class="stat"><span>采集事件</span><b id="eventCount">--</b></div>
    <div class="stat"><span>候选数量</span><b id="signalCount">--</b></div>
    <div class="stat"><span>耗时</span><b id="elapsed">--</b></div>
  </section>

  <section class="card" style="margin-top:16px">
    <div class="row" style="justify-content:space-between; margin-bottom:10px">
      <h3 style="margin:0">短期启动信号排名</h3>
      <div class="row"><span class="muted" id="status">等待运行</span><a class="button" id="pdfLink" href="#" style="display:none" target="_blank">下载报告</a></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>排名</th><th>标的</th><th>信号</th><th class="right">启动分</th><th class="right">预期异常收益</th><th class="right">现价</th><th class="right">5日</th><th class="right">20日</th><th class="right">量比</th><th>核心原因</th><th>顶部事件</th>
        </tr></thead>
        <tbody id="tbody"><tr><td colspan="11" class="muted" style="text-align:center;padding:34px">点击“运行扫描”开始。</td></tr></tbody>
      </table>
    </div>
  </section>

  <section class="grid" style="margin-top:16px">
    <div class="card">
      <h3>历史资讯验证 / 定量校准</h3>
      <p>小批量拉取历史新闻，自动计算事件分数与后续异常收益的 IC、Beta、胜率方向命中率，并沉淀为后续实时扫描可复用的校准模型。</p>
      <div class="controls" style="margin-top:12px">
        <div><label>历史标的</label><input id="histTickers" value="AAPL,NVDA,MSFT,TSLA,AMZN"></div>
        <div><label>开始日期</label><input id="histStart" value="2026-03-01"></div>
        <div><label>结束日期</label><input id="histEnd" value="2026-04-30"></div>
        <div><label>每批标的数</label><input id="histBatch" type="number" min="1" max="20" value="2"></div>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="secondary" id="historyBtn">运行一批历史验证</button>
        <span class="muted" id="historyStatus">建议先小批量运行，避免 GDELT 限流。</span>
      </div>
    </div>
    <div class="card">
      <h3>验证产物</h3>
      <div class="stats">
        <div class="stat"><span>历史 Run</span><b id="histRun">--</b></div>
        <div class="stat"><span>事件数</span><b id="histEvents">--</b></div>
        <div class="stat"><span>成功批次</span><b id="histOk">--</b></div>
        <div class="stat"><span>失败批次</span><b id="histFail">--</b></div>
      </div>
      <p>跑完后会自动刷新上方“历史影响校准”列表。若数据源限流，页面会保留已成功批次，可用 batch run id 继续恢复。</p>
    </div>
  </section>

  <section class="card notice" style="margin-top:16px">
    说明：该模型衡量新闻事件与后续异常收益之间的统计关系。新闻覆盖、发布时间、样本数量和市场环境都会影响结果，请作为研究报告与候选池筛选使用。
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
let calibrations = [];
function universeOptions(items){let groups=[];(items||[]).forEach(x=>{let key=x.group_label||"其他股票池",g=groups.find(y=>y.key===key);if(!g){g={key,items:[]};groups.push(g)}g.items.push(x)});return groups.map(g=>`<optgroup label="${g.key}">${g.items.map(x=>`<option value="${x.universe}">${x.label} · ${x.tier} · 成本${x.run_cost||"中"}</option>`).join("")}</optgroup>`).join("")}
async function loadUniverses() {
  const res = await fetch("/research-universes");
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  $("universe").innerHTML = universeOptions(data.universes || []);
}
const fmtNum = (v, d=2) => { const n = Number(v); return Number.isFinite(n) ? n.toFixed(d) : "--"; };
const fmtPct = (v, d=1) => { const n = Number(v); return Number.isFinite(n) ? (n * 100).toFixed(d) + "%" : "--"; };
const fmtMoney = (v) => { const n = Number(v); return Number.isFinite(n) && n > 0 ? "$" + n.toFixed(2) : "--"; };
function signalCn(row) {
  if (["强启动观察","观察","回避","中性"].includes(row.signal_cn)) return row.signal_cn;
  return row.signal === "strong_watch" ? "强启动观察" : row.signal === "watch" ? "观察" : row.signal === "avoid" ? "回避" : "中性";
}
function updateCalStats(item) {
  $("calEvents").textContent = item ? item.event_count : "--";
  $("calHorizon").textContent = item ? (item.primary_horizon || "5d") : "--";
  $("calIc").textContent = item && item.primary_ic !== null && item.primary_ic !== undefined ? Number(item.primary_ic).toFixed(3) : "--";
  $("calBeta").textContent = item && item.primary_beta !== null && item.primary_beta !== undefined ? Number(item.primary_beta).toFixed(3) : "--";
}
async function loadCalibrations() {
  $("status").textContent = "正在加载校准模型...";
  const res = await fetch("/event-driven/calibrations?usable_only=true&min_events=20");
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  calibrations = data.calibrations || [];
  $("calibration").innerHTML = '<option value="">自动选择最佳可用模型</option>' + calibrations.map(c => `<option value="${c.run_id}">${c.run_id} | ${c.event_count} events | ${(c.primary_horizon || "5d")} IC ${Number(c.primary_ic || 0).toFixed(3)}</option>`).join("");
  updateCalStats(calibrations[0]);
  $("status").textContent = "校准模型已加载";
}
$("calibration").addEventListener("change", () => updateCalStats(calibrations.find(c => c.run_id === $("calibration").value) || calibrations[0]));
$("refreshBtn").addEventListener("click", () => loadCalibrations().catch(e => $("status").textContent = "加载失败：" + e.message));
$("scanBtn").addEventListener("click", async () => {
  $("scanBtn").disabled = true; $("status").textContent = "正在采集新闻和行情...";
  $("tbody").innerHTML = '<tr><td colspan="11" class="muted" style="text-align:center;padding:34px">运行中，请稍候...</td></tr>';
  try {
    const tickers = $("tickers").value.split(/[,;\\s]+/).map(x => x.trim().toUpperCase()).filter(Boolean);
    const calibration = $("calibration").value;
    const payload = {
      universe: $("universe").value,
      tickers: tickers.length ? tickers : undefined,
      limit: tickers.length ? 0 : Number($("limit").value || 50),
      top: Number($("top").value || 20),
      news_per_ticker: Number($("news").value || 3),
      workers: 12,
      sleep_seconds: 0,
      include_prices: true,
      use_llm_scoring: $("llmScore").checked,
      auto_calibration: $("auto").value === "true" && !calibration,
      calibration_run_id: calibration || undefined,
      min_calibration_events: 20,
      timeout_seconds: 900
    };
    const res = await fetch("/event-driven/sp500/scan", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload) });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    $("runStats").style.display = "grid";
    $("runId").textContent = data.run_id;
    $("eventCount").textContent = data.event_count;
    $("signalCount").textContent = data.signal_count;
    $("elapsed").textContent = Number(data.elapsed_seconds || 0).toFixed(1) + "s";
    $("pdfLink").style.display = "inline-flex";
    $("pdfLink").href = data.report_pdf_url + (data.report_pdf_url.includes("?") ? "&" : "?") + "download=1";
    updateCalStats(data.calibration || calibrations[0]);
    const rows = data.signals || [];
    $("tbody").innerHTML = rows.length ? rows.map((r, i) => `
      <tr>
        <td>${i + 1}</td><td><b>${r.symbol}</b></td><td><span class="badge ${r.signal}">${signalCn(r)}</span></td>
        <td class="right">${fmtNum(r.launch_score, 3)}</td><td class="right">${fmtPct(r.expected_abret, 2)}</td><td class="right">${fmtMoney(r.last_price)}</td>
        <td class="right ${Number(r.ret_5d) >= 0 ? "good" : "bad"}">${fmtPct(r.ret_5d)}</td><td class="right ${Number(r.ret_20d) >= 0 ? "good" : "bad"}">${fmtPct(r.ret_20d)}</td><td class="right">${fmtNum(r.volume_ratio, 2)}</td>
        <td class="muted">${r.reason || "--"}</td><td>${r.top_event || "--"}</td>
      </tr>`).join("") : '<tr><td colspan="11" class="muted" style="text-align:center;padding:34px">没有满足条件的候选。</td></tr>';
    $("status").textContent = "扫描完成";
  } catch (e) {
    $("status").textContent = "扫描失败：" + e.message;
  } finally {
    $("scanBtn").disabled = false;
  }
});
$("historyBtn").addEventListener("click", async () => {
  $("historyBtn").disabled = true;
  $("historyStatus").textContent = "正在采集历史新闻并运行事件影响验证...";
  try {
    const tickers = $("histTickers").value.split(/[,;\\s]+/).map(x => x.trim().toUpperCase()).filter(Boolean);
    const payload = {
      tickers,
      start_date: $("histStart").value || "2026-03-01",
      end_date: $("histEnd").value || "2026-04-30",
      batch_size: Number($("histBatch").value || 2),
      max_batches: 1,
      max_records_per_ticker: 20,
      workers: 1,
      sleep_seconds: 1.0,
      batch_sleep_seconds: 2.0,
      validate: true,
      timeout_seconds: 1800
    };
    const res = await fetch("/event-driven/historical-batch", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload) });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    $("histRun").textContent = data.run_id || "--";
    $("histEvents").textContent = data.event_count ?? "--";
    $("histOk").textContent = data.successful_batches ?? "--";
    $("histFail").textContent = data.failed_batches ?? "--";
    $("historyStatus").textContent = data.status === "success" ? "历史验证完成，正在刷新校准模型..." : "历史验证部分完成，请查看失败批次后重试。";
    await loadCalibrations();
    $("historyStatus").textContent = "历史验证已结束，校准模型列表已刷新。";
  } catch (e) {
    $("historyStatus").textContent = "历史验证失败：" + e.message;
  } finally {
    $("historyBtn").disabled = false;
  }
});
Promise.all([loadUniverses(), loadCalibrations()]).catch(e => $("status").textContent = "加载失败：" + e.message);
</script>
</body>
</html>
        """
    )


@app.post("/event-driven/validate", response_model=EventImpactValidationResponse, dependencies=[Depends(require_auth)])
async def validate_event_impact(payload: EventImpactValidationRequest):
    """Validate historical event scores against subsequent abnormal returns."""
    return await asyncio.to_thread(_run_event_impact_validation_sync, payload)


@app.post("/event-driven/historical-collect", response_model=HistoricalEventCollectResponse, dependencies=[Depends(require_auth)])
async def collect_historical_events(payload: HistoricalEventCollectRequest):
    """Collect historical GDELT events and optionally run quantitative validation."""
    return await asyncio.to_thread(_run_historical_event_collect_sync, payload)


@app.post("/event-driven/historical-batch", response_model=HistoricalBatchCollectResponse, dependencies=[Depends(require_auth)])
async def collect_historical_events_batch(payload: HistoricalBatchCollectRequest):
    """Collect historical GDELT events in resumable batches."""
    return await asyncio.to_thread(_run_historical_batch_collect_sync, payload)


@app.get("/daily-stock/search", dependencies=[Depends(require_auth)])
async def daily_stock_search(q: str = Query("", max_length=80), limit: int = Query(8, ge=1, le=20)):
    """Autocomplete symbols from migrated reports, supplemented by Yahoo suggestions."""
    try:
        return {"items": await asyncio.to_thread(search_daily_stock_symbols, q, limit)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/daily-stock/recent", dependencies=[Depends(require_auth)])
async def daily_stock_recent(limit: int = Query(10, ge=1, le=30)):
    """Return recently generated migrated research reports."""
    try:
        return {"items": await asyncio.to_thread(get_daily_stock_recent_symbols, limit)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/daily-stock/analysis/{symbol}", dependencies=[Depends(require_auth)])
async def daily_stock_analysis(symbol: str):
    """Return one migrated research report plus an independently computed technical snapshot."""
    try:
        def _load_cache_only() -> Dict[str, Any]:
            with external_data_scope(False):
                analysis = get_daily_stock_analysis(symbol)
                analysis["research_context"] = _build_symbol_research_context(symbol)
                return analysis

        analysis = await asyncio.to_thread(_load_cache_only)
        return analysis
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/daily-stock-tool", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def daily_stock_tool():
    """Embedded workbench for migrated daily stock analysis reports."""
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>个股研判</title>
  <style>
    :root{color-scheme:dark;--bg:#07131b;--panel:#10212d;--line:#29404f;--muted:#93a8b5;--text:#eff7f8;--cyan:#45c7b8;--blue:#75aeff;--amber:#efbd62;--red:#f58480}
    *{box-sizing:border-box}body{margin:0;padding:24px;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}h1,h2,h3,p{margin:0}.sub,.muted{color:var(--muted)}.head{display:flex;justify-content:space-between;gap:16px;align-items:flex-end;margin-bottom:18px}.search-wrap{position:relative;max-width:760px}.toolbar{display:grid;grid-template-columns:minmax(260px,760px) 110px;gap:10px;margin-bottom:16px}input,button{height:42px;border:1px solid var(--line);border-radius:5px;background:#122532;color:var(--text);font:inherit}input{padding:0 14px;width:100%}button{cursor:pointer;background:#117b72;border-color:#239f94;font-weight:700;white-space:nowrap}.suggest{position:absolute;z-index:3;left:0;right:0;top:46px;border:1px solid var(--line);background:#10212d;box-shadow:0 12px 30px #0007;max-height:310px;overflow:auto}.suggest:empty{display:none}.option{padding:10px 12px;border-bottom:1px solid #203642;cursor:pointer;display:flex;justify-content:space-between;gap:12px}.option:hover{background:#17313d}.tag{font-size:12px;color:var(--cyan)}.grid{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:14px}.panel{border:1px solid var(--line);background:var(--panel);border-radius:6px}.panel h2{padding:12px 14px;border-bottom:1px solid var(--line);font-size:15px}.body{padding:14px}.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px;margin-bottom:14px}.metric{padding:11px;border:1px solid var(--line);background:#102532;border-radius:5px}.metric span{display:block;color:var(--muted);font-size:12px}.metric b{font-size:18px}.score{color:var(--cyan)}.warning{border-left:3px solid var(--amber);padding:9px 12px;background:#2b281a;color:#f7d89d;margin-bottom:14px}.chart{height:210px;width:100%;margin-top:8px}.levels{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0}.level{padding:9px;border:1px solid var(--line);border-radius:4px}.level span{display:block;color:var(--muted);font-size:12px}.dimensions{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}.dimension{padding:10px;border:1px solid var(--line);background:#102532;border-radius:5px}.dimension span{display:block;color:var(--muted);font-size:12px}.dimension b{display:block;margin-top:3px}.good{color:#7de0c3}.wait{color:#efbd62}.section{margin-top:15px}.text{white-space:pre-wrap;color:#d8e4e8}.recent{display:flex;flex-wrap:wrap;gap:7px}.chip{height:auto;padding:4px 8px;background:#142d39;border-color:#315361;font-weight:500}.history{width:100%;border-collapse:collapse}.history td{padding:8px;border-bottom:1px solid var(--line);font-size:13px}.empty{padding:45px 18px;text-align:center;color:var(--muted)}@media(max-width:900px){body{padding:14px}.grid{grid-template-columns:1fr}.metrics,.dimensions{grid-template-columns:repeat(2,1fr)}.levels{grid-template-columns:repeat(2,1fr)}}
  </style>
</head>
<body>
  <div class="head"><div><h1>个股决策复核台</h1><p class="sub">第三层：聚合机会质量、短周期择时、事件风险与历史研究，辅助人工判断是否进入交易计划。</p></div></div>
  <div class="toolbar"><div class="search-wrap"><input id="q" autocomplete="off" placeholder="输入股票代码或名称，例如 NVDA、诺基亚、腾讯控股"><div id="suggest" class="suggest"></div></div><button id="go">查询</button></div>
  <div id="warning"></div>
  <div class="grid">
    <main class="panel"><h2 id="title">研究报告</h2><div id="main" class="body empty">输入股票代码或公司名称开始查询。</div></main>
    <aside class="panel"><h2>最近分析</h2><div class="body"><div id="recent" class="recent"></div><div class="section"><h3>历史记录</h3><table class="history"><tbody id="history"></tbody></table></div></div></aside>
  </div>
<script>
const $=id=>document.getElementById(id),esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])),num=v=>v==null?"--":Number(v).toFixed(2),pct=v=>v==null?"--":(Number(v)*100).toFixed(1)+"%",debounce=(fn,ms)=>{let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms)}};let selected="";
function line(hist){if(!hist?.length)return '<div class="empty">暂无行情序列</div>';let w=760,h=210,p=12,vals=hist.map(x=>Number(x.close)),mn=Math.min(...vals),mx=Math.max(...vals),span=mx-mn||1,pts=vals.map((v,i)=>`${p+i*(w-2*p)/Math.max(1,vals.length-1)},${h-p-(v-mn)*(h-2*p)/span}`).join(" ");return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="#45c7b8" stroke-width="2"/></svg>`}
function text(v){return Array.isArray(v)?v.join("\\n"):v||"暂无"}
function context(c){let o=c?.opportunity,t=c?.timing,e=c?.event,p=c?.peer_earnings,u=t?.daily_tunnel,d=c?.dimensions||{},conf=c?.conflicts||[];return `<div class="section"><h3>三层量化复核 · ${esc(c?.decision_grade||"数据不足")}</h3><div class="text">${esc(c?.decision||"暂无共享结论")}</div></div><div class="dimensions"><div class="dimension"><span>机会质量</span><b class="${d.opportunity?"good":"wait"}">${o?`${esc(o.signal)} · ${Number(o.opportunity_score).toFixed(1)}`:"尚未进入观察池"}</b></div><div class="dimension"><span>启动择时</span><b class="${d.timing?"good":"wait"}">${t?`${esc(t.signal_cn)} · ${Number(t.launch_score).toFixed(3)}`:"尚未运行或未命中"}</b></div><div class="dimension"><span>日隧道协商</span><b class="${Number(u?.score||0)>=70?"good":"wait"}">${u?`${Number(u.score).toFixed(1)} 分 · ${esc(u.label)}<br><small>${esc(u.current_zone)} · 拐点 ${pct(u.turning_point_score)}</small>`:"尚未计算"}</b></div><div class="dimension"><span>同行接力</span><b class="${d.peer_earnings?"good":"wait"}">${p?`${esc(p.leader_symbol)} 先发 · 滞涨 ${pct(p.relative_lag_gap)}`:"暂无接力证据"}</b></div><div class="dimension"><span>事件风险</span><b class="${d.event_clear?"good":"wait"}">${e?esc(e.signal_cn||e.signal):"暂无事件告警"}</b></div></div>${u?`<div class="section"><h3>日隧道协商复核</h3><div class="text">MA5 ${num(u.ma5)} · MA10 ${num(u.ma10)} · MA15 ${num(u.ma15)} · MA20 ${num(u.ma20)}<br>近 ${esc(u.sample_days)} 日 · 当前上行 ${esc(u.current_up_cycle_days??0)} 日 · 最长上行 ${esc(u.longest_up_cycle_days??0)} 日 · 上行力量 ${pct(u.up_cycle_power)} · 拐点 ${pct(u.turning_point_score)} · 守住 MA20 ${pct(u.above_ma20_rate)}<br>${esc(u.note)}</div></div>`:""}${conf.length?`<div class="warning"><b>跨模型冲突：</b><br>${conf.map(x=>"· "+esc(x)).join("<br>")}</div>`:""} `}
function render(d){selected=d.symbol;$("title").textContent=`${d.name||d.symbol} · ${d.symbol}`;let flags=d.review_flags||[],shared=context(d.research_context||{});$("warning").innerHTML=`<div class="warning">${esc(d.warning)}${flags.length?`<br><b>自动复核：</b><br>${flags.map(x=>"· "+esc(x)).join("<br>")}`:""}</div>`;if(!d.has_analysis){$("main").innerHTML=`${shared}<div class="empty">暂无历史 AI 报告。三层量化结论仍可用于人工观察，待研究引擎补充单票报告。</div>${line(d.technical?.history)}`;$("history").innerHTML="";return}let r=d.report,t=d.technical||{},lv=r.levels||{};$("main").innerHTML=`${shared}<div class="metrics"><div class="metric"><span>历史评分</span><b class="score">${esc(r.sentiment_score??"--")}</b></div><div class="metric"><span>报告观点</span><b>${esc(r.operation_advice||"--")}</b></div><div class="metric"><span>趋势判断</span><b>${esc(r.trend_prediction||"--")}</b></div><div class="metric"><span>行情收盘</span><b>${num(t.close)}</b></div><div class="metric"><span>技术快照</span><b>${esc(t.trend||"--")}</b></div></div><div class="muted">报告生成：${esc(r.created_at)} · 行情截至：${esc(t.as_of||"暂无")} · 数据源：${esc(t.source||"暂无")}</div>${line(t.history)}<div class="levels"><div class="level"><span>理想观察价</span><b>${num(lv.ideal_buy)}</b></div><div class="level"><span>次级观察价</span><b>${num(lv.secondary_buy)}</b></div><div class="level"><span>止损复核位</span><b>${num(lv.stop_loss)}</b></div><div class="level"><span>止盈复核位</span><b>${num(lv.take_profit)}</b></div></div><div class="section"><h3>历史研究摘要</h3><div class="text">${esc(r.analysis_summary||"暂无")}</div></div><div class="section"><h3>风险提示</h3><div class="text">${esc(text(r.risk_warning))}</div></div><div class="section"><h3>行情技术复核</h3><div class="text">MA5 ${num(t.ma5)} · MA10 ${num(t.ma10)} · MA20 ${num(t.ma20)} · 量比 ${num(t.volume_ratio)} · 单日变化 ${num(t.change_pct)}%</div></div>`;$("history").innerHTML=(d.history||[]).map(x=>`<tr><td>${esc((x.created_at||"").slice(0,10))}</td><td>${esc(x.score??"--")}分</td><td>${esc(x.advice||"--")}</td></tr>`).join("")}
async function analyze(symbol){if(!symbol)return;let res=await fetch(`/daily-stock/analysis/${encodeURIComponent(symbol)}`);if(!res.ok)throw new Error((await res.json()).detail||"查询失败");render(await res.json());$("suggest").innerHTML=""}
async function search(){let q=$("q").value.trim();let d=await fetch(`/daily-stock/search?q=${encodeURIComponent(q)}`).then(r=>r.json());$("suggest").innerHTML=(d.items||[]).map(x=>`<div class="option" data-symbol="${esc(x.symbol)}"><span><b>${esc(x.symbol)}</b> · ${esc(x.name)}</span><span class="tag">${x.has_history?"已有报告":"搜索建议"}</span></div>`).join("");document.querySelectorAll(".option").forEach(x=>x.onclick=()=>{$("q").value=x.dataset.symbol;analyze(x.dataset.symbol).catch(showError)})}
function showError(e){$("warning").innerHTML=`<div class="warning">${esc(e.message)}</div>`}
$("q").oninput=debounce(search,250);$("q").onkeydown=e=>{if(e.key==="Enter")analyze($("q").value.trim().toUpperCase()).catch(showError)};$("go").onclick=()=>analyze($("q").value.trim().toUpperCase()).catch(showError);
fetch("/daily-stock/recent").then(r=>r.json()).then(d=>{$("recent").innerHTML=(d.items||[]).map(x=>`<button class="chip" data-symbol="${esc(x.symbol)}">${esc(x.symbol)}</button>`).join("");document.querySelectorAll(".chip").forEach(x=>x.onclick=()=>{$("q").value=x.dataset.symbol;analyze(x.dataset.symbol).catch(showError)})}).catch(showError);
const initialSymbol=new URLSearchParams(location.search).get("symbol");if(initialSymbol){$("q").value=initialSymbol.toUpperCase();analyze(initialSymbol.toUpperCase()).catch(showError)}
</script></body></html>
        """
    )


_STOCK_SIGNAL_MIN_BACKUP_CANDIDATES = 8
_SPECULATIVE_PEER_EARNINGS_UNIVERSE = "speculative_peer_earnings"
_STOCK_SIGNAL_SPLIT_POOLS: Dict[str, str] = {}
_STOCK_SIGNAL_UNIVERSE_CATALOG = RESEARCH_UNIVERSE_CATALOG
_stock_signal_update_jobs: Dict[str, Dict[str, Any]] = {}
_stock_signal_update_lock = threading.Lock()
_research_signal_hub_jobs: Dict[str, Dict[str, Any]] = {}
_research_signal_hub_lock = threading.Lock()
_research_universe_rank_cache: Dict[str, Any] = {"expires_at": 0.0, "items": []}
_SUMMARY_SNAPSHOT_TTL_SECONDS = int(os.getenv("SUMMARY_SNAPSHOT_TTL", "300") or 300)
_SUMMARY_DEFAULT_LIMIT = int(os.getenv("SUMMARY_DEFAULT_LIMIT", "20") or 20)
_SUMMARY_SNAPSHOT_ENABLED = os.getenv("SUMMARY_SNAPSHOT_ENABLED", "1").lower() not in {"0", "false", "no"}
_SUMMARY_LAZY_LOAD_DETAILS = os.getenv("SUMMARY_LAZY_LOAD_DETAILS", "1").lower() not in {"0", "false", "no"}


def _perf_log(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    try:
        console.log(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception:
        console.log(str(payload))


def _json_safe(value: Any) -> Any:
    """Recursively convert non-finite numeric values to JSON-safe nulls."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _stock_signal_run_id(path: Path) -> str:
    return path.parent.name if path.parent.name != "artifacts" else path.parent.parent.name


def _run_stock_signal_update_job(job_id: str, universe: str) -> None:
    """Run the existing options screener in an isolated run directory."""
    job = _stock_signal_update_jobs[job_id]
    run_id = str(job["run_id"])
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    (run_dir / "code").mkdir(exist_ok=True)
    if universe == _SPECULATIVE_PEER_EARNINGS_UNIVERSE:
        job.update({"status": "running", "message": "正在运行同行财报接力专项扫描...", "progress": 0.12})
        try:
            result = scan_launch_signals(
                universe=universe,
                threshold=0.5,
                top=200,
                scan_mode="peer_earnings",
                peer_group="all",
                peer_cache_path=RUNS_DIR / "_peer_earnings_signal_cache.json",
            )
            _store_launch_result(universe, result, replace=True)
            job.update({
                "status": "completed",
                "message": f"专项扫描完成：{int(result.get('n_signals', 0) or 0)} 只同行接力候选。",
                "progress": 1.0,
            })
            (run_dir / "state.json").write_text(
                json.dumps({"status": "success", "universe": universe}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            message = str(exc)[-1200:]
            job.update({"status": "failed", "message": f"专项扫描失败：{message}", "progress": 1.0})
        return
    script = AGENT_DIR / "scripts" / "screening_framework_v2_optimized.py"
    output = run_dir / f"options_screening_results_{universe}_{datetime.now().strftime('%y%m%d')}.json"
    if not script.exists():
        job.update({"status": "failed", "message": "筛选脚本不存在。"})
        return
    shutil.copy2(script, run_dir / "code" / script.name)
    request_payload = {
        "prompt": "Refresh stock-signal options evidence report",
        "universe": universe,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    (run_dir / "req.json").write_text(json.dumps(request_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    cmd = [
        _sys.executable,
        str(script),
        "--universe",
        universe,
        "--output",
        str(output),
    ]
    if job.get("reuse_session_results"):
        cmd.append("--reuse-session-results")
    if job.get("daily_price_cache_only"):
        cmd.append("--daily-price-cache-only")
    if job.get("option_chain_cache_minutes") is not None:
        cmd.extend(["--option-chain-cache-minutes", str(job["option_chain_cache_minutes"])])
    job.update({"status": "running", "message": "正在解析股票池并拉取行情...", "progress": 0.02})
    stdout_lines: List[str] = []
    process_done = threading.Event()
    proc = None
    try:
        _check_scan_budget(job)
        proc = subprocess.Popen(
            cmd,
            cwd=str(run_dir),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=1,
        )
        watchdog = watch_process(proc, job, process_done)
        assert proc.stdout is not None
        with (run_dir / "logs" / "stock_signal_update.log").open("w", encoding="utf-8") as log:
            for line in proc.stdout:
                stdout_lines.append(line)
                log.write(line)
                log.flush()
                match = re.search(r"\[(\d+)/(\d+)\]", line)
                if match:
                    current, total = int(match.group(1)), max(1, int(match.group(2)))
                    job.update({
                        "message": f"正在分析 {current} / {total} 只股票...",
                        "progress": min(0.96, 0.05 + 0.9 * current / total),
                    })
        return_code = proc.wait()
        process_done.set()
        watchdog.join(timeout=6)
        _check_scan_budget(job)
        if return_code != 0 or not output.exists():
            tail = "".join(stdout_lines[-20:]).strip()
            raise RuntimeError(tail or f"筛选脚本退出码 {return_code}")
        job.update({
            "status": "completed",
            "message": "更新完成，正在载入最新报告。",
            "progress": 1.0,
            "report_pdf_url": f"/runs/{run_id}/report.pdf?download=1",
        })
        (run_dir / "state.json").write_text(
            json.dumps({"status": "success", "universe": universe}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        message = str(exc)[-1200:]
        job.update({"status": "failed", "message": f"更新失败：{message}", "progress": 1.0})
        (run_dir / "state.json").write_text(
            json.dumps({"status": "failed", "universe": universe, "reason": message}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    finally:
        process_done.set()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        with _stock_signal_update_lock:
            job["finished_at"] = datetime.utcnow().isoformat() + "Z"


def _stock_signal_report_candidates() -> List[Path]:
    """Return de-duplicated options screening JSON reports, newest first."""
    candidates: Dict[str, Path] = {}
    for path in RUNS_DIR.glob("*/options_screening_results*.json"):
        if re.match(r"^\d{8}_\d{6}_[A-Za-z0-9_-]+$", path.parent.name):
            candidates[str(path.resolve())] = path
    for path in RUNS_DIR.glob("*/artifacts/options_screening_results*.json"):
        root_copy = path.parent.parent / path.name
        if not root_copy.exists() and re.match(r"^\d{8}_\d{6}_[A-Za-z0-9_-]+$", path.parent.parent.name):
            candidates[str(path.resolve())] = path
    # Sort by immutable run id instead of mtime. Backfilling report metadata
    # must not make an older research run appear newer than the market scan.
    return sorted(candidates.values(), key=lambda p: (_stock_signal_run_id(p), p.name), reverse=True)


def _stock_signal_report_universe(payload: Dict[str, Any], path: Path) -> str:
    universe = payload.get("universe") or {}
    name = str(universe.get("name") or "").strip()
    if name:
        return name
    match = re.search(r"options_screening_results_(.+?)_\d{6}\.json$", path.name)
    return match.group(1) if match else "unknown"


def _stock_signal_report_has_results(payload: Dict[str, Any]) -> bool:
    """A newest empty/failed report must not hide the latest usable pool snapshot."""
    results = payload.get("results") or []
    if results:
        return True
    try:
        return int(payload.get("valid_tickers", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def _stock_signal_report_summary(path: Path) -> Dict[str, Any]:
    payload = _load_json_file(path) or {}
    universe = payload.get("universe") or {}
    run_id = _stock_signal_run_id(path)
    results = payload.get("results") or []
    bullish = [r for r in results if str(r.get("primary_strategy")) in _STOCK_SIGNAL_BULLISH_STRATEGIES]
    return {
        "run_id": run_id,
        "universe": _stock_signal_report_universe(payload, path),
        "source": universe.get("source", ""),
        "timestamp": payload.get("timestamp", ""),
        "ticker_count": payload.get("ticker_count", 0),
        "valid_tickers": payload.get("valid_tickers", len(results)),
        "bullish_signals": len(bullish),
        "report_pdf_url": f"/runs/{run_id}/report.pdf?download=1",
    }


def _stock_signal_filter_results(payload: Dict[str, Any], universe: str) -> List[Dict[str, Any]]:
    """Filter a combined report into a standalone research pool when requested."""
    results = payload.get("results") or []
    source_label = _STOCK_SIGNAL_SPLIT_POOLS.get(universe)
    if not source_label:
        return results
    memberships = (payload.get("universe") or {}).get("memberships") or {}
    return [
        result for result in results
        if source_label in (result.get("universe_memberships") or memberships.get(str(result.get("ticker") or "")) or [])
    ]


def _stock_signal_find_report(universe: str) -> tuple[Path | None, Dict[str, Any], bool]:
    """Find the newest direct report, or derive a split pool from the latest combined report."""
    direct: tuple[Path, Dict[str, Any]] | None = None
    combined: tuple[Path, Dict[str, Any]] | None = None
    for path in _stock_signal_report_candidates():
        payload = _load_json_file(path) or {}
        if not _stock_signal_report_has_results(payload):
            continue
        report_universe = _stock_signal_report_universe(payload, path)
        if report_universe == universe and direct is None:
            direct = (path, payload)
        if report_universe == "sp100_komp_soxx" and combined is None:
            combined = (path, payload)
    if direct is not None and (
        universe not in _STOCK_SIGNAL_SPLIT_POOLS
        or combined is None
        or _stock_signal_run_id(direct[0]) >= _stock_signal_run_id(combined[0])
    ):
        return direct[0], direct[1], False
    if universe in _STOCK_SIGNAL_SPLIT_POOLS and combined is not None:
        return combined[0], combined[1], True
    return None, {}, False


def _stock_signal_item(result: Dict[str, Any], memberships: Dict[str, List[str]], run_id: str) -> Dict[str, Any]:
    return _shared_stock_signal_item(result, memberships, run_id)


def _peer_relay_stock_signal_items(limit: int = 100) -> List[Dict[str, Any]]:
    """Expose fresh peer-earnings relay candidates as a dedicated speculative watchlist."""
    items = []
    for symbol, timing in _launch_signal_items():
        peer = timing.get("peer_earnings") or {}
        if (
            timing.get("scan_universe") != _SPECULATIVE_PEER_EARNINGS_UNIVERSE
            or not peer
            or int(peer.get("peer_match_version", 0) or 0) != PEER_MATCH_VERSION
            or not _is_fresh_iso(timing.get("scan_time"), 24)
        ):
            continue
        probability = float(peer.get("research_probability", 0) or 0)
        relay_score = float(peer.get("relay_score", 0) or 0)
        target_price = float(peer.get("target_latest_price", timing.get("last_price", 0)) or 0)
        risk_level = "低" if probability >= 0.72 else ("中" if probability >= 0.58 else "高")
        items.append({
            "ticker": str(symbol).upper(),
            "signal": "同行接力观察",
            "signal_tone": "strong" if probability >= 0.72 else "watch",
            "spot": target_price,
            "entry_zone_low": round(target_price * 0.98, 2),
            "entry_zone_high": round(target_price * 1.02, 2),
            "trend_30d": float(peer.get("target_return", 0) or 0),
            "opportunity_score": round(relay_score * 100, 1),
            "opportunity_tier": "speculative_peer_relay",
            "research_score": round(probability * 10, 2),
            "source_pools": ["投机-同行财报接力池"],
            "risk_level": risk_level,
            "risk_flags": ["专项投机池：需人工复核财报原文、披露日期与流动性。"],
            "rejection_reasons": [],
            "recent_closes": [],
            "liquidity_score": 0.0,
            "liquidity_grade": "专项模式不使用期权流动性",
            "peer_earnings": peer,
            "option_evidence": {
                "strategy": "",
                "strategy_desc": "专项模式跳过期权底层筛选",
                "pop": probability,
            },
            "report_pdf_url": "",
        })
    items.sort(key=lambda item: (-item["opportunity_score"], item["ticker"]))
    return items[:limit]


def _stock_signal_observation_pool(universe: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Return strict candidates plus simple backup ideas for downstream scans."""
    if str(universe or "").strip().lower() == "watchlist":
        return _watchlist_observation_pool(limit=limit)
    selected_path, selected_payload, _derived = _stock_signal_find_report(universe)
    if selected_path is None:
        return []
    run_id = _stock_signal_run_id(selected_path)
    memberships = (selected_payload.get("universe") or {}).get("memberships") or {}
    pool_results = _stock_signal_filter_results(selected_payload, universe)
    return _stock_signal_candidate_pool(pool_results, memberships, run_id, limit)


def _stock_signal_candidate_pool(
    pool_results: List[Dict[str, Any]],
    memberships: Dict[str, List[str]],
    run_id: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    return _shared_stock_signal_candidate_pool(pool_results, memberships, run_id, limit)


def _watchlist_observation_pool(limit: int = 100) -> List[Dict[str, Any]]:
    """Build lightweight first-layer opportunity rows from the user's watchlist.

    The normal first-layer script constructs option structures. A user watchlist
    can contain non-optionable names, so this path keeps the equity signal chain
    alive without fabricating option evidence.
    """
    try:
        from app_database import user_watchlist_list
        from pullback_signal_service import detect_recent_pullback_setup
    except Exception:
        return []
    items = []
    for item in user_watchlist_list(include_disabled=False)[: max(1, int(limit))]:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        try:
            frame, source = get_daily_history(symbol, period="1y")
        except Exception:
            frame, source = pd.DataFrame(), "unavailable"
        closes: List[float] = []
        spot = 0.0
        trend_30d = 0.0
        recent_rows: List[Dict[str, Any]] = []
        if frame is not None and not frame.empty and "Close" in frame:
            try:
                recent = frame.tail(80).copy()
                close = pd.to_numeric(recent["Close"], errors="coerce").dropna()
                closes = [float(v) for v in close.tail(60).tolist() if float(v) > 0]
                spot = float(close.iloc[-1]) if len(close) else 0.0
                if len(close) >= 31:
                    trend_30d = spot / float(close.iloc[-31]) - 1.0
                for idx, row in recent.reset_index().iterrows():
                    recent_rows.append({
                        "Date": str(row.iloc[0])[:10],
                        "Open": row.get("Open"),
                        "High": row.get("High"),
                        "Low": row.get("Low"),
                        "Close": row.get("Close"),
                        "Volume": row.get("Volume"),
                    })
            except Exception:
                pass
        pullback_rejection: Dict[str, Any] = {}
        pullback_confirmation: Dict[str, Any] = {}
        try:
            pullback_rejection, pullback_confirmation = detect_recent_pullback_setup(recent_rows, ticker=symbol)
        except Exception:
            pullback_rejection = {"signal_name": "pullback_rejection", "signal_stage": "NONE", "score": 0.0, "risk_flags": ["pullback_calc_failed"]}
            pullback_confirmation = {"signal_name": "pullback_rejection_confirmation", "signal_stage": "NONE", "confirmation_score": 0.0}
        score = max(0.0, min(100.0, 50.0 + trend_30d * 100.0))
        if pullback_rejection.get("signal_stage") == "STRONG_WATCH":
            score += 12
        elif pullback_rejection.get("signal_stage") == "WATCH":
            score += 7
        if pullback_confirmation.get("signal_stage") in {"CONFIRMED", "STRONG_CONFIRMED"}:
            score += 12
        score = round(max(0.0, min(100.0, score)), 1)
        items.append({
            "ticker": symbol,
            "signal": "自选池观察",
            "signal_tone": "watch",
            "spot": spot,
            "price_as_of": recent_rows[-1]["Date"] if recent_rows else None,
            "entry_zone_low": round(spot * 0.98, 2) if spot else None,
            "entry_zone_high": round(spot * 1.02, 2) if spot else None,
            "trend_30d": round(trend_30d, 4),
            "opportunity_score": score,
            "opportunity_tier": "watchlist",
            "candidate_label": "自选池",
            "research_score": round(score / 10.0, 2),
            "source_pools": ["自选池"],
            "risk_level": "中",
            "risk_flags": ["自选池：期权辅助证据不强制要求，需人工复核基本面与流动性。"],
            "rejection_reasons": [],
            "recent_closes": closes,
            "liquidity_score": 0.0,
            "liquidity_grade": "自选池未使用期权流动性过滤",
            "option_evidence": {
                "available": False,
                "status": "not_required_for_watchlist",
                "strategy": "",
                "strategy_desc": "自选池按实股研究信号运行，未伪造期权 POP。",
                "pop": None,
            },
            "pullback_rejection": pullback_rejection,
            "pullback_confirmation": pullback_confirmation,
            "market_cap": None,
            "trailing_pe": None,
            "forward_pe": None,
            "run_id": "watchlist",
            "data_source": source,
        })
    items.sort(key=lambda row: (-float(row.get("opportunity_score") or 0), str(row.get("ticker") or "")))
    return items[: max(1, int(limit))]


def _latest_stock_opportunity(symbol: str) -> Optional[Dict[str, Any]]:
    """Find the strongest latest equity opportunity record across generated pools."""
    symbol = symbol.upper()
    matches: List[Dict[str, Any]] = []
    for path in _stock_signal_report_candidates():
        payload = _load_json_file(path) or {}
        memberships = (payload.get("universe") or {}).get("memberships") or {}
        run_id = _stock_signal_run_id(path)
        for result in payload.get("results") or []:
            if str(result.get("ticker") or "").upper() != symbol:
                continue
            if str(result.get("primary_strategy")) not in _STOCK_SIGNAL_BULLISH_STRATEGIES:
                continue
            item = _stock_signal_item(result, memberships, run_id)
            if item["research_score"] > 0 and item["trend_30d"] > 0 and item["opportunity_tier"] != "exclude":
                matches.append(item)
    if not matches:
        return None
    matches.sort(key=lambda item: (-item["opportunity_score"], -item["trend_30d"]))
    return matches[0]


def _equity_hypothesis_test_plan(
    opportunity: Optional[Dict[str, Any]],
    timing: Optional[Dict[str, Any]],
    decision_grade: str,
) -> Dict[str, Any]:
    """Build a risk-bounded sequential research test for one cash-equity idea."""
    if not opportunity:
        peer = (timing or {}).get("peer_earnings") or {}
        if peer:
            account_capital = float(os.environ.get("RESEARCH_TEST_CAPITAL", "2000") or 2000)
            spot = float(peer.get("target_latest_price", (timing or {}).get("last_price", 0)) or 0)
            probability = float(peer.get("research_probability", 0) or 0)
            lag_gap = max(0.0, float(peer.get("relative_lag_gap", 0) or 0))
            invalidation_pct = max(0.035, min(0.12, 0.04 + lag_gap * 0.35))
            invalidation_price = round(spot * (1.0 - invalidation_pct), 2) if spot > 0 else 0.0
            loss_per_share = max(0.01, spot - invalidation_price)
            timing_confirmed = bool(timing and timing.get("signal") in {"strong_buy", "buy", "watch"})
            sizing = _research_test_sizing(
                spot=spot,
                loss_per_share=loss_per_share,
                account_capital=account_capital,
                probability=max(probability, 0.58),
                allow_micro=bool(timing_confirmed and probability >= 0.55 and spot > 0),
            )
            shares = int(sizing["test_shares"]) if timing_confirmed else 0
            eligible = shares >= 1
            reason = (
                f"同行接力专项：{sizing['reason']}"
                if eligible
                else "同行接力专项：尚未形成足够的启动/概率共振，先做纸面跟踪。"
            )
            return {
                "eligible": eligible,
                "reason": reason,
                "account_capital": round(account_capital, 2),
                "alpha": None,
                "lookback_samples": 0,
                "validation_days": 3,
                "null_hypothesis": "H0：同行财报接力无法传导到目标标的。",
                "alternative_hypothesis": "H1：先发财报验证同赛道需求，目标标的存在补涨窗口。",
                "z_score": None,
                "p_value": None,
                "statistical_pass": bool(probability >= 0.55),
                "daily_volatility": None,
                "entry_reference": round(spot, 2),
                "invalidation_price": invalidation_price,
                "invalidation_pct": round(invalidation_pct, 4),
                "exit_rule": f"若收盘价 <= ${invalidation_price:.2f}，或先发公司财报逻辑被公告/价格证伪，退出接力观察。",
                "risk_budget": sizing["risk_budget"],
                "risk_budget_pct": sizing["risk_budget_pct"],
                "allocation_cap_pct": sizing["allocation_cap_pct"],
                "micro_cap_pct": sizing["micro_cap_pct"],
                "sizing_mode": sizing["sizing_mode"] if timing_confirmed else "paper_track",
                "risk_budget_breach": bool(sizing["risk_budget_breach"]) if timing_confirmed else False,
                "shares_by_risk": sizing["shares_by_risk"] if timing_confirmed else 0,
                "shares_by_capital": sizing["shares_by_capital"] if timing_confirmed else 0,
                "test_shares": shares,
                "capital_required": round(shares * spot, 2),
                "capital_usage_pct": round(shares * spot / account_capital, 4) if account_capital else 0.0,
                "max_test_loss": round(shares * loss_per_share, 2),
                "win_rate_note": "同行接力概率为研究排序分，不是实盘胜率；需人工复核财报原文、披露日期和流动性。",
            }
        return {"eligible": False, "reason": "尚未进入实股机会观察池。"}
    closes = [float(value) for value in (opportunity.get("recent_closes") or []) if float(value or 0) > 0]
    spot = float(opportunity.get("spot", 0) or 0)
    if spot <= 0 or len(closes) < 8:
        return {"eligible": False, "reason": "有效收盘样本不足，无法构造统计检验试仓。"}

    returns = [math.log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]
    sample = returns[-min(20, len(returns)):]
    sigma = stdev(sample) if len(sample) >= 2 else 0.0
    mean_return = sum(sample) / len(sample)
    standard_error = sigma / math.sqrt(len(sample)) if sigma > 0 else 0.0
    z_score = mean_return / standard_error if standard_error > 0 else 0.0
    p_value = 1.0 - NormalDist().cdf(z_score)
    alpha = 0.10
    validation_days = 3
    z_invalidation = NormalDist().inv_cdf(0.95)
    downside_move = max(0.025, min(0.12, z_invalidation * sigma * math.sqrt(validation_days)))
    invalidation_price = round(spot * math.exp(-downside_move), 2)
    loss_per_share = max(0.01, spot - invalidation_price)

    option_evidence = opportunity.get("option_evidence") or {}
    blended_win_rate = float(option_evidence.get("pop", 0) or 0)
    historical_win_rate = option_evidence.get("historical_barrier_win_rate")
    pop_range = option_evidence.get("conservative_pop_range") or {}
    conservative_win_rate = float(pop_range.get("low", blended_win_rate) or blended_win_rate)

    timing_confirmed = bool(timing and timing.get("signal") in {"strong_buy", "buy"})
    event_or_risk_blocked = decision_grade in {"风险偏高", "数据不足"}
    statistical_pass = z_score > 0 and p_value <= alpha
    eligible = bool(timing_confirmed and statistical_pass and not event_or_risk_blocked)

    account_capital = float(os.environ.get("RESEARCH_TEST_CAPITAL", "2000") or 2000)
    conviction = conservative_win_rate
    if decision_grade == "计划候选":
        conviction = max(conviction, 0.68)
    elif decision_grade in {"等待复核", "等待回踩"}:
        conviction = max(conviction, 0.58)
    sizing = _research_test_sizing(
        spot=spot,
        loss_per_share=loss_per_share,
        account_capital=account_capital,
        probability=conviction,
        allow_micro=eligible,
    )
    shares = int(sizing["test_shares"]) if eligible else 0
    capital_required = round(shares * spot, 2)
    max_test_loss = round(shares * loss_per_share, 2)

    if event_or_risk_blocked:
        reason = "事件或数据风险阻断，不建议试仓。"
    elif not timing_confirmed:
        reason = "尚未出现启动择时共振，继续观察，不建议试仓。"
    elif not statistical_pass:
        reason = f"单侧检验尚未通过：p={p_value:.3f} > {alpha:.2f}，继续观察。"
    elif shares < 1:
        reason = sizing["reason"]
    else:
        reason = sizing["reason"]

    return {
        "eligible": bool(eligible and shares >= 1),
        "reason": reason,
        "account_capital": round(account_capital, 2),
        "alpha": alpha,
        "lookback_samples": len(sample),
        "validation_days": validation_days,
        "null_hypothesis": "H0：短期动量无法延续，日均对数收益 <= 0。",
        "alternative_hypothesis": "H1：短期动量延续，日均对数收益 > 0。",
        "z_score": round(z_score, 3),
        "p_value": round(p_value, 4),
        "statistical_pass": statistical_pass,
        "daily_volatility": round(sigma, 4),
        "entry_reference": round(spot, 2),
        "invalidation_price": invalidation_price,
        "invalidation_pct": round(loss_per_share / spot, 4),
        "exit_rule": f"若收盘价 <= ${invalidation_price:.2f}，视为原假设被走势事实推翻，退出研究试仓。",
        "risk_budget": sizing["risk_budget"],
        "risk_budget_pct": sizing["risk_budget_pct"],
        "allocation_cap_pct": sizing["allocation_cap_pct"],
        "micro_cap_pct": sizing["micro_cap_pct"],
        "sizing_mode": sizing["sizing_mode"] if eligible else "not_eligible",
        "risk_budget_breach": bool(sizing["risk_budget_breach"]) if eligible else False,
        "shares_by_risk": sizing["shares_by_risk"] if eligible else 0,
        "shares_by_capital": sizing["shares_by_capital"] if eligible else 0,
        "test_shares": shares,
        "capital_required": capital_required,
        "capital_usage_pct": round(capital_required / account_capital, 4) if account_capital else 0.0,
        "max_test_loss": max_test_loss,
        "blended_win_rate": round(blended_win_rate, 4),
        "model_win_rate": round(float(option_evidence.get("model_pop", blended_win_rate) or 0), 4),
        "historical_barrier_win_rate": historical_win_rate,
        "conservative_win_rate_low": round(conservative_win_rate, 4),
        "win_rate_note": "胜率来自期权结构与历史障碍检验，仅作为实股试仓的辅助证据，不是实股实盘胜率承诺。",
    }


def _research_test_sizing(
    *,
    spot: float,
    loss_per_share: float,
    account_capital: float,
    probability: float,
    allow_micro: bool,
) -> Dict[str, Any]:
    """Size a small-account research probe without filtering every ticker out.

    The old rule capped cash usage at 8-15% of a $2,000 account, so any stock
    above roughly $160-$300 was forced to "no test".  For a research tool, that
    is too brittle.  Standard probes still respect risk and allocation caps;
    micro probes allow exactly one share when the thesis is acceptable and the
    price is not an account-concentration outlier.
    """
    account_capital = max(0.0, float(account_capital or 0))
    spot = max(0.0, float(spot or 0))
    loss_per_share = max(0.01, float(loss_per_share or 0.01))
    probability = max(0.0, min(1.0, float(probability or 0.0)))

    if probability >= 0.72:
        risk_pct, allocation_cap_pct, micro_cap_pct = 0.025, 0.30, 0.50
    elif probability >= 0.60:
        risk_pct, allocation_cap_pct, micro_cap_pct = 0.018, 0.25, 0.40
    else:
        risk_pct, allocation_cap_pct, micro_cap_pct = 0.012, 0.20, 0.35

    risk_budget = round(account_capital * risk_pct, 2)
    shares_by_risk = int(risk_budget // loss_per_share) if loss_per_share > 0 else 0
    shares_by_capital = int((account_capital * allocation_cap_pct) // spot) if spot > 0 else 0
    shares = max(0, min(shares_by_risk, shares_by_capital))
    sizing_mode = "standard_probe" if shares >= 1 else "paper_track"
    risk_budget_breach = False

    if shares < 1 and allow_micro and spot > 0 and spot <= account_capital * micro_cap_pct:
        shares = 1
        sizing_mode = "micro_probe"
        risk_budget_breach = loss_per_share > risk_budget

    if shares >= 1 and sizing_mode == "micro_probe":
        reason = (
            f"可用 1 股做微型研究试仓；该票单股价格较高，允许小幅突破风险预算，"
            f"收盘触发失效价时退出。"
        )
    elif shares >= 1:
        reason = f"可用 {shares} 股做研究试仓；走势推翻假设时退出。"
    else:
        reason = "股价相对账户过高，转为纸面跟踪，不建议为了试仓强行集中持仓。"

    return {
        "risk_budget": risk_budget,
        "risk_budget_pct": risk_pct,
        "allocation_cap_pct": allocation_cap_pct,
        "micro_cap_pct": micro_cap_pct,
        "shares_by_risk": shares_by_risk,
        "shares_by_capital": shares_by_capital,
        "test_shares": shares,
        "capital_required": round(shares * spot, 2),
        "capital_usage_pct": round(shares * spot / account_capital, 4) if account_capital else 0.0,
        "max_test_loss": round(shares * loss_per_share, 2),
        "sizing_mode": sizing_mode,
        "risk_budget_breach": risk_budget_breach,
        "reason": reason,
    }


def _basic_risk_target_price(entry_price: float, stop_price: float, source: Optional[Dict[str, Any]] = None) -> float:
    """Pick a simple target for the first risk/reward gate from existing fields."""
    source = source or {}
    watch_levels = source.get("watch_levels") or {}
    candidates = [
        watch_levels.get("one_sigma_up"),
        watch_levels.get("expected_target_price"),
        source.get("target_price"),
        source.get("expected_target_price"),
    ]
    for value in candidates:
        try:
            target = float(value or 0)
        except (TypeError, ValueError):
            continue
        if target > entry_price:
            return round(target, 2)
    per_share_risk = max(0.01, entry_price - stop_price)
    return round(entry_price + per_share_risk * 2.0, 2)


def _apply_basic_trade_risk_guard(
    hypothesis_test: Dict[str, Any],
    *,
    target_price: float,
    current_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Apply the first-version open and exit risk rules to a hypothesis plan."""
    result = dict(hypothesis_test)
    equity = float(result.get("account_capital", 0) or 0)
    entry_price = float(result.get("entry_reference", 0) or 0)
    stop_price = float(result.get("invalidation_price", 0) or 0)
    requested_qty = int(result.get("test_shares", 0) or 0)
    open_check = validate_open_risk(
        equity=equity,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        requested_qty=requested_qty,
    )
    allowed_qty = int(open_check["allowed_qty"])
    status = str(open_check["status"])
    if status == BLOCK_OPEN:
        final_qty = 0
        result["eligible"] = False
        result["sizing_mode"] = "risk_blocked"
    elif status == "REDUCE_QTY":
        final_qty = allowed_qty
        result["eligible"] = final_qty >= 1
        result["sizing_mode"] = "risk_reduced"
    else:
        final_qty = min(requested_qty, allowed_qty)
        result["eligible"] = final_qty >= 1 and bool(result.get("eligible", False))
    max_test_loss = max(0.0, (entry_price - stop_price) * final_qty)
    result.update({
        "target_price": round(float(target_price or 0), 2),
        "trade_risk_check": open_check,
        "open_risk_status": status,
        "open_risk_reason": open_check["reason"],
        "requested_qty": requested_qty,
        "allowed_qty": allowed_qty,
        "test_shares": final_qty,
        "capital_required": round(final_qty * entry_price, 2),
        "capital_usage_pct": round(final_qty * entry_price / equity, 4) if equity else 0.0,
        "max_test_loss": round(max_test_loss, 2),
        "risk_budget": open_check["risk_budget"],
        "risk_budget_pct": 0.015,
        "allocation_cap_pct": 0.35,
        "reward_risk_ratio": open_check["reward_risk_ratio"],
        "stop_loss_pct": open_check["stop_loss_pct"],
    })
    if status != "ALLOW_OPEN":
        result["reason"] = f"{open_check['reason']}；{result.get('reason', '')}".strip("；")

    exit_check = evaluate_exit_risk(
        equity=equity,
        entry_price=entry_price,
        current_price=float(current_price if current_price is not None else entry_price),
        stop_price=stop_price,
        position_qty=final_qty,
    )
    result["exit_risk_check"] = exit_check
    result["exit_action"] = exit_check["status"]
    result["exit_reason"] = exit_check["reason"]
    result["exit_rule"] = (
        f"若当前价 <= ${stop_price:.2f}，或单笔浮亏达到 ${open_check['risk_budget']:.2f} 风险预算，强制退出。"
    )
    return result


def _win_rate_sort_metrics(
    opportunity: Optional[Dict[str, Any]],
    peer_earnings: Optional[Dict[str, Any]],
    hypothesis_test: Dict[str, Any],
    unified_probability: float,
    portfolio_timing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return _shared_win_rate_sort_metrics(
        opportunity,
        peer_earnings,
        hypothesis_test,
        unified_probability,
        portfolio_timing=portfolio_timing,
    )


def _clamp_probability(value: Any, default: float = 0.5) -> float:
    """Keep research probabilities finite and away from infinite log-odds."""
    try:
        probability = float(value)
    except (TypeError, ValueError):
        probability = default
    if not math.isfinite(probability):
        probability = default
    return max(0.02, min(0.98, probability))


def _research_evidence_summary(
    opportunity: Optional[Dict[str, Any]],
    peer_earnings: Optional[Dict[str, Any]],
    hypothesis_test: Dict[str, Any],
    timing: Optional[Dict[str, Any]] = None,
    stock_panic: Optional[Dict[str, Any]] = None,
    macro_regime: Optional[Dict[str, Any]] = None,
    portfolio_timing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Blend heterogeneous research evidence with weighted log-odds.

    This is an interpretable ranking score, not a calibrated live-trading win
    rate.  Log-odds avoid the false precision of simply averaging probabilities
    from sources with different uncertainty.
    """
    components: List[Dict[str, Any]] = []
    if peer_earnings:
        pair_similarity = _clamp_probability(peer_earnings.get("peer_similarity"), 0.70)
        raw_probability = _clamp_probability(peer_earnings.get("research_probability"))
        conservative_probability = 0.5 + (raw_probability - 0.5) * pair_similarity
        components.append({
            "id": "peer_earnings_relay",
            "label": "同行财报接力",
            "probability": _clamp_probability(conservative_probability),
            "source_probability": raw_probability,
            "reliability": round(pair_similarity, 3),
            "weight": round(0.45 + 0.10 * pair_similarity, 3),
        })
    option_evidence = (opportunity or {}).get("option_evidence") or {}
    option_probability = (
        (option_evidence.get("conservative_pop_range") or {}).get("low")
        or option_evidence.get("pop")
    )
    option_usable = (
        bool(option_evidence.get("available"))
        or option_evidence.get("status") == "tradable_option_structure"
        or option_evidence.get("tradable_structure") is True
    )
    if option_probability is not None and option_usable:
        raw_probability = _clamp_probability(option_probability)
        components.append({
            "id": "option_auxiliary",
            "label": "期权辅助证据",
            "probability": _clamp_probability(0.5 + (raw_probability - 0.5) * 0.70),
            "source_probability": raw_probability,
            "reliability": 0.70,
            "weight": 0.25,
        })
    if hypothesis_test.get("lookback_samples"):
        p_value = _clamp_probability(hypothesis_test.get("p_value"), 0.5)
        sample_reliability = min(1.0, float(hypothesis_test.get("lookback_samples") or 0) / 20.0)
        components.append({
            "id": "one_sided_momentum_test",
            "label": "单侧动量检验",
            "probability": _clamp_probability(0.5 + (0.5 - p_value) * sample_reliability),
            "source_probability": _clamp_probability(1.0 - p_value),
            "reliability": round(sample_reliability, 3),
            "weight": 0.20,
        })
    daily_tunnel = (timing or {}).get("daily_tunnel") or {}
    if daily_tunnel.get("sample_days"):
        tunnel_strength = _clamp_probability(float(daily_tunnel.get("score", 0) or 0) / 100.0)
        sample_reliability = min(1.0, float(daily_tunnel.get("sample_days", 0) or 0) / 20.0)
        components.append({
            "id": "daily_tunnel_strength",
            "label": "日隧道协商",
            "probability": _clamp_probability(0.5 + (tunnel_strength - 0.5) * 0.55),
            "source_probability": tunnel_strength,
            "reliability": round(sample_reliability, 3),
            "weight": 0.18,
        })
    gamma_exposure = (opportunity or {}).get("gamma_exposure") or {}
    if gamma_exposure.get("available"):
        gamma_regime = str(gamma_exposure.get("regime") or "")
        gamma_risk = str(gamma_exposure.get("risk_level") or "")
        if gamma_risk == "high" or gamma_regime == "negative_high":
            gamma_probability = 0.42
        elif gamma_regime == "negative":
            gamma_probability = 0.46
        elif gamma_regime == "positive":
            gamma_probability = 0.54
        else:
            gamma_probability = 0.50
        components.append({
            "id": "gamma_exposure",
            "label": "Gamma Exposure",
            "probability": _clamp_probability(gamma_probability),
            "source_probability": _clamp_probability(gamma_probability),
            "reliability": 0.70,
            "weight": 0.10,
            "risk_level": gamma_risk,
            "regime": gamma_regime,
        })
    macro_component = panic_evidence_for_opportunity(opportunity)
    if macro_component:
        components.append(macro_component)
    stock_panic_component = stock_panic_evidence(stock_panic, macro_regime)
    if stock_panic_component:
        components.append(stock_panic_component)
    if not components:
        exposure_multiplier = max(0.0, min(1.0, float((portfolio_timing or {}).get("gross_exposure_multiplier", 1.0) or 0.0)))
        return {
            "probability": 0.5,
            "score": 50.0,
            "probability_low": 0.32,
            "probability_high": 0.68,
            "ranking_score": 0.5,
            "win_sort": {
                "historical_win_rate": 0.5,
                "historical_source": "缺少历史验证",
                "risk_reward": 0.0,
                "execution_score": 0.0,
                "sort_score": round(0.5 * (0.35 + 0.65 * exposure_multiplier), 4),
                "portfolio_exposure_multiplier": round(exposure_multiplier, 4),
                "portfolio_regime": (portfolio_timing or {}).get("regime"),
                "portfolio_risk_flags": list((portfolio_timing or {}).get("risk_flags") or []),
            },
            "components": [],
            "note": "暂无足够证据；统一概率仅用于研究排序，不构成实盘胜率承诺。",
        }
    total_weight = sum(float(item["weight"]) for item in components)
    weighted_log_odds = sum(
        float(item["weight"]) * math.log(float(item["probability"]) / (1.0 - float(item["probability"])))
        for item in components
    ) / total_weight
    probability = 1.0 / (1.0 + math.exp(-weighted_log_odds))
    effective_weight = sum(float(item["weight"]) * float(item.get("reliability", 1.0)) for item in components)
    uncertainty = min(0.18, max(0.06, 0.16 / math.sqrt(max(0.35, effective_weight))))
    relay_priority = 0.06 * float((peer_earnings or {}).get("peer_similarity", 0) or 0)
    test_priority = 0.02 if hypothesis_test.get("eligible") else 0.0
    win_sort = _win_rate_sort_metrics(opportunity, peer_earnings, hypothesis_test, probability, portfolio_timing)
    return {
        "probability": round(probability, 4),
        "score": round(probability * 100.0, 1),
        "probability_low": round(max(0.02, probability - uncertainty), 4),
        "probability_high": round(min(0.98, probability + uncertainty), 4),
        "ranking_score": round(min(0.98, probability + relay_priority + test_priority), 4),
        "win_sort": win_sort,
        "components": components,
        "note": "总览排序按新口径：历史验证胜率优先，其次统一研究概率，再看风险收益比与可执行性。排序分仍是研究胜算分，不构成实盘胜率承诺。",
    }


def _apply_unified_evidence_to_hypothesis(
    hypothesis_test: Dict[str, Any],
    research_evidence: Dict[str, Any],
) -> Dict[str, Any]:
    """Align test sizing and exit text with the same conservative evidence score."""
    result = dict(hypothesis_test)
    probability = float(research_evidence.get("probability", 0.5) or 0.5)
    result["research_probability"] = round(probability, 4)
    result["research_probability_low"] = research_evidence.get("probability_low")
    result["research_probability_high"] = research_evidence.get("probability_high")
    if not result.get("eligible"):
        entry = float(result.get("entry_reference", 0) or 0)
        stop = float(result.get("invalidation_price", 0) or 0)
        if entry > 0 and stop > 0:
            return _apply_basic_trade_risk_guard(
                result,
                target_price=_basic_risk_target_price(entry, stop),
                current_price=entry,
            )
        return result
    account_capital = float(result.get("account_capital", 2000) or 2000)
    spot = float(result.get("entry_reference", 0) or 0)
    invalidation = float(result.get("invalidation_price", 0) or 0)
    loss_per_share = max(0.01, spot - invalidation)
    sizing = _research_test_sizing(
        spot=spot,
        loss_per_share=loss_per_share,
        account_capital=account_capital,
        probability=probability,
        allow_micro=True,
    )
    shares = int(sizing["test_shares"])
    result.update({
        "risk_budget": sizing["risk_budget"],
        "risk_budget_pct": sizing["risk_budget_pct"],
        "allocation_cap_pct": sizing["allocation_cap_pct"],
        "micro_cap_pct": sizing["micro_cap_pct"],
        "sizing_mode": sizing["sizing_mode"],
        "risk_budget_breach": sizing["risk_budget_breach"],
        "shares_by_risk": sizing["shares_by_risk"],
        "shares_by_capital": sizing["shares_by_capital"],
        "test_shares": shares,
        "capital_required": sizing["capital_required"],
        "capital_usage_pct": sizing["capital_usage_pct"],
        "max_test_loss": sizing["max_test_loss"],
        "eligible": shares >= 1,
        "reason": (
            f"统一研究概率 {probability:.1%}，{sizing['reason']}"
            if shares >= 1 else sizing["reason"]
        ),
    })
    return _apply_basic_trade_risk_guard(
        result,
        target_price=_basic_risk_target_price(spot, invalidation),
        current_price=spot,
    )


def _research_context_sort_key(item: Dict[str, Any]) -> tuple[Any, ...]:
    return _shared_research_context_sort_key(item)


def _research_signal_contexts_for_universe(
    universe: str,
    limit: int = 100,
    portfolio_timing: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Build three-layer contexts for one universe using the shared win-rate ranking口径."""
    rows: List[Dict[str, Any]] = []
    hide_invalidated = os.getenv("HIDE_INVALIDATED_BY_DEFAULT", "1").lower() not in {"0", "false", "no"}
    requested_limit = max(1, int(limit or 100))
    raw_limit = min(500, max(requested_limit, requested_limit * 3 if hide_invalidated else requested_limit))
    observation = _stock_signal_observation_pool(universe, limit=raw_limit)
    opportunity_map = {item["ticker"]: item for item in observation}
    shared_portfolio_timing = portfolio_timing or portfolio_timing_gate()
    peer_relay_pools = {"speculative_peer_earnings", "pre_earnings_revision"}
    peer_symbols: List[str] = []
    if universe in peer_relay_pools:
        for symbol, item in _launch_signal_items():
            if item.get("scan_universe") != universe:
                continue
            if not _is_fresh_iso(item.get("scan_time"), 24):
                continue
            if universe == "speculative_peer_earnings":
                peer = item.get("peer_earnings")
                if not peer or int(peer.get("peer_match_version", 0) or 0) != PEER_MATCH_VERSION:
                    continue
            peer_symbols.append(symbol)
    for symbol in dict.fromkeys([*opportunity_map, *peer_symbols]):
        rows.append(_build_symbol_research_context(
            symbol,
            opportunity=opportunity_map.get(symbol),
            scan_universe=universe,
            portfolio_timing=shared_portfolio_timing,
        ))
    if hide_invalidated:
        rows = [
            row for row in rows
            if (((row.get("opportunity") or {}).get("pullback_confirmation") or {}).get("signal_stage") != "INVALIDATED")
        ]
    rows.sort(key=_research_context_sort_key)
    return rows[:requested_limit]


def _compact_research_context(row: Dict[str, Any]) -> Dict[str, Any]:
    opportunity = row.get("opportunity") or {}
    timing = row.get("timing") or {}
    event = row.get("event") or {}
    peer_mapping = row.get("peer_mapping") or timing.get("peer_mapping") or {}
    evidence = row.get("research_evidence") or {}
    win_sort = evidence.get("win_sort") or {}
    hypothesis = row.get("hypothesis_test") or {}
    macro_regime = row.get("macro_regime") or {}
    stock_panic = row.get("stock_panic") or {}
    gex = opportunity.get("gamma_exposure") or {}
    tunnel = (timing.get("daily_tunnel") or {})
    pullback_rejection = opportunity.get("pullback_rejection") or {}
    pullback_confirmation = opportunity.get("pullback_confirmation") or {}
    llm_review = row.get("llm_review") or get_cached_review(row) or {}
    current_price = opportunity.get("spot") or timing.get("last_price")
    invalidation_price = hypothesis.get("invalidation_price")
    try:
        invalidation_buffer_pct = (
            (float(current_price) - float(invalidation_price)) / float(current_price)
            if current_price and invalidation_price and float(current_price) > 0
            else None
        )
    except (TypeError, ValueError, ZeroDivisionError):
        invalidation_buffer_pct = None
    return _json_safe({
        "symbol": row.get("symbol"),
        "current_price": current_price,
        "price_as_of": opportunity.get("price_as_of"),
        "market_cap": opportunity.get("market_cap"),
        "pe": opportunity.get("trailing_pe") if opportunity.get("trailing_pe") is not None else opportunity.get("forward_pe"),
        "pe_type": "trailing" if opportunity.get("trailing_pe") is not None else (
            "forward" if opportunity.get("forward_pe") is not None else None
        ),
        "trailing_pe": opportunity.get("trailing_pe"),
        "forward_pe": opportunity.get("forward_pe"),
        "source_pools": opportunity.get("source_pools") or [],
        "peer_mapping": peer_mapping,
        "peer_mapping_available": bool(peer_mapping),
        "peer_mapping_source": peer_mapping.get("mapping_source") if isinstance(peer_mapping, dict) else None,
        "peer_group_label": peer_mapping.get("group_label") if isinstance(peer_mapping, dict) else None,
        "entry_zone_low": opportunity.get("entry_zone_low"),
        "entry_zone_high": opportunity.get("entry_zone_high"),
        "risk_level": opportunity.get("risk_level"),
        "liquidity_score": opportunity.get("liquidity_score"),
        "candidate_label": opportunity.get("candidate_label"),
        "stock_signal_score": opportunity.get("opportunity_score"),
        "trend_30d": opportunity.get("trend_30d"),
        "recent_closes": opportunity.get("recent_closes") or [],
        "launch_signal_status": timing.get("signal_cn") or timing.get("signal") or "等待启动",
        "launch_signal_score": timing.get("launch_score"),
        "event_radar_status": event.get("signal_cn") or event.get("signal") or "事件证据未知",
        "macro_regime": macro_regime,
        "macro_vix_value": macro_regime.get("value"),
        "macro_vix_display": macro_regime.get("display_name"),
        "macro_regime_label": macro_regime.get("label_cn"),
        "macro_regime_mode": macro_regime.get("mode"),
        "macro_regime_mode_cn": macro_regime.get("mode_cn"),
        "macro_panic_score": macro_regime.get("panic_score"),
        "macro_systemic_crisis": macro_regime.get("systemic_crisis"),
        "stock_panic": stock_panic,
        "stock_vix_equivalent": stock_panic.get("stock_vix_equivalent"),
        "stock_panic_score": stock_panic.get("stock_panic_score"),
        "stock_panic_level": stock_panic.get("panic_level"),
        "stock_panic_level_cn": stock_panic.get("panic_level_cn"),
        "stock_panic_drivers": stock_panic.get("drivers") or [],
        "stock_panic_hv5": stock_panic.get("hv5"),
        "stock_panic_hv20": stock_panic.get("hv20"),
        "stock_panic_beta": stock_panic.get("beta_spy_qqq"),
        "stock_panic_drawdown_60d": stock_panic.get("drawdown_60d"),
        "stock_panic_volume_spike": stock_panic.get("volume_spike"),
        "gamma_exposure": gex if gex.get("available") else {},
        "gex_level": gex.get("risk_level") if gex.get("available") else None,
        "gex_regime": gex.get("regime") if gex.get("available") else None,
        "unified_score": evidence.get("score"),
        "overall_sort_score": win_sort.get("sort_score"),
        "unified_probability": evidence.get("probability"),
        "unified_probability_low": evidence.get("probability_low"),
        "unified_probability_high": evidence.get("probability_high"),
        "win_sort": win_sort,
        "evidence_components": evidence.get("components") or [],
        "evidence_note": evidence.get("note"),
        "historical_win_rate": win_sort.get("historical_win_rate"),
        "empirical_win_rate": win_sort.get("empirical_win_rate"),
        "proxy_win_rate": win_sort.get("proxy_win_rate"),
        "historical_is_proxy": win_sort.get("historical_is_proxy"),
        "historical_source": win_sort.get("historical_source"),
        "probability_for_ranking": win_sort.get("probability_for_ranking"),
        "expected_value": win_sort.get("expected_value"),
        "expected_value_gross": win_sort.get("expected_value_gross"),
        "sort_weights": win_sort.get("sort_weights"),
        "overall_status": row.get("decision_grade"),
        "invalidation_price": invalidation_price,
        "invalidation_buffer_pct": invalidation_buffer_pct,
        "test_shares": hypothesis.get("test_shares"),
        "requested_qty": hypothesis.get("requested_qty"),
        "allowed_qty": hypothesis.get("allowed_qty"),
        "capital_required": hypothesis.get("capital_required"),
        "max_test_loss": hypothesis.get("max_test_loss"),
        "sizing_mode": hypothesis.get("sizing_mode"),
        "open_risk_status": hypothesis.get("open_risk_status"),
        "open_risk_reason": hypothesis.get("open_risk_reason"),
        "trade_risk_check": hypothesis.get("trade_risk_check"),
        "target_price": hypothesis.get("target_price"),
        "reward_risk_ratio": hypothesis.get("reward_risk_ratio"),
        "stop_loss_pct": hypothesis.get("stop_loss_pct"),
        "exit_action": hypothesis.get("exit_action"),
        "exit_reason": hypothesis.get("exit_reason"),
        "exit_risk_check": hypothesis.get("exit_risk_check"),
        "short_reason": row.get("decision"),
        "risk_flags": (["event_risk_unknown"] if not event or not _is_fresh_iso(event.get("scan_time"), 24) else []) + (opportunity.get("risk_flags") or [])[:3],
        "snapshot_time": timing.get("scan_time") or (opportunity.get("run_id") or ""),
        "daily_tunnel": tunnel,
        "daily_tunnel_score": tunnel.get("score"),
        "daily_tunnel_label": tunnel.get("label"),
        "pullback_rejection_status": pullback_rejection.get("signal_stage"),
        "pullback_rejection_score": pullback_rejection.get("score"),
        "pullback_confirmation_status": pullback_confirmation.get("signal_stage"),
        "pullback_confirmation_score": pullback_confirmation.get("confirmation_score"),
        "waiting_breakout_price": pullback_confirmation.get("waiting_breakout_price") or pullback_rejection.get("waiting_breakout_price"),
        "nearest_support": pullback_rejection.get("nearest_support"),
        "portfolio_timing": row.get("portfolio_timing") or {},
        "portfolio_regime": (row.get("portfolio_timing") or {}).get("regime"),
        "portfolio_regime_cn": (row.get("portfolio_timing") or {}).get("regime_cn"),
        "portfolio_gross_exposure_multiplier": (row.get("portfolio_timing") or {}).get("gross_exposure_multiplier"),
        "portfolio_max_gross_exposure_pct": (row.get("portfolio_timing") or {}).get("max_gross_exposure_pct"),
        "portfolio_risk_flags": (row.get("portfolio_timing") or {}).get("risk_flags") or [],
        "llm_review": llm_review,
        "detail_url": f"/research-signal-hub/{row.get('symbol')}?universe={timing.get('scan_universe') or row.get('scan_universe') or ''}",
    })


def _stock_signal_report_index() -> Dict[str, tuple[Path, Dict[str, Any]]]:
    """Load latest report payloads once for selector ranking."""
    selected_paths: Dict[str, Path] = {}
    combined_path: Optional[Path] = None
    for path in _stock_signal_report_candidates():
        match = re.search(r"options_screening_results_(.+?)_\d{6}\.json$", path.name)
        report_universe = match.group(1) if match else ""
        if not report_universe:
            payload = _load_json_file(path) or {}
            report_universe = _stock_signal_report_universe(payload, path)
        if report_universe and report_universe not in selected_paths:
            selected_paths[report_universe] = path
        if report_universe == "sp100_komp_soxx" and combined_path is None:
            combined_path = path
    if combined_path is not None:
        for universe in _STOCK_SIGNAL_SPLIT_POOLS:
            direct = selected_paths.get(universe)
            if direct is None or _stock_signal_run_id(direct) < _stock_signal_run_id(combined_path):
                selected_paths[universe] = combined_path
    index: Dict[str, tuple[Path, Dict[str, Any]]] = {}
    for universe, path in selected_paths.items():
        index[universe] = (path, _load_json_file(path) or {})
    return index


def _universe_recent_win_metrics(
    universe: str,
    selected_report: Optional[tuple[Path, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Summarize latest completed evidence without rebuilding the full three-layer table."""
    if selected_report is None:
        selected_path, payload, _derived = _stock_signal_find_report(universe)
    else:
        selected_path, payload = selected_report
    timestamp = str(payload.get("timestamp") or "") if payload else ""
    if selected_path is None:
        return {
            "candidate_count": 0,
            "recent_win_score": 0.0,
            "historical_win_rate": 0.0,
            "unified_probability": 0.0,
            "risk_reward_score": 0.0,
            "execution_score": 0.0,
            "gex_coverage": 0.0,
            "gex_available_count": 0,
            "top_symbol": "",
            "report_run_id": "",
            "report_timestamp": timestamp,
            "status": "no_completed_report",
        }
    pool_results = _stock_signal_filter_results(payload, universe)
    if not pool_results:
        return {
            "candidate_count": 0,
            "recent_win_score": 0.0,
            "historical_win_rate": 0.0,
            "unified_probability": 0.0,
            "risk_reward_score": 0.0,
            "execution_score": 0.0,
            "gex_coverage": 0.0,
            "gex_available_count": 0,
            "top_symbol": "",
            "report_run_id": _stock_signal_run_id(selected_path),
            "report_timestamp": timestamp,
            "status": "no_current_candidates",
        }
    bullish = [
        result for result in pool_results
        if str(result.get("primary_strategy")) in _STOCK_SIGNAL_BULLISH_STRATEGIES
        or float(result.get("trend_30d", 0) or 0) > 0
    ]
    ranked_results = sorted(
        bullish or pool_results,
        key=lambda result: (
            -float(result.get("final_score", result.get("research_score", 0)) or 0),
            -float(result.get("trend_30d", 0) or 0),
        ),
    )
    top_n = ranked_results[: min(10, len(ranked_results))]

    historical_values: List[float] = []
    proxy_values: List[float] = []
    unified_values: List[float] = []
    rr_values: List[float] = []
    execution_values: List[float] = []

    def valid_probability(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number <= 0.0 or number > 1.0:
            return None
        return number

    for item in top_n:
        historical = valid_probability(item.get("historical_barrier_win_rate"))
        proxy = None
        if historical is None:
            proxy = valid_probability(item.get("model_pop")) or valid_probability(item.get("pop"))
        unified = valid_probability(item.get("pop")) or valid_probability(item.get("model_pop")) or historical
        expected_value = float(item.get("expected_value", 0) or 0)
        max_loss = float(item.get("max_loss", 0) or 0)
        rr = max(0.0, min(1.0, (expected_value / max_loss + 0.25) / 2.25)) if max_loss > 0 else None
        liquidity_score = float(item.get("liquidity_score", 0) or 0)
        execution = 0.55 + min(0.35, liquidity_score * 0.35) if liquidity_score > 0 else None
        for bucket, value in (
            (historical_values, historical),
            (proxy_values, proxy),
            (unified_values, unified),
            (rr_values, rr),
            (execution_values, execution),
        ):
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                bucket.append(max(0.0, min(1.0, number)))

    def mean(values: List[float], default: float = 0.0) -> float:
        return sum(values) / len(values) if values else default

    has_win_data = bool(historical_values or proxy_values or unified_values)
    historical = mean(historical_values) if historical_values else None
    proxy = mean(proxy_values) if proxy_values else None
    unified = mean(unified_values) if unified_values else None
    risk_reward = mean(rr_values) if rr_values else None
    execution = mean(execution_values) if execution_values else None
    gex_available = sum(1 for item in ranked_results if ((item.get("gamma_exposure") or {}).get("available")))
    gex_coverage = gex_available / len(ranked_results) if ranked_results else 0.0
    if has_win_data:
        empirical_component = historical if historical is not None else 0.5
        proxy_component = proxy if proxy is not None else 0.5
        sort_score = (
            empirical_component * 0.34
            + proxy_component * 0.12
            + (unified or 0.5) * 0.28
            + (risk_reward or 0.0) * 0.18
            + (execution or 0.0) * 0.08
        )
        status = "ready"
    else:
        sort_score = None
        status = "insufficient_win_data"
    universe_info = payload.get("universe") or {}
    source_text = str(universe_info.get("source") or payload.get("source") or "")
    source_kind = "unknown"
    if source_text.startswith("live:"):
        source_kind = "live"
    elif source_text.startswith("fallback:"):
        source_kind = "fallback"
    elif source_text.startswith("archive:") or ";snapshot:" in source_text:
        source_kind = "archive"
    elif source_text.startswith("curated:"):
        source_kind = "curated"
    return {
        "candidate_count": len(ranked_results),
        "recent_win_score": round(sort_score, 4) if sort_score is not None else None,
        "historical_win_rate": round(historical, 4) if historical is not None else None,
        "proxy_win_rate": round(proxy, 4) if proxy is not None else None,
        "historical_source": "期权历史障碍检验" if historical is not None else ("POP/模型概率代理" if proxy is not None else "缺少历史验证"),
        "historical_is_proxy": historical is None and proxy is not None,
        "unified_probability": round(unified, 4) if unified is not None else None,
        "risk_reward_score": round(risk_reward, 4) if risk_reward is not None else None,
        "execution_score": round(execution, 4) if execution is not None else None,
        "gex_coverage": round(gex_coverage, 4),
        "gex_available_count": gex_available,
        "source": source_text,
        "source_kind": source_kind,
        "source_is_fallback": source_kind == "fallback",
        "source_audit_note": "历史回测需使用归档快照；fallback 仅为离线兜底。" if source_kind == "fallback" else "",
        "top_symbol": ranked_results[0].get("ticker", "") if has_win_data else "",
        "report_run_id": _stock_signal_run_id(selected_path),
        "report_timestamp": timestamp,
        "status": status,
    }


def _ranked_research_universes() -> List[Dict[str, Any]]:
    """Return catalog items ordered by recent research win profile, then fallback priority."""
    now = time.time()
    if _SUMMARY_SNAPSHOT_ENABLED and _research_universe_rank_cache.get("expires_at", 0.0) > now:
        return [dict(item) for item in (_research_universe_rank_cache.get("items") or [])]
    started = time.perf_counter()
    ranked: List[Dict[str, Any]] = []
    query_started = time.perf_counter()
    report_index = _stock_signal_report_index()
    for item in list_research_universes():
        universe = str(item.get("universe") or "")
        metrics = _universe_recent_win_metrics(universe, report_index.get(universe))
        enriched = {**item, "recent_win": metrics}
        ranked.append(enriched)
    query_ms = round((time.perf_counter() - query_started) * 1000, 2)
    ranked.sort(
        key=lambda item: (
            -float((item.get("recent_win") or {}).get("recent_win_score", 0) or 0),
            -float((item.get("recent_win") or {}).get("historical_win_rate", 0) or 0),
            -float((item.get("recent_win") or {}).get("proxy_win_rate", 0) or 0),
            -float((item.get("recent_win") or {}).get("unified_probability", 0) or 0),
            -int((item.get("recent_win") or {}).get("candidate_count", 0) or 0),
            int(item.get("priority", 999)),
            str(item.get("universe") or ""),
        )
    )
    for index, item in enumerate(ranked, start=1):
        item["dynamic_rank"] = index
        item["default_selected"] = index == 1
    if _SUMMARY_SNAPSHOT_ENABLED:
        _research_universe_rank_cache["items"] = [dict(item) for item in ranked]
        _research_universe_rank_cache["expires_at"] = now + _SUMMARY_SNAPSHOT_TTL_SECONDS
    _perf_log(
        "research_universe_rank",
        load_latest_snapshot_ms=query_ms,
        query_candidates_ms=query_ms,
        aggregate_signals_ms=0,
        fetch_external_data_ms=0,
        serialize_response_ms=0,
        candidate_count=sum(int((item.get("recent_win") or {}).get("candidate_count", 0) or 0) for item in ranked),
        db_query_count=0,
        external_call_count=0,
        cache_hit=False,
        cache_miss=True,
        total_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    return ranked


def _daily_tunnel_from_opportunity(opportunity: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Compute daily tunnel from first-layer closes when timing did not keep a row."""
    if not opportunity:
        return None
    closes: List[float] = []
    for value in opportunity.get("recent_closes") or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            closes.append(number)
    if len(closes) < 25:
        return None
    frame = pd.DataFrame({"Close": closes, "Volume": [0] * len(closes)})
    tunnel = compute_daily_tunnel_score(frame)
    if tunnel.get("sample_days"):
        tunnel["source"] = "opportunity_recent_closes"
        return tunnel
    return None


def _build_symbol_research_context(
    symbol: str,
    opportunity: Optional[Dict[str, Any]] = None,
    scan_universe: Optional[str] = None,
    portfolio_timing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Combine candidate quality, timing, and event evidence into one decision card."""
    symbol = symbol.upper()
    macro_regime = get_vix_regime()
    portfolio_timing = portfolio_timing or portfolio_timing_gate()
    opportunity = opportunity if scan_universe else (opportunity or _latest_stock_opportunity(symbol))
    timing = _latest_launch_signals.get(symbol)
    if scan_universe and timing and timing.get("scan_universe") != scan_universe:
        timing = None
    fallback_daily_tunnel = None
    if not (timing or {}).get("daily_tunnel"):
        fallback_daily_tunnel = _daily_tunnel_from_opportunity(opportunity)
        if fallback_daily_tunnel:
            timing = {
                "symbol": symbol,
                "signal": "neutral",
                "signal_cn": "等待启动",
                "launch_score": 0.0,
                "daily_tunnel": fallback_daily_tunnel,
                "last_price": opportunity.get("spot") if opportunity else 0,
                "reason": "未命中启动信号；日隧道协商按第一层行情试算。",
                "scan_universe": scan_universe,
                "market_data_source": "opportunity_recent_closes",
            }
    peer_earnings = (timing or {}).get("peer_earnings")
    if peer_earnings and int(peer_earnings.get("peer_match_version", 0) or 0) != PEER_MATCH_VERSION:
        peer_earnings = None
    peer_mapping = (timing or {}).get("peer_mapping")
    event = _latest_event_signals.get(symbol)
    conflicts = []
    if not event or not _is_fresh_iso(event.get("scan_time"), 24):
        conflicts.append("事件证据未知或已过期，需复核最新公告和财报；不代表无风险。")
        if event:
            event = {**event, "signal_cn": "旧事件告警待复核"}
    if macro_regime.get("mode") == "crisis_guard":
        conflicts.append("VIX 进入危机级别，启用系统性危机过滤，降低承接优先级。")
    elif macro_regime.get("mode") == "panic_reclaim":
        conflicts.append("VIX 处于极端恐慌区，候选按恐慌承接模式复核，禁止无条件追单。")
    if opportunity and opportunity.get("risk_level") == "高":
        conflicts.append("机会层风险较高，需要缩小仓位或等待回踩。")
    if timing and timing.get("signal") in {"watch", "neutral"}:
        conflicts.append("机会层与短周期择时尚未共振。")
    if event and str(event.get("signal") or "") == "avoid":
        conflicts.append("事件雷达提示回避，暂缓介入。")
    if not opportunity and peer_earnings:
        grade, decision = "同行接力观察", (
            f"{peer_earnings.get('leader_symbol')} 已先发财报验证 {peer_earnings.get('group_label')}，"
            f"{peer_earnings.get('sublane_relation_display') or peer_earnings.get('sublane_relation') or '同行赛道'}；"
            f"{peer_earnings.get('transmission_direction') or '传导方向待确认'}。{symbol} 尚待披露，纳入人工复核。"
        )
    elif not opportunity:
        grade, decision = "数据不足", "尚未进入实股观察池"
    elif event and str(event.get("signal") or "") == "avoid":
        grade, decision = "风险偏高", "事件风险优先，暂缓判断"
    elif (
        timing
        and timing.get("signal") in {"strong_buy", "buy"}
        and opportunity.get("opportunity_tier") == "core"
        and opportunity.get("risk_level") != "高"
    ):
        grade, decision = "计划候选", "机会质量与启动择时共振，可进入人工交易计划"
    elif timing and timing.get("signal") in {"strong_buy", "buy"} and opportunity.get("risk_level") == "高":
        grade, decision = "等待回踩", "启动迹象已出现，但风险或过热惩罚较高，等待回踩后复核"
    elif timing and timing.get("signal") in {"strong_buy", "buy"}:
        grade, decision = "等待复核", "已出现启动迹象，等待个股价位与风险复核"
    else:
        grade, decision = "等待确认", "机会层已纳入观察，等待启动信号"
    hypothesis_test = _equity_hypothesis_test_plan(opportunity, timing, grade)
    stock_panic = stock_panic_proxy(symbol, opportunity, macro_regime=macro_regime, event=event)
    research_evidence = _research_evidence_summary(
        opportunity,
        peer_earnings,
        hypothesis_test,
        timing,
        stock_panic=stock_panic,
        macro_regime=macro_regime,
        portfolio_timing=portfolio_timing,
    )
    hypothesis_test = _apply_unified_evidence_to_hypothesis(hypothesis_test, research_evidence)
    # Extract pre_earnings data if this is a pre-earnings signal
    pre_earnings = None
    if timing and timing.get("signal_type") == "pre_earnings_expectation_revision":
        pre_earnings = timing

    # For pre-earnings pool, generate appropriate grade/decision
    if pre_earnings and not opportunity:
        in_window = pre_earnings.get("in_target_window", False)
        days = pre_earnings.get("report", {}).get("trading_days_to_report", 999)
        score = pre_earnings.get("score", {}).get("final", 0)
        if in_window:
            grade = "pre_earnings_in_window"
            decision = f"财报前 {days} 交易日，进入目标窗口。评分 {score:.1%}"
        else:
            grade = "pre_earnings_upcoming"
            decision = f"财报前 {days} 交易日，尚未进入目标窗口。评分 {score:.1%}"

    result = {
        "symbol": symbol,
        "scan_universe": scan_universe,
        "decision_grade": grade,
        "decision": decision,
        "opportunity": opportunity,
        "timing": timing,
        "peer_mapping": peer_mapping,
        "peer_earnings": peer_earnings,
        "pre_earnings": pre_earnings,
        "event": event,
        "macro_regime": macro_regime,
        "portfolio_timing": portfolio_timing,
        "stock_panic": stock_panic,
        "conflicts": conflicts,
        "hypothesis_test": hypothesis_test,
        "research_evidence": research_evidence,
        "dimensions": {
            "opportunity": bool(opportunity),
            "timing": bool(timing and timing.get("signal") in {"strong_buy", "buy"}),
            "daily_tunnel": bool((timing or {}).get("daily_tunnel")),
            "pullback_rejection": bool((opportunity or {}).get("pullback_rejection")),
            "pullback_confirmation": bool((opportunity or {}).get("pullback_confirmation")),
            "peer_earnings": bool(peer_earnings),
            "peer_mapping": bool(peer_mapping),
            "pre_earnings": bool(pre_earnings),
            "event_clear": bool(event and _is_fresh_iso(event.get("scan_time"), 24)
                                and str(event.get("signal") or "") != "avoid"),
        },
    }
    cached_review = get_cached_review(result)
    if cached_review:
        result["llm_review"] = cached_review
    return result


def _is_fresh_iso(value: Any, max_age_hours: float = 24.0) -> bool:
    """Return whether an ISO-like timestamp is within the freshness window."""
    if not value:
        return False
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone().replace(tzinfo=None)
        return (datetime.now() - stamp).total_seconds() <= max_age_hours * 3600
    except (TypeError, ValueError):
        return False


def _stock_signal_report_is_fresh(universe: str, max_age_hours: float = 24.0) -> bool:
    """Check whether the latest first-layer report can be reused."""
    selected_path, payload, _derived = _stock_signal_find_report(universe)
    if selected_path is None:
        return False
    if _is_fresh_iso(payload.get("timestamp"), max_age_hours):
        return True
    return time.time() - selected_path.stat().st_mtime <= max_age_hours * 3600


def _stock_signal_report_has_pullback_inputs(universe: str, sample_size: int = 12) -> bool:
    """Check whether a report contains OHLCV and pullback fields for one-click runs."""
    selected_path, payload, _derived = _stock_signal_find_report(universe)
    if selected_path is None:
        return False
    results = [
        item for item in _stock_signal_filter_results(payload, universe)
        if str(item.get("ticker") or "").strip() and float(item.get("spot", 0) or 0) > 0
    ][:max(1, sample_size)]
    if not results:
        return False
    required = max(1, min(len(results), int(len(results) * 0.5 + 0.999)))
    with_ohlcv = sum(1 for item in results if len(item.get("recent_ohlcv") or []) >= 5)
    with_pullback_fields = sum(
        1 for item in results
        if "pullback_rejection" in item and "pullback_confirmation" in item
    )
    return with_ohlcv >= required and with_pullback_fields >= required


def _stock_signal_report_reuse_status(universe: str, max_age_hours: float = 24.0) -> Dict[str, Any]:
    """Explain whether the first-layer report is safe to reuse for the hub run."""
    selected_path, payload, _derived = _stock_signal_find_report(universe)
    fresh = _stock_signal_report_is_fresh(universe, max_age_hours)
    has_pullback_inputs = _stock_signal_report_has_pullback_inputs(universe)
    try:
        from market_calendar import most_recent_session
        session = most_recent_session().isoformat()
        sample = _stock_signal_filter_results(payload, universe)
        current_count = sum(str(item.get("price_as_of") or "") >= session for item in sample)
        price_coverage = current_count / len(sample) if sample else 0.0
        current_prices = bool(sample) and price_coverage >= _daily_min_price_coverage()
    except Exception:
        current_prices = False
        price_coverage = 0.0
    reason = "ok"
    if selected_path is None:
        reason = "missing_report"
    elif not fresh:
        reason = "stale_report"
    elif not has_pullback_inputs:
        reason = "missing_recent_ohlcv_or_pullback_fields"
    elif not current_prices:
        reason = "stale_or_unverified_price_evidence"
    return {
        "reusable": bool(fresh and has_pullback_inputs and current_prices),
        "fresh": bool(fresh),
        "has_pullback_inputs": bool(has_pullback_inputs),
        "current_prices": bool(current_prices),
        "price_coverage": round(price_coverage, 4),
        "reason": reason,
        "report_path": str(selected_path) if selected_path else "",
        "timestamp": payload.get("timestamp") if payload else None,
    }


def _event_scan_is_fresh(universe: str, symbols: List[str], max_age_hours: float = 24.0) -> bool:
    """Check event freshness and ensure the current observation pool was covered."""
    scan = _latest_event_scans.get(universe)
    covered = {str(symbol).upper() for symbol in (scan or {}).get("symbols", [])}
    required = {str(symbol).upper() for symbol in symbols}
    return _is_fresh_iso((scan or {}).get("scan_time"), max_age_hours) and required.issubset(covered)


def _run_research_signal_hub_job(job_id: str, payload: ResearchSignalHubRunRequest) -> None:
    """Refresh evidence, run timing scan, and assemble decision cards."""
    job = _research_signal_hub_jobs[job_id]
    universe = payload.universe.strip().lower()
    peer_relay_pool = universe == "speculative_peer_earnings"
    watchlist_pool = universe == "watchlist"
    daily = bool(job.get("daily_price_cache_only"))
    evidence_scope = external_data_scope(False if daily else external_data_allowed())
    evidence_scope.__enter__()
    data_warnings = []

    def update(phase: str, message: str, progress: float) -> None:
        _check_scan_budget(job)
        job.update({"phase": phase, "message": message, "progress": round(progress, 3)})

    try:
        report_status = _stock_signal_report_reuse_status(universe)
        report_was_reusable = bool(report_status.get("reusable"))
        if peer_relay_pool:
            update("opportunity", "专项池：跳过全量期权底层刷新，直接运行同行财报接力扫描。", 0.43)
        elif watchlist_pool:
            update("opportunity", "自选池：跳过期权报告刷新，直接使用自选标的运行实股三层信号。", 0.43)
        elif payload.refresh_evidence and not report_was_reusable:
            update("opportunity", "第一层：正在更新实股机会证据...", 0.03)
            child_job_id = f"stock_signal_{uuid.uuid4().hex[:10]}"
            child_run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:2]}_{uuid.uuid4().hex[:6]}"
            _stock_signal_update_jobs[child_job_id] = {
                "job_id": child_job_id,
                "run_id": child_run_id,
                "universe": universe,
                "status": "queued",
                "message": "由三维信号编排器发起",
                "progress": 0.0,
                "started_at": datetime.utcnow().isoformat() + "Z",
                "reuse_session_results": payload.reuse_session_results,
                "option_chain_cache_minutes": payload.option_chain_cache_minutes,
                "deadline_monotonic": job.get("deadline_monotonic"),
                "daily_price_cache_only": job.get("daily_price_cache_only", False),
            }
            if daily:
                _stock_signal_update_jobs[child_job_id]["deadline_monotonic"] = min(
                    job.get("deadline_monotonic") or float("inf"),
                    time.monotonic() + max(30, int(os.getenv("VIBE_DAILY_OPPORTUNITY_TIMEOUT", "180"))),
                )
            job["stock_signal_job_id"] = child_job_id
            job["stock_signal_refresh_reason"] = report_status.get("reason")
            child_thread = threading.Thread(
                target=_run_stock_signal_update_job,
                args=(child_job_id, universe),
                daemon=True,
            )
            child_thread.start()
            while child_thread.is_alive():
                child_progress = float(_stock_signal_update_jobs[child_job_id].get("progress", 0.0) or 0.0)
                update(
                    "opportunity",
                    str(_stock_signal_update_jobs[child_job_id].get("message") or "第一层：正在更新实股机会证据..."),
                    0.03 + min(0.4, child_progress * 0.4),
                )
                time.sleep(0.8)
            child_thread.join()
            child = _stock_signal_update_jobs[child_job_id]
            if child.get("status") != "completed":
                if not daily:
                    raise RuntimeError(str(child.get("message") or "第一层机会证据更新失败"))
                data_warnings.append("第一层更新未完成，复用已有报告；旧行情候选不进入当天排名")
            report_status = _stock_signal_report_reuse_status(universe)
            report_was_reusable = bool(report_status.get("reusable"))
        else:
            update(
                "opportunity",
                "第一层：最近 24 小时内已更新，复用最新行情证据。"
                if report_was_reusable
                else "第一层：按请求复用已有行情证据。",
                0.43,
            )

        update("opportunity", "第一层：正在读取严格机会观察池...", 0.45)
        observation = [] if peer_relay_pool else _stock_signal_observation_pool(universe, limit=payload.top)
        if daily and not observation and not peer_relay_pool:
            raise RuntimeError("该池没有可用机会报告，已跳过；不重复拉取全池行情")
        symbols = [item["ticker"] for item in observation]
        opportunity_map = {item["ticker"]: item for item in observation}

        update("timing", f"第二层：正在对 {len(symbols)} 只机会候选运行启动择时...", 0.58)
        timing = scan_launch_signals(
            universe=universe,
            symbols=symbols,
            threshold=0.5,
            top=payload.top,
            opportunity_map=opportunity_map,
            scan_mode="peer_earnings" if peer_relay_pool else "combined",
            peer_group="all",
            peer_cache_path=RUNS_DIR / "_peer_earnings_signal_cache.json",
            cache_only=daily,
        )
        timing["input_source"] = "stock_observation_pool"
        if (timing.get("peer_earnings") or {}).get("evidence_status") == "unknown":
            data_warnings.append("同行财报缓存不可用，该证据未参与本次判断，需单独复核")
        _check_scan_budget(job)
        _store_launch_result(universe, timing, symbols, opportunity_map)
        peer_symbols = [
            str(item.get("symbol") or "")
            for item in timing.get("signals", [])
            if item.get("peer_earnings")
        ]
        peer_mapping_coverage = ((timing.get("peer_earnings") or {}).get("target_mapping_coverage") or {})
        decision_symbols = list(dict.fromkeys([*symbols, *peer_symbols])) if peer_relay_pool else symbols

        event_was_fresh = _event_scan_is_fresh(universe, decision_symbols)
        event_deferred = bool(daily and not event_was_fresh and decision_symbols)
        if event_deferred:
            data_warnings.append("事件证据未知，后台补充公告/新闻；不代表无风险")
            update("event", "事件证据待后台补充，先完成缓存内技术分析。", 0.84)
        elif not event_was_fresh and decision_symbols:
            update("event", f"事件复核：正在采集 {len(symbols)} 只候选的最新公告/新闻事件与行情确认...", 0.76)
            _run_event_driven_scan_sync(
                EventDrivenScanRequest(
                    universe=universe,
                    tickers=decision_symbols,
                    top=min(100, max(1, len(decision_symbols))),
                    news_per_ticker=3,
                    lookback_hours=96,
                    cache_ttl_minutes=0,
                    sleep_seconds=0,
                    workers=12,
                    include_prices=True,
                    auto_calibration=True,
                    use_llm_scoring=bool(payload.enable_event_llm_review),
                    timeout_seconds=900,
                )
            )
        else:
            update("event", "事件复核：最近 24 小时内已扫描，复用最新公告/新闻事件结论。", 0.84)

        update("decision", "第三层：正在整合机会质量、启动择时与事件风险...", 0.88)
        shared_portfolio_timing = portfolio_timing_gate()
        rows = [
            _build_symbol_research_context(
                symbol,
                opportunity=opportunity_map.get(symbol),
                scan_universe=universe,
                portfolio_timing=shared_portfolio_timing,
            )
            for symbol in decision_symbols
        ]
        rows.sort(key=_research_context_sort_key)
        llm_reviews: Dict[str, Dict[str, Any]] = {}
        if payload.enable_llm_review and payload.llm_review_top > 0 and rows:
            update("llm_review", f"DeepSeek 决策辅助：正在复核 Top {min(payload.llm_review_top, len(rows))} 张决策卡...", 0.94)
            llm_reviews = review_top_contexts(rows, limit=payload.llm_review_top, cache_only=daily)
            for row in rows:
                symbol = str(row.get("symbol") or "")
                if symbol in llm_reviews:
                    row["llm_review"] = llm_reviews[symbol]
        grades: Dict[str, int] = {}
        for row in rows:
            grade = str(row.get("decision_grade") or "未分类")
            grades[grade] = grades.get(grade, 0) + 1
        _check_scan_budget(job)
        job.update({
            "status": "completed",
            "phase": "completed",
            "message": "三层分析完成，已生成整合决策清单。",
            "progress": 1.0,
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "result": {
                "universe": universe,
                "macro_regime": get_vix_regime(),
                "portfolio_timing": shared_portfolio_timing,
                "candidate_count": len(observation),
                "timing_signal_count": int(timing.get("n_signals", 0) or 0),
                "peer_earnings_signal_count": len(peer_symbols),
                "peer_mapping_coverage": peer_mapping_coverage,
                "decision_count": len(rows),
                "decision_grades": grades,
                "scan_time": timing.get("scan_time"),
                "market_evidence_refreshed": bool(payload.refresh_evidence and job.get("stock_signal_job_id")),
                "market_evidence_status": report_status,
                "data_warnings": data_warnings,
                "optional_evidence_pending": daily,
                "event_evidence_refreshed": bool(not daily and not event_was_fresh and decision_symbols),
                "event_llm_review_enabled": bool(payload.enable_event_llm_review),
                "event_llm_review_used": bool(not daily and payload.enable_event_llm_review and not event_was_fresh and decision_symbols),
                "llm_review_enabled": bool(payload.enable_llm_review),
                "llm_review_count": len(llm_reviews),
                "llm_review_available_count": sum(1 for item in llm_reviews.values() if item.get("available")),
            },
        })
    except Exception as exc:
        job.update({
            "status": "failed",
            "phase": "failed",
            "message": f"三层分析失败：{str(exc)[-1200:]}",
            "progress": 1.0,
            "finished_at": datetime.utcnow().isoformat() + "Z",
        })
        if job.get("cancel_requested") and job.get("stock_signal_job_id"):
            stock_job = _stock_signal_update_jobs.get(job["stock_signal_job_id"], {})
            stock_job["cancel_requested"] = True
            if "child_thread" in locals():
                child_thread.join(timeout=10)
                if child_thread.is_alive():
                    with _AUTO_SCAN_LOCK:
                        _DAILY_ACTIVE_WORKERS[job["stock_signal_job_id"]] = child_thread
    finally:
        evidence_scope.__exit__(None, None, None)


@app.get("/research-signal-hub/{symbol}", dependencies=[Depends(require_auth)])
async def research_signal_hub_symbol(symbol: str, universe: str = Query("")):
    """Return the three-layer research context for one equity."""
    with external_data_scope(False):
        safe_symbol = _safe_event_ticker(symbol)
        opportunity = None
        if universe:
            observation = _stock_signal_observation_pool(universe, limit=200)
            opportunity = {item["ticker"]: item for item in observation}.get(safe_symbol)
        return _json_safe(_build_symbol_research_context(safe_symbol, opportunity=opportunity, scan_universe=universe or None))


@app.get("/research-signal-hub", dependencies=[Depends(require_auth)])
async def research_signal_hub_overview(
    universe: str = Query("spx"),
    limit: int = Query(50, ge=1, le=500),
    compact: bool = Query(False),
):
    """Return the intersection overview for candidate, timing, and event layers."""
    started = time.perf_counter()
    with external_data_scope(False):
        macro_regime = get_vix_regime()
        portfolio_timing = portfolio_timing_gate()
        rows = _research_signal_contexts_for_universe(universe, limit=limit, portfolio_timing=portfolio_timing)
        total_candidate_count = max(len(rows), int((_universe_recent_win_metrics(universe).get("candidate_count") or 0)))
        aggregate_ms = round((time.perf_counter() - started) * 1000, 2)
        payload_rows = [_compact_research_context(row) for row in rows] if compact else rows
    _perf_log(
        "research_signal_hub_overview",
        load_latest_snapshot_ms=0,
        query_candidates_ms=0,
        aggregate_signals_ms=aggregate_ms,
        fetch_external_data_ms=0,
        serialize_response_ms=0,
        candidate_count=len(rows),
        db_query_count=0,
        external_call_count=0,
        cache_hit=True,
        cache_miss=False,
        compact=compact,
        limit=limit,
        total_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    payload = {
        "universe": universe,
        "rows": payload_rows,
        "compact": compact,
        "macro_regime": macro_regime,
        "portfolio_timing": portfolio_timing,
        "snapshot": {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "stale": False,
            "latest_run_status": "completed",
            "candidate_count": len(rows),
            "total_candidate_count": total_candidate_count,
            "display_limit": limit,
        },
        "ranking_note": "三维总览按研究胜率口径排序：历史验证胜率优先，其次统一研究概率，再看风险收益比、可执行性与宏观VIX恐慌状态。",
    }
    return _json_safe(payload)


@app.post("/overnight-alpha/summary", dependencies=[Depends(require_auth)])
async def overnight_alpha_summary(payload: OvernightAlphaSummaryRequest):
    """Validate cockpit candidates by close-to-next-open beta-adjusted alpha."""
    try:
        with external_data_scope(False):
            rows = [item.model_dump() for item in payload.rows]
            return _json_safe(validate_overnight_alpha_summary(
                rows,
                period=payload.period,
                min_edge=payload.min_edge,
                limit=payload.limit,
            ))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"overnight alpha validation failed: {str(exc)[-500:]}") from exc


_SINGLE_STOCK_METRIC_DEFINITIONS: Dict[str, Dict[str, str]] = {
    "prev_day_return": {
        "label": "当日涨跌幅",
        "plain": "当日收盘价相对前一交易日收盘价的涨跌，用来观察追涨或回落对隔夜 Alpha 的影响。",
    },
    "intraday_return": {
        "label": "日内涨跌幅",
        "plain": "当日收盘价相对开盘价的涨跌，更接近尾盘买入前的盘中多空结果。",
    },
    "ret_5d": {
        "label": "5日趋势",
        "plain": "最近5个交易日累计涨跌幅，用来衡量短线动量是否仍在延续。",
    },
    "ret_20d": {
        "label": "20日趋势",
        "plain": "最近20个交易日累计涨跌幅，用来观察中短期趋势背景。",
    },
    "ma5_distance": {
        "label": "距MA5",
        "plain": "收盘价相对5日均线的位置，正值表示站在短线均线上方。",
    },
    "ma20_distance": {
        "label": "距MA20",
        "plain": "收盘价相对20日均线的位置，用来判断是否仍在可承接的中短期结构内。",
    },
    "range_pct": {
        "label": "日内振幅",
        "plain": "最高价与最低价相对收盘价的比例，代表当天波动强度。",
    },
    "close_location": {
        "label": "收盘位置",
        "plain": "收盘价在当日高低区间中的位置，越接近1代表越靠近日高收盘。",
    },
    "lower_shadow_pct": {
        "label": "下影线占比",
        "plain": "下影线占全天振幅的比例，用来衡量低位承接是否明显。",
    },
    "volume_ratio": {
        "label": "量能比",
        "plain": "当日成交量相对20日均量的倍数，用来判断资金参与是否放大。",
    },
    "hv20": {
        "label": "20日历史波动率",
        "plain": "过去20个交易日日收益波动年化后的结果，反映真实走出来的波动。",
    },
    "pullback_from_20d_high": {
        "label": "距20日高点回撤",
        "plain": "收盘价相对20日高点的回撤幅度，用来定位是否处在回调后承接区域。",
    },
}


def _single_stock_row_from_snapshot(symbol: str) -> Optional[Dict[str, Any]]:
    snapshot = latest_home_dashboard_snapshot()
    rows = (((snapshot or {}).get("payload") or {}).get("rows") or [])
    symbol = symbol.upper()
    for row in rows:
        if str(row.get("symbol") or "").upper() == symbol:
            return dict(row)
    return None


def _single_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _single_percentile(series: pd.Series, value: float) -> Optional[float]:
    sample = pd.to_numeric(series, errors="coerce").dropna()
    if sample.empty or not math.isfinite(value):
        return None
    return round(float((sample <= value).sum()) / float(len(sample)), 4)


def _single_corr(feature: pd.Series, target: pd.Series) -> tuple[Optional[float], int]:
    frame = pd.concat([feature, target], axis=1).replace([math.inf, -math.inf], pd.NA).dropna()
    if len(frame) < 20:
        return None, int(len(frame))
    corr = float(frame.iloc[:, 0].corr(frame.iloc[:, 1]))
    return (round(corr, 4), int(len(frame))) if math.isfinite(corr) else (None, int(len(frame)))


def _single_iv_state(iv_hv: Optional[float]) -> str:
    if iv_hv is None:
        return "不可判断"
    if iv_hv < 0.8:
        return "IV偏低"
    if iv_hv <= 1.2:
        return "IV正常"
    if iv_hv <= 1.8:
        return "IV偏高"
    return "IV极端偏高"


def _single_exchange_display(exchange: str) -> str:
    mapping = {
        "NMS": "NASDAQ Global Select",
        "NGM": "NASDAQ Global Market",
        "NCM": "NASDAQ Capital Market",
        "NYQ": "NYSE",
        "NYS": "NYSE",
        "ASE": "NYSE American",
        "PCX": "NYSE Arca",
        "BTS": "Cboe BZX",
        "PNK": "OTC Pink",
    }
    return mapping.get(str(exchange or "").upper(), str(exchange or "") or "未知板块")


def _single_search_cache_key(query: str) -> str:
    return f"single_stock_search:v1:{query.strip().lower()}"


def _upsert_dynamic_symbols() -> None:
    """Persist symbols discovered from local snapshots / reports / daily-stock into the directory."""
    rows: Dict[str, Dict[str, Any]] = {}

    def add(symbol: Any, name: Any = "", exchange: Any = "", source: str = "local") -> None:
        ticker = str(symbol or "").strip().upper()
        if not ticker or not re.fullmatch(r"[A-Z0-9.-]{1,16}", ticker):
            return
        existing = rows.get(ticker, {})
        resolved_exchange = str(exchange or existing.get("exchange") or "")
        rows[ticker] = {
            "symbol": ticker,
            "name": str(name or existing.get("name") or ticker),
            "exchange": resolved_exchange,
            # Only set a display when we actually know the exchange; an empty
            # value must stay empty so it does not clobber the seed's real
            # exchange_display (NASDAQ/NYSE) on upsert.
            "exchange_display": _single_exchange_display(resolved_exchange) if resolved_exchange else "",
            "quote_type": existing.get("quote_type") or "EQUITY",
            "source": existing.get("source") or source,
            "has_history": bool(existing.get("has_history") or source == "daily_stock"),
        }

    try:
        snapshot = latest_home_dashboard_snapshot()
        for row in (((snapshot or {}).get("payload") or {}).get("rows") or []):
            symbol = row.get("symbol")
            peer = row.get("peer_mapping") or {}
            # Do NOT write universe/pool labels into the exchange field; the
            # search dropdown should show the real listing exchange (NASDAQ/NYSE),
            # not a research-pool name. Leave exchange empty so the seed's real
            # exchange is preserved.
            add(symbol, peer.get("company_name") or symbol, "", "home_snapshot")
    except Exception:
        pass

    try:
        for path in _stock_signal_report_candidates():
            payload = _load_json_file(path) or {}
            memberships = (payload.get("universe") or {}).get("memberships") or {}
            for symbol in memberships.keys():
                add(symbol, symbol, "", "report_membership")
            for result in payload.get("results") or []:
                add(result.get("ticker"), result.get("company_name") or result.get("name") or result.get("ticker"), "", "report")
    except Exception:
        pass

    try:
        for item in get_daily_stock_recent_symbols(300):
            add(item.get("symbol"), item.get("name"), item.get("exchange"), "daily_stock")
    except Exception:
        pass

    if rows:
        try:
            symbol_directory_upsert_many(list(rows.values()))
        except Exception:
            pass


def _ensure_symbol_directory() -> None:
    """Seed the persistent symbol directory once, then refresh dynamic sources on a TTL.

    The autocomplete query path reads only from the SQLite ``symbol_directory``
    table via SQL, so it never issues a live quote/search API call.
    """
    try:
        has_rows = symbol_directory_count() > 0
    except Exception:
        has_rows = False

    if not has_rows:
        try:
            from symbol_seed import load_symbol_seed

            seed_rows = load_symbol_seed()
            for item in seed_rows:
                item["exchange_display"] = _single_exchange_display(item.get("exchange") or "")
            symbol_directory_upsert_many(seed_rows)
        except Exception:
            pass
        _upsert_dynamic_symbols()
        try:
            cache_set("symbol_directory_dynamic_refresh:v1", True, ttl_seconds=30 * 60)
        except Exception:
            pass
        return

    # One-time self-heal: clear pool labels wrongly stored as exchange, then
    # re-apply the seed's real exchanges over the corrected rows.
    _fix_directory_exchanges_once()

    # Directory already seeded: only refresh dynamic sources occasionally.
    if not cache_get("symbol_directory_dynamic_refresh:v1"):
        _upsert_dynamic_symbols()
        try:
            cache_set("symbol_directory_dynamic_refresh:v1", True, ttl_seconds=30 * 60)
        except Exception:
            pass


def _fix_directory_exchanges_once() -> None:
    """Self-heal historic exchange pollution (run once, guarded by a kv flag)."""
    try:
        if cache_get("symbol_directory_exchange_fix:v3"):
            return
        from app_database import symbol_directory_blank_pool_exchanges
        from symbol_seed import load_symbol_seed

        symbol_directory_blank_pool_exchanges()
        seed_rows = load_symbol_seed()
        for item in seed_rows:
            item["exchange_display"] = _single_exchange_display(item.get("exchange") or "")
        symbol_directory_upsert_many(seed_rows)
        cache_set("symbol_directory_exchange_fix:v3", True)
    except Exception:
        pass


@app.get("/single-stock-overnight/search", dependencies=[Depends(require_auth)])
async def single_stock_overnight_search(q: str = Query("", max_length=32), limit: int = Query(8, ge=1, le=12)):
    """Search US tickers for the single-stock overnight cockpit."""
    query = str(q or "").strip()
    if not query:
        return {"items": []}
    _ensure_symbol_directory()
    try:
        items = symbol_directory_search(query, limit)
    except Exception:
        items = []
    return {"items": items, "cache_hit": True}


def _single_hv(frame: pd.DataFrame, window: int) -> Optional[float]:
    close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna()
    returns = close.pct_change().dropna().tail(window)
    if len(returns) < max(8, window // 2):
        return None
    value = float(returns.std() * math.sqrt(252))
    return round(value, 4) if math.isfinite(value) else None


def _single_bs_greeks(
    *,
    spot: Optional[float],
    strike: Optional[float],
    dte: Optional[int],
    sigma: Optional[float],
    option_type: str,
    risk_free_rate: float = 0.045,
) -> Dict[str, Optional[float]]:
    """Black-Scholes greeks fallback for delayed chains that only expose IV.

    This is an approximation for research display only. It lets the UI avoid a
    misleading blank when Massive greeks are unavailable, while the source field
    still makes clear these are model-derived, not live exchange greeks.
    """
    try:
        s = float(spot or 0)
        k = float(strike or 0)
        t = max(float(dte or 0) / 365.0, 1.0 / 365.0)
        vol = float(sigma or 0)
    except (TypeError, ValueError):
        return {"delta": None, "gamma": None, "theta": None, "vega": None}
    if s <= 0 or k <= 0 or vol <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None}
    nd = NormalDist()
    d1 = (math.log(s / k) + (risk_free_rate + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    if option_type.lower().startswith("p"):
        delta = nd.cdf(d1) - 1.0
        theta = (-(s * pdf * vol) / (2 * math.sqrt(t)) + risk_free_rate * k * math.exp(-risk_free_rate * t) * nd.cdf(-d2)) / 365.0
    else:
        delta = nd.cdf(d1)
        theta = (-(s * pdf * vol) / (2 * math.sqrt(t)) - risk_free_rate * k * math.exp(-risk_free_rate * t) * nd.cdf(d2)) / 365.0
    gamma = pdf / (s * vol * math.sqrt(t))
    vega = s * pdf * math.sqrt(t) / 100.0
    return {
        "delta": round(delta, 6),
        "gamma": round(gamma, 6),
        "theta": round(theta, 6),
        "vega": round(vega, 6),
    }


def _average_greeks(call: Dict[str, Optional[float]], put: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for key in ("delta", "gamma", "theta", "vega"):
        vals = [float(v) for v in (call.get(key), put.get(key)) if v is not None and math.isfinite(float(v))]
        out[key] = round(sum(vals) / len(vals), 6) if vals else None
    return out


def _single_yfinance_option_activity(symbol: str, expiry: str, current_price: float) -> Dict[str, Any]:
    """Best-effort activity fallback: volume/OI/bid/ask from yfinance.

    CBOE gives a robust delayed IV but not the activity fields this page needs.
    yfinance is used only to enrich those fields and failures remain non-fatal.
    """
    try:
        ticker = yf.Ticker(symbol)
        chain = ticker.option_chain(expiry)
    except Exception as exc:
        return {"available": False, "source": "yfinance:activity", "reason": str(exc)[-160:]}

    def row_for(frame: pd.DataFrame) -> Dict[str, Any]:
        if frame is None or frame.empty or "strike" not in frame:
            return {}
        local = frame.copy()
        local["distance"] = (pd.to_numeric(local["strike"], errors="coerce") - float(current_price)).abs()
        local = local.sort_values("distance")
        raw = local.iloc[0].to_dict()

        def f(value: Any) -> Optional[float]:
            try:
                number = float(value)
                return number if math.isfinite(number) else None
            except (TypeError, ValueError):
                return None

        return {
            "strike": f(raw.get("strike")),
            "bid": f(raw.get("bid")),
            "ask": f(raw.get("ask")),
            "last": f(raw.get("lastPrice")),
            "volume": f(raw.get("volume")),
            "open_interest": f(raw.get("openInterest")),
        }

    call = row_for(chain.calls)
    put = row_for(chain.puts)
    call_volume = call.get("volume")
    put_volume = put.get("volume")
    call_oi = call.get("open_interest")
    put_oi = put.get("open_interest")

    def ratio(num: Optional[float], den: Optional[float]) -> Optional[float]:
        return round(num / den, 4) if num is not None and den is not None and den > 0 else None

    return {
        "available": bool(call or put),
        "source": "yfinance:activity",
        "call_quote": call or None,
        "put_quote": put or None,
        "call_volume": int(call_volume) if call_volume is not None else None,
        "put_volume": int(put_volume) if put_volume is not None else None,
        "call_open_interest": int(call_oi) if call_oi is not None else None,
        "put_open_interest": int(put_oi) if put_oi is not None else None,
        "call_put_ratio": ratio(call_volume, put_volume),
        "put_call_ratio": ratio(put_volume, call_volume),
        "call_put_oi_ratio": ratio(call_oi, put_oi),
    }


def _cboe_atm_iv(symbol: str, current_price: float, hv20: Optional[float]) -> Optional[Dict[str, Any]]:
    """Near-month ATM IV from CBOE's free delayed option chain (carries its own
    IV). One HTTP call WITH a timeout -> far more robust than yfinance's
    no-timeout option_chain. Returns None on any failure so the caller can fall
    back to yfinance. Honors a proxy (CBOE is geo/GFW-variable): CBOE_PROXY ->
    FMP_PROXY -> direct."""
    if not current_price or current_price <= 0:
        return None
    sym = str(symbol).upper().strip()
    proxy = (os.getenv("CBOE_PROXY") or os.getenv("FMP_PROXY") or "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        import requests

        resp = requests.get(
            f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://www.cboe.com/"},
            proxies=proxies, timeout=20,
        )
        if resp.status_code != 200:
            return None
        options = ((resp.json() or {}).get("data") or {}).get("options") or []
    except Exception:
        return None
    if not options:
        return None
    today = datetime.utcnow().date()
    parsed = []  # (dte, exp_iso, cp, strike, iv)
    for opt in options:
        name = str(opt.get("option") or "")
        iv = opt.get("iv")
        if len(name) < 15 or iv is None:
            continue
        try:
            strike = int(name[-8:]) / 1000.0
            cp = name[-9]
            exp = datetime(2000 + int(name[-15:-13]), int(name[-13:-11]), int(name[-11:-9])).date()
        except (ValueError, IndexError):
            continue
        dte = (exp - today).days
        if dte < 1 or cp not in ("C", "P"):
            continue
        parsed.append((dte, exp.isoformat(), cp, strike, float(iv)))
    if not parsed:
        return None
    # Pick near month (7-45 DTE preferred, closest to 21).
    expiries = sorted({(p[0], p[1]) for p in parsed}, key=lambda x: (0 if 7 <= x[0] <= 45 else 100) + abs(x[0] - 21))
    target_dte, target_exp = expiries[0]
    same = [p for p in parsed if p[1] == target_exp]

    def closest(cp: str):
        rows = [p for p in same if p[2] == cp and p[4] >= 0.01]
        if not rows:
            return None, None
        best = min(rows, key=lambda p: abs(p[3] - current_price))
        return round(best[4], 4), best[3]

    call_iv, call_strike = closest("C")
    put_iv, put_strike = closest("P")
    values = [v for v in (call_iv, put_iv) if v is not None and v >= 0.01]
    if not values:
        return None
    atm_iv = round(sum(values) / len(values), 4)
    iv_hv20 = round(atm_iv / hv20, 4) if hv20 and hv20 > 0 else None
    call_greeks = _single_bs_greeks(spot=current_price, strike=call_strike, dte=target_dte, sigma=call_iv or atm_iv, option_type="call")
    put_greeks = _single_bs_greeks(spot=current_price, strike=put_strike, dte=target_dte, sigma=put_iv or atm_iv, option_type="put")
    activity = _single_yfinance_option_activity(sym, target_exp, current_price)
    return {
        "available": True, "status": "ok", "status_cn": "已获取近月平值 IV（CBOE 延迟链）",
        "source": "cboe:delayed", "expiry": target_exp, "dte": target_dte,
        "atm_iv": atm_iv, "hv20": hv20, "iv_hv20": iv_hv20, "state_cn": _single_iv_state(iv_hv20),
        "call_strike": call_strike, "put_strike": put_strike, "call_iv": call_iv, "put_iv": put_iv,
        "greeks": _average_greeks(call_greeks, put_greeks),
        "call_greeks": call_greeks,
        "put_greeks": put_greeks,
        "greeks_source": "black_scholes_from_cboe_iv",
        "activity_source": activity.get("source") if activity.get("available") else None,
        "call_quote": activity.get("call_quote"),
        "put_quote": activity.get("put_quote"),
        "call_volume": activity.get("call_volume"),
        "put_volume": activity.get("put_volume"),
        "call_open_interest": activity.get("call_open_interest"),
        "put_open_interest": activity.get("put_open_interest"),
        "call_put_ratio": activity.get("call_put_ratio"),
        "put_call_ratio": activity.get("put_call_ratio"),
        "call_put_oi_ratio": activity.get("call_put_oi_ratio"),
        "missing_activity_reason": activity.get("reason") if not activity.get("available") else None,
        "cache_hit": False,
    }


def _single_option_iv(symbol: str, current_price: Optional[float], hv20: Optional[float]) -> Dict[str, Any]:
    cache_key = f"single_stock_iv:v8:{symbol.upper()}"
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        return {**cached, "cache_hit": True}
    if not external_data_allowed():
        return {
            "available": False,
            "status": "page_read_cache_only",
            "status_cn": "页面只读模式：未命中本地 IV 缓存，不现场请求期权链",
            "cache_hit": False,
        }
    if not current_price or current_price <= 0:
        return {"available": False, "status": "missing_price", "status_cn": "缺少现价，无法定位平值期权"}
    # Primary: Massive/Polygon current option snapshot (IV + Greeks + OI +
    # call/put activity). CBOE/yfinance below are fallbacks for IV only.
    massive = get_massive_options_snapshot(symbol, current_price)
    if massive.get("available"):
        atm_iv = _single_float(massive.get("atm_iv"))
        iv_hv20 = round(atm_iv / hv20, 4) if atm_iv and hv20 and hv20 > 0 else None
        payload = {
            **massive,
            "hv20": hv20,
            "iv_hv20": iv_hv20,
            "state_cn": _single_iv_state(iv_hv20),
            "status_cn": "已获取 Massive 当前期权快照（IV/Greeks/OI/Call-Put）",
            "cache_hit": False,
        }
        cache_set(cache_key, payload, ttl_seconds=900)
        return payload
    massive_attempt = {
        "source": "massive",
        "available": False,
        "reason": massive.get("reason"),
        "status_code": massive.get("status_code"),
    }

    databento = get_databento_options_snapshot(symbol, current_price)
    if databento.get("available"):
        atm_iv = _single_float(databento.get("atm_iv"))
        iv_hv20 = round(atm_iv / hv20, 4) if atm_iv and hv20 and hv20 > 0 else None
        call_greeks = _single_bs_greeks(
            spot=current_price,
            strike=_single_float(databento.get("call_strike")),
            dte=int(databento.get("dte") or 0),
            sigma=_single_float(databento.get("call_iv")) or atm_iv,
            option_type="call",
        )
        put_greeks = _single_bs_greeks(
            spot=current_price,
            strike=_single_float(databento.get("put_strike")),
            dte=int(databento.get("dte") or 0),
            sigma=_single_float(databento.get("put_iv")) or atm_iv,
            option_type="put",
        )
        payload = {
            **databento,
            "hv20": hv20,
            "iv_hv20": iv_hv20,
            "state_cn": _single_iv_state(iv_hv20),
            "greeks": _average_greeks(call_greeks, put_greeks),
            "call_greeks": call_greeks,
            "put_greeks": put_greeks,
            "greeks_source": "black_scholes_from_databento_cbbo_iv",
            "primary_source_attempt": massive_attempt,
            "status_cn": "已获取 Databento OPRA 延迟报价并反推 ATM IV",
            "cache_hit": False,
        }
        cache_set(cache_key, payload, ttl_seconds=1800)
        return payload
    databento_attempt = {
        "source": "databento:opra",
        "available": False,
        "reason": databento.get("reason"),
        "detail": databento.get("detail"),
    }

    # Fallback: CBOE delayed chain (one timed HTTP, official IV). yfinance below = fallback.
    cboe = _cboe_atm_iv(symbol, current_price, hv20)
    if cboe is not None:
        cboe["primary_source_attempt"] = massive_attempt
        cboe["secondary_source_attempt"] = databento_attempt
        cache_set(cache_key, cboe, ttl_seconds=1800)
        return cboe
    try:
        ticker = yf.Ticker(symbol)
        expiries = list(ticker.options or [])
    except Exception as exc:
        payload = {"available": False, "status": "source_unavailable", "status_cn": "期权数据源暂不可用", "detail": str(exc)[-180:]}
        cache_set(cache_key, payload, ttl_seconds=900)
        return payload
    if not expiries:
        payload = {"available": False, "status": "no_listed_options", "status_cn": "无上市期权，不能计算 IV"}
        cache_set(cache_key, payload, ttl_seconds=6 * 3600)
        return payload
    today = datetime.utcnow().date()
    ranked_expiries: List[tuple[int, str]] = []
    for expiry in expiries:
        try:
            dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        if dte >= 1:
            penalty = 0 if 7 <= dte <= 45 else 100
            ranked_expiries.append((penalty + abs(dte - 21), expiry))
    if not ranked_expiries:
        payload = {"available": False, "status": "no_valid_expiry", "status_cn": "没有可用到期日"}
        cache_set(cache_key, payload, ttl_seconds=1800)
        return payload
    expiry = sorted(ranked_expiries)[0][1]
    try:
        chain = ticker.option_chain(expiry)
        calls = chain.calls.copy()
        puts = chain.puts.copy()
    except Exception as exc:
        payload = {"available": False, "status": "chain_unavailable", "status_cn": "期权链暂不可用", "expiry": expiry, "detail": str(exc)[-180:]}
        cache_set(cache_key, payload, ttl_seconds=900)
        return payload
    if calls.empty and puts.empty:
        payload = {"available": False, "status": "empty_chain", "status_cn": "期权链为空", "expiry": expiry}
        cache_set(cache_key, payload, ttl_seconds=1800)
        return payload

    def closest_iv(frame: pd.DataFrame) -> tuple[Optional[float], Optional[float], Dict[str, Any]]:
        if frame.empty or "strike" not in frame:
            return None, None, {}
        local = frame.copy()
        local["distance"] = (pd.to_numeric(local["strike"], errors="coerce") - float(current_price)).abs()
        local = local.sort_values("distance")
        row = local.iloc[0].to_dict()
        iv = _single_float(row.get("impliedVolatility"))
        strike = _single_float(row.get("strike"))
        return iv, strike, {
            "bid": _single_float(row.get("bid")),
            "ask": _single_float(row.get("ask")),
            "volume": _single_float(row.get("volume")),
            "open_interest": _single_float(row.get("openInterest")),
        }

    call_iv, call_strike, call_quote = closest_iv(calls)
    put_iv, put_strike, put_quote = closest_iv(puts)
    values = [value for value in (call_iv, put_iv) if value is not None and value >= 0.01]
    if not values:
        payload = {
            "available": False,
            "status": "missing_or_invalid_implied_volatility",
            "status_cn": "期权链存在，但 IV 字段缺失或接近 0，暂不采用",
            "expiry": expiry,
            "call_strike": call_strike,
            "put_strike": put_strike,
            "call_iv": call_iv,
            "put_iv": put_iv,
        }
        cache_set(cache_key, payload, ttl_seconds=1800)
        return payload
    atm_iv = round(sum(values) / len(values), 4)
    iv_hv20 = round(atm_iv / hv20, 4) if hv20 and hv20 > 0 else None
    payload = {
        "available": True,
        "status": "ok",
        "status_cn": "已获取近月平值 IV",
        "source": "yfinance",
        "expiry": expiry,
        "dte": (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days,
        "atm_iv": atm_iv,
        "hv20": hv20,
        "iv_hv20": iv_hv20,
        "state_cn": _single_iv_state(iv_hv20),
        "call_strike": call_strike,
        "put_strike": put_strike,
        "call_iv": call_iv,
        "put_iv": put_iv,
        "call_quote": call_quote,
        "put_quote": put_quote,
        "primary_source_attempt": massive_attempt,
        "cache_hit": False,
    }
    cache_set(cache_key, payload, ttl_seconds=1800)
    return payload


def _single_stock_correlation_pack(row: Dict[str, Any], period: str) -> Dict[str, Any]:
    symbol = str(row.get("symbol") or row.get("ticker") or "").upper()
    source_universes = row.get("source_universe_ids") or row.get("source_pools") or []
    benchmark, benchmark_source = benchmark_for_universes(source_universes)
    stock, stock_source = get_daily_history(symbol, period=period)
    bench, bench_source = get_daily_history(benchmark, period=period)
    if stock.empty or bench.empty:
        return {
            "available": False,
            "reason": "insufficient_price_history",
            "benchmark": benchmark,
            "benchmark_source_universe": benchmark_source,
            "data_sources": {"stock": stock_source, "benchmark": bench_source},
            "metrics": [],
            "price_line": [],
        }
    common = stock.index.intersection(bench.index).sort_values()
    stock = stock.loc[common].copy()
    bench = bench.loc[common].copy()
    if len(stock) < 60:
        return {
            "available": False,
            "reason": "insufficient_price_history",
            "benchmark": benchmark,
            "benchmark_source_universe": benchmark_source,
            "data_sources": {"stock": stock_source, "benchmark": bench_source},
            "metrics": [],
            "price_line": [],
        }
    stock_ret = stock["Close"].astype(float).pct_change().dropna()
    bench_ret = bench["Close"].astype(float).pct_change().dropna()
    beta_common = stock_ret.index.intersection(bench_ret.index)
    beta = 1.0
    if len(beta_common) >= 30:
        variance = float(bench_ret.loc[beta_common].tail(120).var())
        if variance > 1e-12:
            covariance = float(stock_ret.loc[beta_common].tail(120).cov(bench_ret.loc[beta_common].tail(120)))
            beta = max(-1.0, min(3.0, covariance / variance))

    open_ = stock["Open"].astype(float)
    high = stock["High"].astype(float)
    low = stock["Low"].astype(float)
    close = stock["Close"].astype(float)
    volume = stock["Volume"].astype(float)
    bench_open = bench["Open"].astype(float)
    bench_close = bench["Close"].astype(float)
    stock_overnight = open_.shift(-1) / close - 1.0
    bench_overnight = bench_open.shift(-1) / bench_close - 1.0
    alpha_target = stock_overnight - beta * bench_overnight
    absolute_target = stock_overnight
    candle_range = (high - low).replace(0, pd.NA)
    body_floor = (close - open_).abs().replace(0, pd.NA)
    lower_shadow = (pd.concat([open_, close], axis=1).min(axis=1) - low).clip(lower=0.0)
    features = {
        "prev_day_return": close.pct_change(),
        "intraday_return": close / open_ - 1.0,
        "ret_5d": close.pct_change(5),
        "ret_20d": close.pct_change(20),
        "ma5_distance": close / close.rolling(5).mean() - 1.0,
        "ma20_distance": close / close.rolling(20).mean() - 1.0,
        "range_pct": (high - low) / close,
        "close_location": (close - low) / candle_range,
        "lower_shadow_pct": lower_shadow / candle_range,
        "volume_ratio": volume / volume.rolling(20).mean(),
        "hv20": close.pct_change().rolling(20).std() * math.sqrt(252),
        "pullback_from_20d_high": close / close.rolling(20).max() - 1.0,
    }
    metrics: List[Dict[str, Any]] = []
    for key, series in features.items():
        corr_alpha, sample_count = _single_corr(series, alpha_target)
        corr_abs, _ = _single_corr(series, absolute_target)
        current_value = _single_float(series.dropna().iloc[-1] if not series.dropna().empty else None)
        definition = _SINGLE_STOCK_METRIC_DEFINITIONS.get(key, {})
        metrics.append({
            "key": key,
            "label": definition.get("label", key),
            "plain_meaning": definition.get("plain", ""),
            "current_value": round(current_value, 6) if current_value is not None else None,
            "percentile": _single_percentile(series, current_value) if current_value is not None else None,
            "alpha_correlation": corr_alpha,
            "next_open_correlation": corr_abs,
            "sample_count": sample_count,
            "direction_cn": "正相关" if (corr_alpha or 0) > 0 else ("负相关" if (corr_alpha or 0) < 0 else "不明显"),
        })
    metrics.sort(key=lambda item: abs(float(item.get("alpha_correlation") or 0)), reverse=True)
    latest_rows = stock.tail(80)
    price_line = [
        {
            "date": index.strftime("%Y-%m-%d"),
            "close": round(float(item["Close"]), 4),
        }
        for index, item in latest_rows.iterrows()
        if math.isfinite(float(item["Close"]))
    ]
    return {
        "available": True,
        "benchmark": benchmark,
        "benchmark_source_universe": benchmark_source,
        "beta": round(beta, 4),
        "data_sources": {"stock": stock_source, "benchmark": bench_source},
        "sample_count": int(len(stock)),
        "metrics": metrics,
        "price_line": price_line,
    }


def _single_atr(frame: pd.DataFrame, window: int = 14) -> Optional[float]:
    if frame.empty or len(frame) < 5:
        return None
    high = pd.to_numeric(frame.get("High"), errors="coerce")
    low = pd.to_numeric(frame.get("Low"), errors="coerce")
    close = pd.to_numeric(frame.get("Close"), errors="coerce")
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1).dropna()
    # Guard against corrupt bars: a true range exceeding ~half the price is a bad
    # print, not volatility -- drop it so one fat-finger bar can't blow up ATR
    # (and every level derived from it).
    sane_close = close.reindex(true_range.index)
    true_range = true_range[true_range <= (sane_close.abs() * 0.5).fillna(true_range)]
    if true_range.empty:
        return None
    value = float(true_range.tail(window).mean())
    return round(value, 4) if math.isfinite(value) and value > 0 else None


def detect_distribution_risk(frame: pd.DataFrame, *, lookback: int = 60) -> Dict[str, Any]:
    """Detect high-volume stalling / upper-wick distribution risk.

    The warning is deliberately conjunctive: it requires a high-price context and
    abnormal volume, then looks for weak close / failed breakout / long upper
    wick / multi-day churn. This avoids flagging healthy high-volume breakouts.
    """
    if frame is None or frame.empty or len(frame) < 25:
        return {"available": False, "triggered": False, "level": "none", "reasons": ["insufficient_history"]}
    try:
        recent = frame.tail(max(lookback, 25)).copy()
        open_ = pd.to_numeric(recent.get("Open"), errors="coerce")
        high = pd.to_numeric(recent.get("High"), errors="coerce")
        low = pd.to_numeric(recent.get("Low"), errors="coerce")
        close = pd.to_numeric(recent.get("Close"), errors="coerce")
        volume = pd.to_numeric(recent.get("Volume"), errors="coerce")
        data = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}).dropna()
        data = data[(data["Open"] > 0) & (data["High"] > 0) & (data["Low"] > 0) & (data["Close"] > 0) & (data["Volume"] > 0)]
    except Exception:
        return {"available": False, "triggered": False, "level": "none", "reasons": ["bad_ohlcv"]}
    if len(data) < 25:
        return {"available": False, "triggered": False, "level": "none", "reasons": ["insufficient_clean_history"]}

    latest = data.iloc[-1]
    prev_close = float(data["Close"].iloc[-2]) if len(data) >= 2 else float(latest["Close"])
    close_v = float(latest["Close"])
    high_v = float(latest["High"])
    low_v = float(latest["Low"])
    open_v = float(latest["Open"])
    vol_v = float(latest["Volume"])
    vol20 = float(data["Volume"].tail(21).iloc[:-1].median()) if len(data) >= 21 else float(data["Volume"].iloc[:-1].median())
    if not math.isfinite(vol20) or vol20 <= 0:
        vol20 = float(data["Volume"].iloc[:-1].mean())
    volume_ratio = vol_v / vol20 if vol20 > 0 else None
    rolling_high_20_prev = float(data["High"].iloc[:-1].tail(20).max())
    rolling_high_60 = float(data["High"].tail(60).max())
    range_v = max(high_v - low_v, 1e-9)
    upper_shadow = max(0.0, high_v - max(open_v, close_v))
    lower_shadow = max(0.0, min(open_v, close_v) - low_v)
    close_location = (close_v - low_v) / range_v
    upper_shadow_pct = upper_shadow / range_v
    day_return = close_v / prev_close - 1.0 if prev_close > 0 else 0.0
    intraday_return = close_v / open_v - 1.0 if open_v > 0 else 0.0
    near_high = close_v >= rolling_high_60 * 0.92 or high_v >= rolling_high_60 * 0.98
    failed_breakout = high_v > rolling_high_20_prev * 1.003 and close_v < rolling_high_20_prev
    weak_close = close_location <= 0.45 or intraday_return <= -0.003
    long_upper_wick = upper_shadow_pct >= 0.35 and upper_shadow > lower_shadow
    volume_spike = bool(volume_ratio is not None and volume_ratio >= 1.8)
    stalling = day_return <= 0.012

    churn = False
    churn_count = 0
    if len(data) >= 23:
        tail = data.tail(5).copy()
        tail_range = max(float(tail["High"].max() - tail["Low"].min()), 1e-9)
        tail_close_range = abs(float(tail["Close"].iloc[-1] / tail["Close"].iloc[0] - 1.0)) if float(tail["Close"].iloc[0]) > 0 else 1.0
        median20 = data["Volume"].tail(25).iloc[:-5].median()
        if median20 and math.isfinite(float(median20)) and float(median20) > 0:
            churn_count = int((tail["Volume"] / float(median20) >= 1.5).sum())
            churn = bool(churn_count >= 3 and tail_close_range <= 0.035 and tail_range / close_v <= 0.12 and close_v >= rolling_high_60 * 0.9)

    signals = {
        "near_high": near_high,
        "volume_spike": volume_spike,
        "stalling": stalling,
        "weak_close": weak_close,
        "failed_breakout": failed_breakout,
        "long_upper_wick": long_upper_wick,
        "high_volume_churn": churn,
    }
    score = 0.0
    weights = {
        "near_high": 1.0,
        "volume_spike": 1.2,
        "stalling": 0.7,
        "weak_close": 0.9,
        "failed_breakout": 1.0,
        "long_upper_wick": 1.0,
        "high_volume_churn": 0.8,
    }
    for key, active in signals.items():
        if active:
            score += weights[key]
    triggered = bool(near_high and volume_spike and (weak_close or failed_breakout or long_upper_wick or churn) and stalling)
    level = "high" if triggered and score >= 4.4 else ("medium" if triggered else "none")
    reasons = []
    if volume_spike:
        reasons.append(f"成交量约为20日中位量 {volume_ratio:.1f}x")
    if near_high:
        reasons.append("处于阶段高位附近")
    if failed_breakout:
        reasons.append("盘中突破近期高点但收盘未站稳")
    if long_upper_wick:
        reasons.append(f"长上影占日内振幅 {upper_shadow_pct:.0%}")
    if weak_close:
        reasons.append(f"收盘位于日内区间 {close_location:.0%}，收盘偏弱")
    if churn:
        reasons.append(f"近5日有 {churn_count} 日放量但价格横盘")
    label = "高位放量滞涨" if level == "medium" else ("高位派发风险" if level == "high" else "无派发警示")
    return {
        "available": True,
        "triggered": triggered,
        "level": level,
        "label": label,
        "score": round(score, 2),
        "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None and math.isfinite(volume_ratio) else None,
        "day_return": round(day_return, 4),
        "intraday_return": round(intraday_return, 4),
        "close_location": round(close_location, 4),
        "upper_shadow_pct": round(upper_shadow_pct, 4),
        "failed_breakout": failed_breakout,
        "high_volume_churn": churn,
        "reasons": reasons,
        "method": "near-stage-high + abnormal volume + weak close/failed breakout/upper wick/churn",
    }


from distribution_risk_service import detect_distribution_risk


def _sanitize_candle_rows(candles: list) -> list:
    """Defend the chart + frontend volume profile against corrupt feed prints:
    drop isolated whole-bar spikes (a 10x fat-finger that reverts next bar) and
    clamp corrupt wicks (High/Low absurdly far from the bar body)."""
    if not candles:
        return candles
    n = len(candles)
    out: list = []
    for i, k in enumerate(candles):
        c = k.get("c")
        if c is None:
            continue
        prev_c = candles[i - 1].get("c") if i > 0 else (candles[i + 1].get("c") if i + 1 < n else c)
        next_c = candles[i + 1].get("c") if i + 1 < n else prev_c
        try:
            cf, pc, nc = float(c), float(prev_c), float(next_c)
        except (TypeError, ValueError):
            out.append(k)
            continue
        if pc > 0 and nc > 0 and ((cf > 2.5 * pc and cf > 2.5 * nc) or (cf * 2.5 < pc and cf * 2.5 < nc)):
            continue  # isolated bad-print bar -> drop
        try:
            of, hf, lf = float(k.get("o")), float(k.get("h")), float(k.get("l"))
            body_hi, body_lo = max(of, cf), min(of, cf)
            if hf > body_hi * 1.5:
                k = {**k, "h": round(body_hi, 4)}
            if 0 < lf < body_lo / 1.5:
                k = {**k, "l": round(body_lo, 4)}
        except (TypeError, ValueError):
            pass
        out.append(k)
    return out


def _single_open_action_plan(symbol: str, row: Dict[str, Any], frame: pd.DataFrame) -> Dict[str, Any]:
    """Build next-session research levels for close-to-open style review."""
    close = _single_float(row.get("current_price"))
    if close is None and not frame.empty:
        close = _single_float(frame["Close"].iloc[-1])
    if close is None or close <= 0:
        return {"available": False, "reason": "缺少有效现价，无法生成盘后行动区间。"}
    atr = _single_atr(frame) or max(close * 0.025, 0.01)
    latest_high = _single_float(frame["High"].iloc[-1]) if not frame.empty else None
    latest_low = _single_float(frame["Low"].iloc[-1]) if not frame.empty else None
    waiting_breakout = _single_float(row.get("waiting_breakout_price"))
    invalidation = _single_float(row.get("invalidation_price"))
    support = _single_float(row.get("nearest_support"))
    target = _single_float(row.get("target_price"))

    strength_buy = max(
        close + 0.15 * atr,
        waiting_breakout if waiting_breakout and waiting_breakout <= close + 1.25 * atr else 0,
    )
    # A pullback (dip) buy level must sit BELOW the current price, but also be a
    # REAL pullback -- not the old base tens of percent below. For names at new
    # highs the nearest recorded support / invalidation can be far under price;
    # anchoring 回踩买 / 止损 there is meaningless (e.g. price 269, support 151 ->
    # "回踩买 152"). Only use support/invalidation when within ~3 ATR of price;
    # otherwise fall back to ATR-based levels, and floor the dip at ~2.5 ATR.
    max_dip = 3.0 * atr
    near_support = support if (support and (close - max_dip) <= support < close) else None
    near_invalidation = invalidation if (invalidation and 0 < invalidation < close and invalidation >= close - max_dip) else None
    if near_support:
        pullback_buy = near_support + 0.15 * atr
    else:
        pullback_buy = close - 0.35 * atr
    pullback_buy = min(pullback_buy, close - 0.05 * atr)
    pullback_buy = max(0.01, pullback_buy, close - 2.5 * atr)
    chase_stop = max(close + 0.85 * atr, strength_buy + 0.45 * atr)
    breakdown_stop = min(
        close - 0.75 * atr,
        near_invalidation if near_invalidation else close - 0.75 * atr,
        (near_support - 0.25 * atr) if near_support else close - 0.75 * atr,
    )
    reference_entry = strength_buy if abs(strength_buy - close) <= abs(close - pullback_buy) else pullback_buy
    stop_loss = min(
        reference_entry - 0.75 * atr,
        near_invalidation if near_invalidation else reference_entry - 0.75 * atr,
    )
    per_share_risk = max(0.01, reference_entry - stop_loss)
    take_profit = max(
        reference_entry + 1.5 * per_share_risk,
        target if target and target > reference_entry else 0,
        latest_high if latest_high and latest_high > reference_entry else 0,
    )
    aggressive_take_profit = reference_entry + 2.2 * per_share_risk
    def pct_from_ref(value: float) -> float:
        return round(value / close - 1.0, 4)

    return {
        "available": True,
        "symbol": symbol,
        "as_of_price": round(close, 2),
        "reference_label": "前一交易日收盘价",
        "atr14": round(atr, 2),
        "buy_strength_price": round(strength_buy, 2),
        "buy_strength_pct": pct_from_ref(strength_buy),
        "buy_pullback_price": round(pullback_buy, 2),
        "buy_pullback_pct": pct_from_ref(pullback_buy),
        "stop_chasing_above": round(chase_stop, 2),
        "stop_chasing_pct": pct_from_ref(chase_stop),
        "stop_buying_below": round(max(0.01, breakdown_stop), 2),
        "stop_buying_below_pct": pct_from_ref(max(0.01, breakdown_stop)),
        "reference_entry": round(reference_entry, 2),
        "reference_entry_pct": pct_from_ref(reference_entry),
        "stop_loss": round(max(0.01, stop_loss), 2),
        "stop_loss_pct": pct_from_ref(max(0.01, stop_loss)),
        "take_profit": round(take_profit, 2),
        "take_profit_pct": pct_from_ref(take_profit),
        "aggressive_take_profit": round(aggressive_take_profit, 2),
        "aggressive_take_profit_pct": pct_from_ref(aggressive_take_profit),
        "risk_per_share": round(per_share_risk, 2),
        "reward_risk": round((take_profit - reference_entry) / per_share_risk, 2) if per_share_risk > 0 else None,
        "basis": {
            "latest_high": round(latest_high, 2) if latest_high else None,
            "latest_low": round(latest_low, 2) if latest_low else None,
            "waiting_breakout_price": waiting_breakout,
            "nearest_support": support,
            "invalidation_price": invalidation,
            "target_price": target,
        },
        "plain_cn": (
            f"开盘后只看两类触发：放量站上 ${strength_buy:.2f} 可视为强势确认；"
            f"回踩 ${pullback_buy:.2f} 附近且不破支撑可视为低吸复核。"
            f"若直接冲高超过 ${chase_stop:.2f}，不追；若跌破 ${breakdown_stop:.2f}，停止买入动作。"
            f"若执行研究买入，参考止损 ${stop_loss:.2f}，第一止盈 ${take_profit:.2f}。"
        ),
        "disclaimer": "该区间基于最近收盘、ATR、支撑/突破/失效位生成，仅供盘前研究复核，不是确定性交易指令。",
    }


def _single_three_month_comparison(symbol: str, benchmark: str) -> Dict[str, Any]:
    stock, stock_source = get_daily_history(symbol, period="3mo")
    bench, bench_source = get_daily_history(benchmark, period="3mo")
    if stock.empty or bench.empty:
        return {
            "available": False,
            "reason": "缺少标的或基准的近三个月行情。",
            "benchmark": benchmark,
            "data_sources": {"stock": stock_source, "benchmark": bench_source},
        }
    common = stock.index.intersection(bench.index).sort_values()
    stock = stock.loc[common].tail(70).copy()
    bench = bench.loc[common].tail(70).copy()
    if len(stock) < 10 or len(bench) < 10:
        return {
            "available": False,
            "reason": "近三个月可对齐样本不足。",
            "benchmark": benchmark,
            "data_sources": {"stock": stock_source, "benchmark": bench_source},
        }
    stock_base = float(stock["Close"].iloc[0])
    bench_base = float(bench["Close"].iloc[0])

    def rows(frame: pd.DataFrame, base: float) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for index, item in frame.iterrows():
            if base <= 0:
                continue
            result.append({
                "date": index.strftime("%Y-%m-%d"),
                "open": round(float(item["Open"]) / base - 1.0, 4),
                "high": round(float(item["High"]) / base - 1.0, 4),
                "low": round(float(item["Low"]) / base - 1.0, 4),
                "close": round(float(item["Close"]) / base - 1.0, 4),
            })
        return result

    stock_return = float(stock["Close"].iloc[-1]) / stock_base - 1.0
    bench_return = float(bench["Close"].iloc[-1]) / bench_base - 1.0
    return {
        "available": True,
        "symbol": symbol,
        "benchmark": benchmark,
        "stock": rows(stock, stock_base),
        "benchmark_series": rows(bench, bench_base),
        "stock_return": round(stock_return, 4),
        "benchmark_return": round(bench_return, 4),
        "relative_return": round(stock_return - bench_return, 4),
        "data_sources": {"stock": stock_source, "benchmark": bench_source},
    }


def _single_stock_summary_keys(payload: Dict[str, Any]) -> tuple:
    """Return (digest_key, stable_key, digest_source). The digest key keys the
    content cache; the stable key (per-symbol, overwritten) lets a lightweight
    poll endpoint fetch the latest summary without re-deriving the digest."""
    symbol = str(payload.get("symbol") or "").upper()
    profile = payload.get("company_profile") or {}
    digest_source = {
        "symbol": symbol,
        "llm_provider": os.getenv("LANGCHAIN_PROVIDER", ""),
        "llm_model": os.getenv("LANGCHAIN_MODEL_NAME", ""),
        "company": {
            "business_cn": profile.get("business_cn"),
            "sector": (profile.get("facts") or {}).get("sector"),
            "track_position": profile.get("track_position"),
        },
        "signal_read": payload.get("signal_read") or {},
        "row": payload.get("row"),
        "overnight_alpha": payload.get("overnight_alpha"),
        "top_metrics": (payload.get("correlation") or {}).get("metrics", [])[:6],
        "volatility": payload.get("volatility"),
        "action_plan": payload.get("action_plan"),
    }
    digest = hashlib.sha256(json.dumps(_json_safe(digest_source), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return f"single_stock_deepseek_summary:v3:{symbol}:{digest}", f"single_stock_summary_latest:v1:{symbol}", digest_source


def _single_stock_summary_lazy(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Non-blocking summary: return the cached result instantly if present,
    otherwise mark it pending and compute it in the background (the ~20-45s LLM
    call is the single biggest cost on a cold page load, so we keep it off the
    critical path and let the frontend poll /summary)."""
    symbol = str(payload.get("symbol") or "").upper()
    digest_key, stable_key, _src = _single_stock_summary_keys(payload)
    cached = cache_get(digest_key)
    if isinstance(cached, dict):
        cache_set(stable_key, {**cached, "cache_hit": True}, ttl_seconds=6 * 3600)
        return {**cached, "cache_hit": True}
    with _SSO_REFRESH_LOCK:
        if digest_key in _SSO_SUMMARY_ACTIVE:
            return {"available": False, "status": "pending", "symbol": symbol}
        _SSO_SUMMARY_ACTIVE.add(digest_key)
    cache_set(stable_key, {"available": False, "status": "pending", "symbol": symbol}, ttl_seconds=600)

    def _worker():
        try:
            with external_data_scope(True):
                _single_stock_deepseek_summary(payload)
        except Exception as exc:
            console.log(f"deepseek summary worker failed for {symbol}: {str(exc)[-200:]}")
        finally:
            with _SSO_REFRESH_LOCK:
                _SSO_SUMMARY_ACTIVE.discard(digest_key)

    threading.Thread(target=_worker, daemon=True).start()
    return {"available": False, "status": "pending", "symbol": symbol,
            "summary": "AI 复核生成中…（约 20–40 秒），完成后自动刷新。"}


def _single_stock_deepseek_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    symbol = str(payload.get("symbol") or "").upper()
    cache_key, stable_key, digest_source = _single_stock_summary_keys(payload)
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        return {**cached, "cache_hit": True}
    try:
        from src.providers.chat import ChatLLM

        prompt_payload = json.dumps(_json_safe(digest_source), ensure_ascii=False)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是美股波段交易复核助手。基于给定 JSON（含该股基本面、已验证的回调买入信号 signal_read、波动率、行动价位）"
                    "给出针对【这只股票】的、有区分度的具体判断，服务回调买入、持有数日的研究方式。"
                    "禁止套话和模板（例如不要每只都写'等待回踩、风险未解除'）；必须结合本股票的 calibrated_win_rate、"
                    "pullback_state、波动率、相对强度给出不同结论。不编造数据。只输出严格 JSON。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请输出 JSON：verdict、summary、bull_points、bear_points、watch_levels、risk_flags、action_bias。"
                    "verdict 必须从这四类中选一个并说清为什么是这一类（不要含糊）："
                    "'可考虑买入'(校准胜率达标+回调/超卖形态+风险可控)、"
                    "'回踩到X可买'(方向对但当前价位不佳，给出具体回踩价)、"
                    "'观望'(信号不足或波动过大)、'回避'(形态差/胜率低/风险大)。"
                    "summary 2 句话以内：第一句给该股【独有】的关键判断（点名最能决定结论的那个指标的具体数值），第二句给最该盯的价位与方向。"
                    "bull_points/bear_points 要具体到该股的数值或基本面，不要通用废话。"
                    "watch_levels 用中文给出：买入触发价、止损价、止盈/减仓价。action_bias 给一句可执行倾向。"
                    f"\nJSON数据：{prompt_payload[:9000]}"
                ),
            },
        ]
        response = ChatLLM().chat(messages, timeout=int(os.getenv("SINGLE_STOCK_DEEPSEEK_TIMEOUT", "45")))
        text = response.content or "{}"
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1] if start >= 0 and end > start else text)
        result = {
            "available": True,
            "status": "llm_reviewed",
            "provider": os.getenv("LANGCHAIN_PROVIDER", ""),
            "model": os.getenv("LANGCHAIN_MODEL_NAME", ""),
            "verdict": str(data.get("verdict") or "人工复核"),
            "summary": str(data.get("summary") or ""),
            "bull_points": [str(x) for x in (data.get("bull_points") or [])][:4],
            "bear_points": [str(x) for x in (data.get("bear_points") or [])][:4],
            "watch_levels": [str(x) for x in (data.get("watch_levels") or [])][:5],
            "risk_flags": [str(x) for x in (data.get("risk_flags") or [])][:5],
            "action_bias": str(data.get("action_bias") or "等待复核"),
            "does_not_override_rules": True,
            "cache_hit": False,
        }
    except Exception as exc:
        action_plan = payload.get("action_plan") or {}
        result = {
            "available": False,
            "status": "unavailable",
            "provider": os.getenv("LANGCHAIN_PROVIDER", ""),
            "model": os.getenv("LANGCHAIN_MODEL_NAME", ""),
            "verdict": "DeepSeek复核不可用",
            "summary": f"DeepSeek 暂未完成总结：{type(exc).__name__}。请先按行动区间和风险位人工复核。",
            "bull_points": [],
            "bear_points": [],
            "watch_levels": [action_plan.get("plain_cn")] if action_plan.get("plain_cn") else [],
            "risk_flags": ["llm_summary_unavailable"],
            "action_bias": "人工复核",
            "does_not_override_rules": True,
            "cache_hit": False,
        }
    cache_set(cache_key, result, ttl_seconds=6 * 3600)
    cache_set(stable_key, result, ttl_seconds=6 * 3600)
    return result


def _single_company_facts(symbol: str) -> Dict[str, Any]:
    """Structured company facts from yfinance .info (free, no key needed)."""
    if not external_data_allowed():
        return {}
    try:
        import yfinance as yf

        info = yf.Ticker(symbol).info or {}
    except Exception:
        info = {}
    return {
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "country": info.get("country"),
        "employees": info.get("fullTimeEmployees"),
        "website": info.get("website"),
        "market_cap": info.get("marketCap"),
        "business_summary_en": info.get("longBusinessSummary"),
    }


def _single_enrich_company_facts(symbol: str, facts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge structured company facts from the available providers.

    Old cached company profiles may contain the AI-written business portrait but
    an empty ``facts`` object.  This helper refreshes only structured facts so we
    can repair market cap / sector / industry without forcing another LLM call.
    """
    merged: Dict[str, Any] = dict(facts or {})
    if not external_data_allowed():
        return merged
    try:
        from market_data_service import get_company_fundamentals, get_ticker_reference

        fmp = get_company_fundamentals(symbol) or {}
        if fmp.get("company_name") and not merged.get("name"):
            merged["name"] = fmp["company_name"]
        if fmp.get("sector") and not merged.get("sector"):
            merged["sector"] = fmp["sector"]
        if fmp.get("industry") and not merged.get("industry"):
            merged["industry"] = fmp["industry"]
        if fmp.get("market_cap"):
            merged["market_cap"] = fmp["market_cap"]
        if fmp.get("revenue_yoy") is not None:
            merged["revenue_yoy"] = fmp["revenue_yoy"]

        ref = get_ticker_reference(symbol) or {}
        if ref.get("name") and not merged.get("name"):
            merged["name"] = ref["name"]
        if ref.get("market_cap") and not merged.get("market_cap"):
            merged["market_cap"] = ref["market_cap"]
        if ref.get("sic_description") and not merged.get("industry"):
            merged["industry"] = ref["sic_description"]
        if ref.get("homepage_url") and not merged.get("website"):
            merged["website"] = ref["homepage_url"]
        if ref.get("total_employees") and not merged.get("employees"):
            merged["employees"] = ref["total_employees"]
        if ref.get("source"):
            merged["reference_source"] = ref["source"]
    except Exception:
        pass
    return merged


def _single_inferred_upstream_suppliers(profile: Dict[str, Any], facts: Dict[str, Any]) -> List[str]:
    """Conservative category-level upstream fallback for sparse AI profiles."""
    blob = " ".join(
        str(x or "")
        for x in [
            facts.get("industry"),
            facts.get("sector"),
            facts.get("business_summary_en"),
            profile.get("business_cn"),
            profile.get("segment_cn"),
            " ".join(profile.get("products") or []),
        ]
    ).lower()
    items: List[str] = []
    if any(token in blob for token in ("wireless", "radio", "communication", "fullmax", "lte", "rf")):
        items.extend(["射频/通信芯片供应商（推断）", "工业电子元件供应商（推断）"])
    if any(token in blob for token in ("drone", "uav", "autonomous", "robot", "无人机")):
        items.extend(["飞控与无人机机体组件供应商（推断）", "摄像头/传感器组件供应商（推断）"])
    if any(token in blob for token in ("rail", "铁路", "utility", "oil and gas")):
        items.append("工业通信设备代工与安装服务商（推断）")
    return list(dict.fromkeys(items))[:5]


def _single_profile_research_overlay(symbol: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    """Attach dated display data without altering the cached qualitative profile."""
    from gildata_shadow_service import cached_equity
    from market_calendar import most_recent_session
    from market_data_service import get_market_cap_snapshot

    session = most_recent_session().isoformat()
    out = {**profile, "facts": dict(profile.get("facts") or {}), "market_session": session}
    if isinstance(out.get("analyst"), dict):
        analyst = dict(out["analyst"])
        for key in ("target_upside", "target_price_reference", "target_signal"):
            analyst.pop(key, None)
        out["analyst"] = analyst
    cap = get_market_cap_snapshot(symbol, refresh=external_data_allowed())
    if cap.get("available"):
        out["market_cap_meta"] = cap
        if not cap.get("stale"):
            out["facts"]["market_cap"] = cap["market_cap"]
    elif out["facts"].get("market_cap"):
        out["market_cap_meta"] = {"source": "legacy_profile_cache", "stale": True}
    shadow = cached_equity(symbol)
    if shadow:
        out["gildata_research"] = {
            "source": shadow["source"],
            "as_of": shadow["as_of"],
            "is_latest_session": shadow["as_of"] == session,
            "ratings": shadow.get("ratings"),
            "target_avg_usd": shadow.get("target_avg_usd"),
            "target_count": shadow.get("target_count"),
            "target_window_days": shadow.get("target_window_days"),
            "pe": shadow.get("pe"),
            "pb": shadow.get("pb"),
            "ps": shadow.get("ps"),
        }
    return out


def _single_company_profile(symbol: str) -> Dict[str, Any]:
    """Company fundamentals card: structured facts (yfinance) + an LLM-written
    qualitative profile (business, supply chain, customers, competitors, moat,
    track position) in Chinese.  Cached 7 days per symbol -- this is slow-moving
    reference info, not a trading signal.  The qualitative part is clearly
    AI-generated and may be wrong.
    """
    sym = str(symbol or "").upper()
    cache_key = f"single_company_profile:v3:{sym}"
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        facts = cached.get("facts") if isinstance(cached.get("facts"), dict) else {}
        if external_data_allowed() and not facts.get("market_cap"):
            fresh = _single_enrich_company_facts(sym, _single_company_facts(sym))
            if any(fresh.get(k) for k in ("market_cap", "name", "sector", "industry")):
                cached = {**cached, "facts": {**fresh, **{k: v for k, v in facts.items() if v not in (None, "")}}}
                cache_set(cache_key, cached, ttl_seconds=7 * 24 * 3600)
                facts = cached.get("facts") if isinstance(cached.get("facts"), dict) else facts
        if external_data_allowed() and not cached.get("upstream_suppliers"):
            inferred = _single_inferred_upstream_suppliers(cached, facts)
            if inferred:
                cached = {**cached, "upstream_suppliers": inferred, "upstream_suppliers_inferred": True}
                cache_set(cache_key, cached, ttl_seconds=7 * 24 * 3600)
        analyst = cached.get("analyst") if isinstance(cached.get("analyst"), dict) else {}
        if external_data_allowed() and (analyst and not analyst.get("rating_stats")):
            try:
                from market_data_service import get_analyst_view

                fresh_analyst = get_analyst_view(sym) or {}
                if fresh_analyst.get("available"):
                    cached = {**cached, "analyst": fresh_analyst}
                    cache_set(cache_key, cached, ttl_seconds=7 * 24 * 3600)
            except Exception:
                pass
        return _single_profile_research_overlay(sym, {**cached, "cache_hit": True})

    if not external_data_allowed():
        return {"available": False, "status": "loading", "symbol": sym}
    facts = _single_enrich_company_facts(sym, _single_company_facts(sym))
    # Prefer FMP real fundamentals for the structured facts when available.
    try:
        if not external_data_allowed():
            raise RuntimeError("page_read_cache_only")
        from market_data_service import get_analyst_view, get_company_fundamentals, get_next_earnings

        fmp = get_company_fundamentals(sym) or {}
        if fmp.get("sector"):
            facts["sector"] = fmp["sector"]
        if fmp.get("industry"):
            facts["industry"] = fmp["industry"]
        if fmp.get("market_cap"):
            facts["market_cap"] = fmp["market_cap"]
        if fmp.get("revenue_yoy") is not None:
            facts["revenue_yoy"] = fmp["revenue_yoy"]
        earnings = get_next_earnings(sym) or {}
        analyst = get_analyst_view(sym) or {}
    except Exception:
        earnings, analyst = {}, {}
    profile: Dict[str, Any] = {
        "available": True,
        "symbol": sym,
        "facts": facts,
        "earnings": earnings if earnings.get("available") else None,
        "analyst": analyst if analyst.get("available") else None,
        "ai_available": False,
        "ai_note": "供应链/客户/竞争对手/赛道地位为 AI 整理，仅供参考，可能有误。",
        "cache_hit": False,
    }
    try:
        from src.providers.chat import ChatLLM

        ctx = {
            "symbol": sym,
            "name": facts.get("name"),
            "sector": facts.get("sector"),
            "industry": facts.get("industry"),
            "business_summary_en": (facts.get("business_summary_en") or "")[:1500],
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是美股行业研究助手。基于给定公司信息和你已知的事实，用中文输出该公司的基本面画像。"
                    "只输出严格 JSON，不要编造具体数字；不确定的字段给空数组或空字符串。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请输出 JSON，字段："
                    "business_cn(一句话主营业务,中文)、"
                    "segment_cn(所属板块/细分赛道,中文)、"
                    "products(主要产品或服务,数组)、"
                    "upstream_suppliers(主要上游/供应链合作厂商,数组,只列知名且较确定的)、"
                    "downstream_customers(主要下游客户/往哪供货,数组)、"
                    "competitors(主要竞争对手,数组)、"
                    "moat(核心竞争力/护城河,1-2句中文)、"
                    "track_position(赛道地位:是独占/领先/跟随,并一句话说明,中文)、"
                    "track_cn(该公司所属的细分赛道名,中文,如'AI应用软件')、"
                    "peer_tickers(该细分赛道里主要的美股上市公司代码,含本公司,数组,只列真实美股ticker如['APP','TTD','ZETA'],最多8个)。"
                    f"\n公司信息：{json.dumps(ctx, ensure_ascii=False)}"
                ),
            },
        ]
        response = ChatLLM().chat(messages, timeout=int(os.getenv("SINGLE_STOCK_PROFILE_TIMEOUT", "45")))
        text = response.content or "{}"
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1] if start >= 0 and end > start else text)
        profile.update({
            "ai_available": True,
            "business_cn": str(data.get("business_cn") or ""),
            "segment_cn": str(data.get("segment_cn") or ""),
            "products": [str(x) for x in (data.get("products") or [])][:8],
            "upstream_suppliers": [str(x) for x in (data.get("upstream_suppliers") or [])][:8],
            "downstream_customers": [str(x) for x in (data.get("downstream_customers") or [])][:8],
            "competitors": [str(x) for x in (data.get("competitors") or [])][:8],
            "moat": str(data.get("moat") or ""),
            "track_position": str(data.get("track_position") or ""),
            "track_cn": str(data.get("track_cn") or ""),
            "peer_tickers": [str(x).strip().upper() for x in (data.get("peer_tickers") or []) if str(x).strip()][:8],
            "model": os.getenv("LANGCHAIN_MODEL_NAME", ""),
        })
    except Exception as exc:
        profile["ai_note"] = f"AI 画像暂不可用（{type(exc).__name__}）；以上为公开基础资料。"
    if not profile.get("upstream_suppliers"):
        inferred = _single_inferred_upstream_suppliers(profile, facts)
        if inferred:
            profile["upstream_suppliers"] = inferred
            profile["upstream_suppliers_inferred"] = True
    cache_set(cache_key, profile, ttl_seconds=7 * 24 * 3600)
    return _single_profile_research_overlay(sym, profile)


def _track_leader_compare(symbol: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    """Rank the stock vs its same-track peers by LIQUIDITY (avg daily $ volume),
    so the user can weigh the model's oversold pick against the track leader.

    Leader = most liquid name in the track. Liquidity is framed as exit/recovery
    margin (you can get out if wrong) -- NOT a guarantee of recovery. We also flag
    whether the leader itself is in a pullback (i.e. whether you can get both the
    validated edge AND the liquidity by buying the leader instead).
    """
    sym = str(symbol or "").upper()
    peers = [t for t in (profile.get("peer_tickers") or []) if t]
    universe = list(dict.fromkeys([sym] + peers))[:8]
    if len(universe) < 2:
        return {"available": False, "reason": "无同赛道可比公司（AI 未给出 peer_tickers）"}
    cache_key = f"track_leader_compare:v1:{sym}:{','.join(sorted(universe))}"
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        return {**cached, "cache_hit": True}
    if not external_data_allowed():
        return {"available": False, "status": "loading", "symbol": sym}
    from priority_board_service import _adv, _liquidity_tier, single_stock_signal_read

    rows: List[Dict[str, Any]] = []
    for t in universe:
        try:
            # Massive (unlimited) is the primary per-symbol source now, so this
            # interactive fetch gives full peer coverage without burning Tiingo.
            frame, _src = get_daily_history(t, period="6mo")
            if frame is None or frame.empty:
                continue
            adv = _adv(frame)
            close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna()
            price = float(close.iloc[-1]) if len(close) else None
            sr = single_stock_signal_read(t, frame=frame)  # pullback read for the peer
            rows.append({
                "symbol": t,
                "is_query": t == sym,
                "adv": adv,
                "liquidity": _liquidity_tier(adv),
                "current_price": round(price, 2) if price else None,
                "calibrated_win_rate": (sr or {}).get("calibrated_win_rate"),
                "pullback_state": (sr or {}).get("pullback_state"),
                "in_pullback": str((sr or {}).get("pullback_state") or "") in {"深度回调(超卖)", "回调中"},
            })
        except Exception:
            continue
    rows = [r for r in rows if r.get("adv")]
    if not rows:
        return {"available": False, "reason": "同赛道公司行情不足"}
    rows.sort(key=lambda r: r["adv"], reverse=True)
    leader = rows[0]
    query = next((r for r in rows if r["is_query"]), None)
    # Verdict logic (honest): leader vs the model's pick.
    if query and query["symbol"] == leader["symbol"]:
        verdict = "本标的就是该赛道流动性龙头——回调 edge + 流动性兼得，最佳情形。"
    elif leader.get("in_pullback"):
        ratio = (leader["adv"] / query["adv"]) if (query and query.get("adv")) else None
        verdict = (f"龙头 {leader['symbol']} 当前也在回调，且流动性更强"
                   + (f"(约 {ratio:.0f}×)" if ratio else "")
                   + "——同样能拿回调 edge，容错更高，优先考虑龙头。")
    else:
        verdict = (f"本标的非龙头(流动性较弱)；龙头 {leader['symbol']} 当前未回调。"
                   "要吃回调 edge 只能买本标的，但流动性弱=判断错了不易脱身，务必控制仓位/止损。")
    result = {
        "available": True,
        "track_cn": profile.get("track_cn") or profile.get("segment_cn"),
        "leader": leader["symbol"],
        "query_is_leader": bool(query and query["symbol"] == leader["symbol"]),
        "members": rows,
        "verdict": verdict,
        "note": "龙头按平均每日成交额(流动性)排序。流动性=判断错时的脱身/容错空间，不等于一定回本。",
        "cache_hit": False,
    }
    cache_set(cache_key, result, ttl_seconds=24 * 3600)
    return result


def _single_enrich_norm(value: Any) -> Dict[str, Any]:
    """Normalize a slow-section read: if it isn't available yet (cold cache),
    mark it 'loading' so the frontend knows to poll /enrich for it."""
    if isinstance(value, dict) and value.get("available"):
        return value
    base = value if isinstance(value, dict) else {}
    return {**base, "available": False, "status": "loading"}


def _warm_single_stock_enrichment(symbol: str, current_price: Optional[float], hv20: Optional[float]) -> None:
    """Background warm for the slow sections (company profile + leader compare +
    option IV). External fetches ALLOWED so a cold symbol fills in; each function
    caches its own result. Leader + IV run with a hard timeout because yfinance's
    option_chain has no timeout of its own and can hang -- a hang must NOT stop
    the 'done' marker (otherwise the frontend polls forever)."""
    sym = str(symbol or "").upper()
    iv_key = f"single_stock_iv:v8:{sym}"
    try:
        with external_data_scope(True):
            prof = _single_company_profile(sym)  # LLM-backed; ~20s on a cold symbol

        def _warm_leader():
            with external_data_scope(True):
                _track_leader_compare(sym, prof if isinstance(prof, dict) else {})

        def _warm_iv():
            with external_data_scope(True):
                _single_option_iv(sym, current_price, hv20)

        for fut in (_SSO_ENRICH_POOL.submit(_warm_leader), _SSO_ENRICH_POOL.submit(_warm_iv)):
            try:
                fut.result(timeout=30)
            except Exception as exc:
                console.log(f"enrich warm section failed {sym}: {str(exc)[-150:]}")
    except Exception as exc:
        console.log(f"enrich warm failed {sym}: {str(exc)[-200:]}")
    finally:
        # If IV never resolved (e.g. yfinance hung), write a terminal marker so
        # the page shows 'IV unavailable' instead of polling forever.
        if not isinstance(cache_get(iv_key), dict):
            cache_set(iv_key, {"available": False, "status": "timeout",
                               "status_cn": "期权源响应超时，IV 暂不可用（稍后重开再试）"}, ttl_seconds=1800)
        cache_set(f"single_stock_enrich_done:v1:{sym}", True, ttl_seconds=900)


def _single_metric_benchmarks(row: Dict[str, Any], iv_obj: Dict[str, Any], correlation: Dict[str, Any],
                              signal_read: Dict[str, Any], leader: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build a 'is this high or low?' reference table for the key metrics: each
    with a neutral/reference band, a high/low verdict (for highlighting), and the
    same-track leader's value where we have it. Pure compute, no fetches."""
    def f(v):
        try:
            x = float(v)
            return x if x == x else None  # drop NaN
        except (TypeError, ValueError):
            return None

    # Same-track leader's calibrated win-rate (from the leader-compare card).
    lead_wr = None
    if isinstance(leader, dict) and leader.get("available"):
        for m in (leader.get("members") or []):
            if m.get("symbol") == leader.get("leader"):
                lead_wr = f(m.get("calibrated_win_rate"))
                break
    leader_name = leader.get("leader") if isinstance(leader, dict) else None

    out: List[Dict[str, Any]] = []

    wr = f((signal_read or {}).get("calibrated_win_rate"))
    if wr is None:
        wr = f(row.get("calibrated_probability") or row.get("unified_probability"))
    if wr is not None:
        verdict = "good" if wr >= 0.53 else ("bad" if wr < 0.50 else "neutral")
        out.append({"key": "calibrated_win_rate", "label": "校准胜率(跑赢自身基线)", "value": round(wr, 4), "fmt": "pct1",
                    "ref": "中性线 50% · ≥53% 偏好 · <50% 预计跑不赢", "verdict": verdict,
                    "tone": {"good": "green", "bad": "red", "neutral": "muted"}[verdict],
                    "leader_value": round(lead_wr, 4) if lead_wr is not None else None,
                    "leader_symbol": leader_name,
                    "hint": "扣成本后跑赢该股自身基线的概率——不是赚钱概率，更不是绝对胜率"})

    rs = f(row.get("relative_strength_20d"))
    if rs is not None:
        verdict = "good" if rs > 0.05 else ("bad" if rs < -0.05 else "neutral")
        out.append({"key": "relative_strength_20d", "label": "相对大盘强度(20日)", "value": round(rs, 4), "fmt": "pct1s",
                    "ref": "中性 0 · >+5% 强于大盘 · <−5% 弱于大盘", "verdict": verdict,
                    "tone": {"good": "green", "bad": "red", "neutral": "muted"}[verdict],
                    "hint": "近20日相对 SPY 的超额涨幅；趋势参考，非前向预测"})

    ivhv = f(iv_obj.get("iv_hv20"))
    if ivhv is not None:
        verdict = "high" if ivhv > 1.2 else ("low" if ivhv < 0.85 else "neutral")
        out.append({"key": "iv_hv", "label": "IV/HV(期权贵贱)", "value": round(ivhv, 2), "fmt": "x",
                    "ref": "中性 ~1.0 · >1.2 期权偏贵(市场预期波动大) · <0.85 偏便宜", "verdict": verdict,
                    "tone": {"high": "amber", "low": "sky", "neutral": "muted"}[verdict],
                    "hint": "隐含波动相对真实历史波动的高低；偏贵常意味着将有事件/财报"})

    iv = f(iv_obj.get("atm_iv"))
    if iv is not None:
        verdict = "high" if iv > 0.45 else ("low" if iv < 0.25 else "neutral")
        out.append({"key": "atm_iv", "label": "隐含波动率(年化)", "value": round(iv, 4), "fmt": "pct1",
                    "ref": "粗略带 <25% 低 · 25–45% 中 · >45% 高", "verdict": verdict,
                    "tone": {"high": "amber", "low": "muted", "neutral": "muted"}[verdict],
                    "hint": "期权隐含的未来年化波动；绝对带为粗略参考，宜结合赛道"})

    beta = f(correlation.get("beta"))
    if beta is not None:
        verdict = "high" if beta > 1.3 else ("low" if beta < 0.7 else "neutral")
        out.append({"key": "beta", "label": "Beta(对大盘敏感度)", "value": round(beta, 2), "fmt": "x",
                    "ref": "中性 1.0 · >1.3 涨跌放大 · <0.7 防御性", "verdict": verdict,
                    "tone": {"high": "amber", "low": "sky", "neutral": "muted"}[verdict],
                    "hint": "对大盘波动的放大倍数；高 beta 单票方差更大，仓位要更小"})

    return out


_CANDLE_CFG = {
    "daily": (1, "day", 1100),   # ~3 years of daily bars (Massive Starter has 5y)
    "1h": (1, "hour", 60),
    "15m": (15, "minute", 20),
    "5m": (5, "minute", 10),
}


def _single_candles(symbol: str, timeframe: str = "daily") -> Dict[str, Any]:
    """Cached daily chart first; intraday retains its existing provider path."""
    sym = str(symbol or "").upper().strip()
    tf = timeframe if timeframe in _CANDLE_CFG else "daily"
    cache_key = f"single_candles:v3:{sym}:{tf}"
    cached = cache_get(cache_key)
    daily_frame = None
    daily_source = None
    if tf == "daily":
        with external_data_scope(False):
            daily_frame, daily_source = get_daily_history(sym, period="2y")
        if isinstance(cached, dict) and daily_frame is not None and not daily_frame.empty:
            date_key = daily_frame.index[-1].strftime("%Y-%m-%d")
            last_bar = (cached.get("candles") or [{}])[-1]
            if cached.get("as_of_date") != date_key or _single_float(last_bar.get("c")) != _single_float(daily_frame["Close"].iloc[-1]):
                cached = None
    if isinstance(cached, dict):
        return {**cached, "cache_hit": True}
    mult, span, days = _CANDLE_CFG[tf]
    end = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    from market_data_service import _massive_get

    massive_error = ""
    source = "massive:aggregates"
    try:
        payload = {} if daily_frame is not None and not daily_frame.empty else _massive_get(
            f"/v2/aggs/ticker/{sym}/range/{mult}/{span}/{start}/{end}",
            adjusted="true", sort="asc", limit=50000)
        rows = (payload or {}).get("results") or []
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        massive_error = f"massive_http_{status}" if status else f"massive_{type(exc).__name__}"
        rows = []
    candles = [{"t": int(r["t"]), "o": r.get("o"), "h": r.get("h"), "l": r.get("l"),
                "c": r.get("c"), "v": r.get("v")} for r in rows if r.get("c") is not None]
    if tf == "daily" and not candles:
        try:
            if daily_frame is None or daily_frame.empty:
                daily_frame, source = get_daily_history(sym, period="2y", skip_massive=True)
            else:
                source = daily_source
        except Exception as exc:
            massive_error = massive_error or f"daily_fallback_{type(exc).__name__}"
        if daily_frame is not None and not daily_frame.empty:
            source = source or "cache:ohlcv"
            for stamp, bar in daily_frame.tail(1100).iterrows():
                try:
                    prices = [float(bar[key]) for key in ("Open", "High", "Low", "Close")]
                    if not all(math.isfinite(value) for value in prices):
                        continue
                    ts = pd.Timestamp(stamp)
                    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
                    volume = float(bar.get("Volume", 0) or 0)
                    candles.append({"t": int(ts.timestamp() * 1000), "o": prices[0], "h": prices[1],
                                    "l": prices[2], "c": prices[3], "v": volume if math.isfinite(volume) else 0})
                except (TypeError, ValueError, KeyError):
                    continue
    candles = _sanitize_candle_rows(candles)  # drop bad prints / clamp corrupt wicks
    # Daily only: mark historical pullback_hv triggers (the validated buy signal),
    # using the exact backtest replay collector so markers are real, not faked.
    markers: list = []
    if tf == "daily" and candles:
        try:
            from scripts.research_signal_framework_backtest import _collect_symbol_events
            import pandas as _pd

            frame = daily_frame if daily_frame is not None else get_daily_history(sym, period="2y")[0]
            # include_unresolved=True also scores the trailing ~10 sessions (today
            # included) whose forward return is not measurable yet, so a fresh
            # pullback_hv setup gets a LIVE buy marker -- the historical-only
            # collector structurally skips the last `horizon` bars.
            evs = _collect_symbol_events(sym, frame, 10, min_launch_score=0.0, min_tunnel_score=0.0,
                                         cooldown_days=5, include_unresolved=True)
            scored = [e for e in evs if e.get("pullback_hv_score") is not None]
            # Cutoff from RESOLVED history only, so the live bar uses the same bar as past Bs.
            resolved_vals = sorted(e["pullback_hv_score"] for e in scored if e.get("resolved"))
            if resolved_vals:
                cutoff = max(0.58, resolved_vals[int(0.8 * (len(resolved_vals) - 1))])  # top ~20% (oversold + HV expanding)
                cl = _pd.to_numeric(frame["Close"], errors="coerce")
                close_map = {ts.strftime("%Y-%m-%d"): float(v) for ts, v in zip(frame.index, cl) if _pd.notna(v)}
                for e in scored:
                    if e["pullback_hv_score"] >= cutoff:
                        px = close_map.get(e["date"])
                        if px:
                            live = not e.get("resolved", True)
                            markers.append({
                                "time": e["date"], "price": round(px, 2), "side": "BUY", "live": live,
                                "reason": (f"今日/近期 pullback_hv 设置 {e['pullback_hv_score']:.2f}（未到期）" if live
                                           else f"pullback_hv {e['pullback_hv_score']:.2f}"),
                            })
                markers = markers[-60:]  # cap
        except Exception as exc:
            console.log(f"pullback markers failed {sym}: {str(exc)[-150:]}")
    as_of_date = datetime.fromtimestamp(candles[-1]["t"] / 1000, timezone.utc).date().isoformat() if candles else None
    from market_calendar import most_recent_session
    stale = bool(tf == "daily" and as_of_date and as_of_date < most_recent_session().isoformat())
    if not candles:
        note = "分时行情目前仅接 Massive；该源暂不可用。" if tf != "daily" else "日线及现有缓存均不可用。"
    elif source == "massive:aggregates":
        note = f"Massive 行情，最新 {as_of_date}；非实时成交报价。"
    else:
        note = f"日线备用源 {source}，最新 {as_of_date}；可能与复权图口径不同。"
    if stale:
        note += " 数据早于最近完成的美股交易日，可能滞后。"
    out = {
        "available": bool(candles), "timeframe": tf, "symbol": sym,
        "candles": candles, "count": len(candles), "markers": markers, "delayed": True,
        "source": source if candles else None, "as_of_date": as_of_date,
        "stale": stale,
        "reason": massive_error or ("no_chart_data" if not candles else None),
        "note": note,
        "cache_hit": False,
    }
    if candles:
        cache_set(cache_key, out, ttl_seconds=3600 if tf == "daily" else 600)
    return out


def _single_fib_levels(frame, current_price: Optional[float], window: int = 120) -> Dict[str, Any]:
    """Fibonacci retracement levels over the recent swing — a DISPLAY-LAYER price
    reference (entry zone / stop), NOT a validated signal. For a pullback-buy the
    38.2–61.8% retracement of the prior up-leg is the classic buy zone."""
    try:
        import pandas as pd

        high = pd.to_numeric(frame["High"], errors="coerce").dropna().tail(window)
        low = pd.to_numeric(frame["Low"], errors="coerce").dropna().tail(window)
    except Exception:
        return {"available": False}
    if high.empty or low.empty or not current_price or current_price <= 0:
        return {"available": False}
    hi, lo = float(high.max()), float(low.min())
    if hi <= lo:
        return {"available": False}
    rng = hi - lo
    trend = "up" if high.idxmax() >= low.idxmin() else "down"
    ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    levels = []
    for r in ratios:
        price = hi - rng * r if trend == "up" else lo + rng * r
        levels.append({"ratio": r, "label": f"{r * 100:.1f}%", "price": round(price, 2)})
    levels.sort(key=lambda x: x["price"], reverse=True)
    zone = None
    for i in range(len(levels) - 1):
        if levels[i + 1]["price"] <= current_price <= levels[i]["price"]:
            zone = {"upper": levels[i], "lower": levels[i + 1]}
            break
    buy_low = round(hi - rng * 0.618, 2)
    buy_high = round(hi - rng * 0.382, 2)
    in_buy = trend == "up" and buy_low <= current_price <= buy_high
    return {
        "available": True,
        "trend": trend,
        "trend_cn": "上涨后回调（看回撤买点）" if trend == "up" else "下跌后反弹（看反弹阻力）",
        "swing_high": round(hi, 2),
        "swing_low": round(lo, 2),
        "window_days": int(window),
        "current_price": round(float(current_price), 2),
        "levels": levels,
        "zone": zone,
        "shallow_zone": {"low": buy_low, "high": buy_high} if trend == "up" else None,
        "in_shallow_zone": bool(in_buy),
        "evidence": "negative",
        "note": "斐波那契仅作画线参考(支撑/阻力)。⚠️ 回测结论：本系统的超卖回调 edge 在 38.2–61.8% 浅回撤区反而显著为负(−0.88%/10日)，真正的 edge 在更深回撤(不在该区, +0.46%)——所以 fib 浅回撤区不是买点，勿当信号用。",
    }


def _build_single_stock_payload(safe_symbol: str, universe: str, period: str,
                                *, include_context: bool = True) -> Dict[str, Any]:
    """Assemble cached research sections; provider refresh is a separate job."""
    from priority_board_service import single_stock_signal_read

    started = time.perf_counter()

    # Phase 1b: fast base row + recent frame (cache-only now).
    with external_data_scope(False):
        snapshot_row = _single_stock_row_from_snapshot(safe_symbol)
        explicit_universe = None if universe == "auto" else (universe.strip().lower() or None)
        if snapshot_row and universe == "auto":
            row = dict(snapshot_row)
            needs_context = not any(row.get(k) is not None for k in ("empirical_win_rate", "proxy_win_rate", "historical_source"))
        else:
            row = {"symbol": safe_symbol}
            needs_context = True
        if not row.get("symbol"):
            row["symbol"] = safe_symbol
        recent_frame = get_daily_history(safe_symbol, period="6mo")[0]
    hv20 = _single_hv(recent_frame, 20)
    hv30 = _single_hv(recent_frame, 30)
    distribution_risk = detect_distribution_risk(recent_frame)
    if not recent_frame.empty:
        lc = _single_float(recent_frame["Close"].iloc[-1])
        if lc is not None:
            row["current_price"] = lc
    current_price = _single_float(row.get("current_price"))
    # Day change (latest cached close vs the prior session) for the page header.
    day_change = None
    try:
        import pandas as _pd

        _c = _pd.to_numeric(recent_frame["Close"], errors="coerce").dropna()
        if len(_c) >= 2 and float(_c.iloc[-2]) > 0:
            _cur, _prev = float(_c.iloc[-1]), float(_c.iloc[-2])
            _last_dt = recent_frame.index[-1]
            day_change = {
                "price": round(_cur, 2),
                "prev_close": round(_prev, 2),
                "change": round(_cur - _prev, 2),
                "change_pct": round(_cur / _prev - 1.0, 4),
                "as_of": _last_dt.strftime("%Y-%m-%d") if hasattr(_last_dt, "strftime") else None,
            }
    except Exception:
        day_change = None
    alpha_input = {
        "symbol": safe_symbol,
        "source_universe_ids": row.get("source_universe_ids") or [],
        "source_pools": row.get("source_pools") or [],
        "pullback_rejection_status": row.get("pullback_rejection_status") or None,
        "pullback_confirmation_status": row.get("pullback_confirmation_status") or None,
    }

    def _cache_only(fn):
        # Sections run cache-only (threadlocal scope must be set in the worker).
        def _wrapped():
            with external_data_scope(False):
                return fn()
        return _wrapped

    # All sections are cache-only now -> the gather is fast (sub-second when warm).
    tasks: Dict[str, Any] = {
        "alpha": _cache_only(lambda: validate_symbol_overnight_alpha(alpha_input, period=period, min_edge=0.001, force_refresh=False)),
        "correlation": _cache_only(lambda: _single_stock_correlation_pack({**row, **alpha_input}, period)),
        "iv": _cache_only(lambda: _single_option_iv(safe_symbol, current_price, hv20)),
        "action_plan": _cache_only(lambda: _single_open_action_plan(safe_symbol, row, recent_frame)),
        "signal_read": _cache_only(lambda: single_stock_signal_read(safe_symbol, frame=recent_frame)),
        "company_profile": _cache_only(lambda: _single_company_profile(safe_symbol)),
        "earnings": _cache_only(lambda: get_next_earnings(safe_symbol)),
        "three_month_comparison": _cache_only(lambda: _single_three_month_comparison(safe_symbol, "SPY")),
        "leader_compare": _cache_only(lambda: _track_leader_compare(safe_symbol, _single_company_profile(safe_symbol))),
    }
    if needs_context and include_context:
        tasks["context_refresh"] = _cache_only(lambda: _compact_research_context(
            _build_symbol_research_context(safe_symbol, scan_universe=explicit_universe)))
    elif needs_context:
        row["context_status"] = "loading"
    base_ms = round((time.perf_counter() - started) * 1000, 1)
    gather_started = time.perf_counter()
    sec = _sso_gather(tasks, timeout=10.0)
    gather_ms = round((time.perf_counter() - gather_started) * 1000, 1)

    # Merge any freshly computed win-rate / context fields into the row.
    refreshed = sec.get("context_refresh")
    if isinstance(refreshed, dict) and refreshed.get("status") != "loading":
        row.update({k: v for k, v in refreshed.items() if v is not None})

    alpha = sec.get("alpha") if isinstance(sec.get("alpha"), dict) else {}
    company_profile = _single_enrich_norm(sec.get("company_profile"))
    leader = _single_enrich_norm(sec.get("leader_compare"))
    iv_obj = sec.get("iv") if isinstance(sec.get("iv"), dict) else {}
    metric_benchmarks = _single_metric_benchmarks(
        row, iv_obj, sec.get("correlation") or {}, sec.get("signal_read") or {}, leader)

    # Read cached news only; a miss must never delay the first response.
    try:
        from news_sentiment_service import estimate_impact, news_digest
        with external_data_scope(False):
            news = news_digest(safe_symbol)
        _atr = _single_atr(recent_frame)
        _atr_pct = (_atr / current_price) if (_atr and current_price and current_price > 0) else None
        _trump = news.get("trump") or {}
        news["impact"] = estimate_impact(
            news.get("net_score", 0.0), _atr_pct,
            trump_net=_trump.get("net_score") if _trump.get("mentioned") else None,
        )
    except Exception:
        news = {"available": False}

    return {
        "symbol": safe_symbol,
        "row": row,
        "news": news,
        "context": None,
        "overnight_alpha": alpha,
        "correlation": sec.get("correlation") or {},
        "three_month_comparison": sec.get("three_month_comparison") or {},
        "volatility": {
            "hv20": hv20,
            "hv30": hv30,
            "iv": _single_enrich_norm(sec.get("iv")),
            "definitions": {
                "iv": "隐含波动率：由期权价格反推的未来波动预期。这里取近月平值 Call/Put IV 的均值。",
                "hv": "历史波动率：过去实际日收益波动年化后的结果。",
                "iv_hv": "IV/HV：期权隐含波动相对真实历史波动的贵/便宜程度。",
            },
        },
        "action_plan": sec.get("action_plan") or {},
        "metric_definitions": _SINGLE_STOCK_METRIC_DEFINITIONS,
        "metric_benchmarks": metric_benchmarks,
        "day_change": day_change,
        "distribution_risk": distribution_risk,
        "fib_levels": _single_fib_levels(recent_frame, current_price),
        "signal_read": sec.get("signal_read") or {"available": False},
        "company_profile": company_profile,
        "earnings": sec.get("earnings") if isinstance(sec.get("earnings"), dict) else (company_profile.get("earnings") if isinstance(company_profile, dict) else None),
        "leader_compare": leader,
        "data_access_mode": "cache_first_background_refresh",
        "load_timing": {"base_ms": base_ms, "gather_ms": gather_ms,
                        "total_ms": round((time.perf_counter() - started) * 1000, 1)},
        "note": "先展示已有缓存，行情和资讯在后台按需更新；请留意各区块数据日期，后台完成后自动补齐。",
    }


def _single_stock_page_key(symbol: str, universe: str, period: str) -> str:
    return f"single_stock_page:v1:{symbol}:{universe}:{period}"


def _single_stock_cached_payload(symbol: str, universe: str, period: str) -> Dict[str, Any]:
    key = _single_stock_page_key(symbol, universe, period)
    cached = cache_get(key)
    if isinstance(cached, dict):
        return {**cached, "page_cache_hit": True}
    with external_data_scope(False):
        payload = _build_single_stock_payload(symbol, universe, period, include_context=False)
    payload["page_revision"] = uuid.uuid4().hex
    cache_set(key, _json_safe(payload), ttl_seconds=120)
    return payload


def _start_single_stock_refresh(symbol: str, universe: str, period: str) -> bool:
    key = _single_stock_page_key(symbol, universe, period)
    state_key = key + ":refresh"
    with _SSO_REFRESH_LOCK:
        state = cache_get(state_key) or {}
        if key in _SSO_REFRESH_ACTIVE or (
            state.get("status") == "completed" and time.time() - state.get("finished_at", 0) < 120
        ):
            return False
        if not _SSO_REFRESH_SLOTS.acquire(blocking=False):
            return False
        _SSO_REFRESH_ACTIVE.add(key)

    def _worker():
        status = "completed"
        try:
            cache_set(state_key, {"status": "running"}, ttl_seconds=900)
            with external_data_scope(True):
                for ticker in dict.fromkeys((symbol, "SPY")):
                    try:
                        get_daily_history(ticker, period=period)
                    except Exception as exc:
                        console.log(f"single-stock refresh failed {ticker}: {type(exc).__name__}")
                from news_sentiment_service import news_digest
                news_digest(symbol)
                get_next_earnings(symbol)
            with external_data_scope(False):
                payload = _build_single_stock_payload(symbol, universe, period)
            payload["page_revision"] = uuid.uuid4().hex
            payload["deepseek_summary"] = _single_stock_summary_lazy(payload)
            cache_set(key, _json_safe(payload), ttl_seconds=120)
            _warm_single_stock_enrichment(symbol, _single_float((payload.get("row") or {}).get("current_price")),
                                          (payload.get("volatility") or {}).get("hv20"))
        except Exception as exc:
            status = "unavailable"
            console.log(f"single-stock background failed {symbol}: {type(exc).__name__}")
        finally:
            cache_set(state_key, {"status": status, "finished_at": time.time()}, ttl_seconds=900)
            with _SSO_REFRESH_LOCK:
                _SSO_REFRESH_ACTIVE.discard(key)
                _SSO_REFRESH_SLOTS.release()

    try:
        threading.Thread(target=_worker, daemon=True, name=f"single-refresh-{symbol}").start()
    except Exception:
        with _SSO_REFRESH_LOCK:
            _SSO_REFRESH_ACTIVE.discard(key)
            _SSO_REFRESH_SLOTS.release()
        raise
    return True


@app.get("/single-stock-overnight/{symbol}", dependencies=[Depends(require_auth)])
async def single_stock_overnight(symbol: str, universe: str = Query("auto"), period: str = Query("2y", max_length=16)):
    """Single-symbol research cockpit. Sections fan out in parallel and the page
    runs in a worker thread (never blocks the event loop). The ~20-45s DeepSeek
    summary is LAZY -- returned from cache if warm, otherwise computed in the
    background while the page renders; the frontend polls /summary for it."""
    safe_symbol = _safe_event_ticker(symbol)
    started = time.perf_counter()
    universe = universe.strip().lower() or "auto"
    payload = await asyncio.to_thread(_single_stock_cached_payload, safe_symbol, universe, period)
    payload["deepseek_summary"] = await asyncio.to_thread(_single_stock_summary_lazy, payload)
    await asyncio.to_thread(_start_single_stock_refresh, safe_symbol, universe, period)
    console.log("[single-stock-timing] " + json.dumps({
        "symbol": safe_symbol, "total_ms": round((time.perf_counter() - started) * 1000, 1),
        "page_cache_hit": bool(payload.get("page_cache_hit")), "stages": payload.get("load_timing"),
    }))
    return _json_safe(payload)


@app.get("/single-stock-overnight/{symbol}/summary", dependencies=[Depends(require_auth)])
async def single_stock_overnight_summary(symbol: str):
    """Poll endpoint for the lazy DeepSeek summary (per-symbol latest)."""
    safe_symbol = _safe_event_ticker(symbol)
    cached = cache_get(f"single_stock_summary_latest:v1:{safe_symbol}")
    if isinstance(cached, dict):
        return _json_safe(cached)
    return _json_safe({"available": False, "status": "idle"})


@app.get("/single-stock-overnight/{symbol}/enrich", dependencies=[Depends(require_auth)])
async def single_stock_overnight_enrich(symbol: str, universe: str = Query("auto"),
                                      period: str = Query("2y", max_length=16), revision: str = Query("")):
    """Poll endpoint for the slow sections (company profile / leader compare /
    option IV) warmed in the background. Cache-only so it's fast; ``done`` flips
    true once the background warm has finished (so the frontend can stop)."""
    safe = _safe_event_ticker(symbol)

    def _read():
        with external_data_scope(False):
            prof = _single_company_profile(safe)
            leader = _track_leader_compare(safe, prof if isinstance(prof, dict) else {})
            iv = _single_option_iv(safe, None, None)
            earnings = get_next_earnings(safe)
        key = _single_stock_page_key(safe, universe.strip().lower() or "auto", period)
        page = cache_get(key)
        state = cache_get(key + ":refresh") or {}
        done = state.get("status") in {"completed", "unavailable"}

        def fin(v):
            if done:
                return v if isinstance(v, dict) else {"available": False}
            return _single_enrich_norm(v)

        return {
            "company_profile": fin(prof),
            "earnings": earnings if isinstance(earnings, dict) and earnings.get("available") else None,
            "leader_compare": fin(leader),
            "volatility": {"iv": fin(iv)},
            "core_payload": page if isinstance(page, dict) and page.get("page_revision") != revision else None,
            "refresh_status": state.get("status", "idle"),
            "done": done,
        }

    return _json_safe(await asyncio.to_thread(_read))


@app.get("/single-stock-overnight/{symbol}/candles", dependencies=[Depends(require_auth)])
async def single_stock_candles(symbol: str, timeframe: str = Query("daily")):
    """OHLCV candles for the in-app chart (Massive aggregates; DELAYED ~15min on
    Starter, not real-time tick). User-initiated single symbol -> external fetch
    allowed (Massive is unlimited)."""
    safe = _safe_event_ticker(symbol)
    return _json_safe(await asyncio.to_thread(_single_candles, safe, timeframe))


def _pdf_price_png(candles: list, price_lines: list) -> Optional[str]:
    """Near-3-month close line + EMA20 + horizontal action levels, as base64 PNG.
    Mobile-narrow; CJK font for any labels."""
    try:
        import base64
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "Noto Sans CJK JP", "sans-serif"]
        plt.rcParams["axes.unicode_minus"] = False
        cs = (candles or [])[-63:]
        closes = [float(c["c"]) for c in cs if c.get("c") is not None]
        if len(closes) < 2:
            return None
        k = 2.0 / 21.0
        ema, e = [], closes[0]
        for c in closes:
            e = c * k + e * (1 - k)
            ema.append(e)
        fig, ax = plt.subplots(figsize=(3.95, 2.05), dpi=110)
        ax.plot(closes, color="#16a34a", lw=1.3, label="收盘")
        ax.plot(ema, color="#f59e0b", lw=0.9, label="EMA20")
        for ln in (price_lines or []):
            try:
                ax.axhline(float(ln["price"]), color=ln.get("color", "#94a3b8"), ls="--", lw=0.7)
            except Exception:
                continue
        ax.legend(fontsize=6, loc="best", frameon=False)
        ax.tick_params(labelsize=6)
        ax.grid(alpha=0.18)
        fig.tight_layout(pad=0.4)
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        console.log(f"pdf price png failed: {str(exc)[-150:]}")
        return None


def _pdf_fmt(value, fmt: str) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "--"
    if fmt in ("pct1", "pct1s"):
        s = f"{v * 100:.1f}%"
        return f"+{s}" if fmt == "pct1s" and v > 0 else s
    if fmt == "x":
        return f"{v:.2f}x"
    return f"{v:.2f}"


def _build_research_pdf(picks: list) -> bytes:
    """Render the top picks into a single tall mobile-width PDF (no page splits)."""
    import weasyprint

    per_h = 1180
    total_h = 70 + max(1, len(picks)) * per_h
    blocks = []
    for p in picks:
        sym = str(p.get("symbol") or "").upper()
        try:
            with external_data_scope(False):
                payload = _build_single_stock_payload(sym, "auto", "2y")
        except Exception:
            payload = {"row": {}, "company_profile": {}, "action_plan": {}, "metric_benchmarks": []}
        candles = (_single_candles(sym, "daily") or {}).get("candles") or []
        row = payload.get("row") or {}
        dc = payload.get("day_change") or {}
        plan = payload.get("action_plan") or {}
        prof = (payload.get("company_profile") or {})
        facts = prof.get("facts") or {}
        sr = payload.get("signal_read") or {}
        dov = p.get("deep_oversold") or {}
        summary = cache_get(f"single_stock_summary_latest:v1:{sym}") or {}
        cur = dc.get("price") or row.get("current_price")
        plines = []
        if cur:
            plines.append({"price": float(cur), "color": "#3b82f6"})
        for key, col in (("buy_pullback_price", "#16a34a"), ("take_profit", "#16a34a"), ("stop_loss", "#ef4444")):
            try:
                if plan.get(key):
                    plines.append({"price": float(plan[key]), "color": col})
            except Exception:
                pass
        img = _pdf_price_png(candles, plines)

        chg = dc.get("change_pct")
        chg_html = ""
        if chg is not None:
            cls = "up" if chg > 0 else ("down" if chg < 0 else "")
            chg_html = f'<span class="{cls}">{"+" if chg >= 0 else ""}{chg * 100:.2f}%</span>'
        dov_html = ""
        if dov.get("level") == "deep":
            dov_html = '<span class="tag deep">深超卖</span>'
        elif dov.get("level") == "oversold":
            dov_html = '<span class="tag os">超卖</span>'

        metric_rows = "".join(
            f'<tr><td>{html.escape(str(m.get("label","")))}</td>'
            f'<td class="num">{_pdf_fmt(m.get("value"), m.get("fmt",""))}</td>'
            f'<td class="num muted">{_pdf_fmt(m.get("leader_value"), m.get("fmt","")) if m.get("leader_value") is not None else "--"}</td>'
            f'<td class="muted sm">{html.escape(str(m.get("ref","")))}</td></tr>'
            for m in (payload.get("metric_benchmarks") or [])
        )

        def _lvl(key):
            try:
                return f"${float(plan[key]):.2f}" if plan.get(key) else "--"
            except Exception:
                return "--"
        action_html = (
            f'<div class="kv"><span>回踩买</span><b class="g">{_lvl("buy_pullback_price")}</b></div>'
            f'<div class="kv"><span>止盈</span><b class="g">{_lvl("take_profit")}</b></div>'
            f'<div class="kv"><span>止损</span><b class="r">{_lvl("stop_loss")}</b></div>'
        ) if plan.get("available") else '<div class="muted sm">暂无行动价位</div>'

        biz = html.escape(str(prof.get("business_cn") or "")[:160])
        ai_v = html.escape(str(summary.get("verdict") or "--"))
        ai_s = html.escape(str(summary.get("summary") or "AI 复核生成中 / 暂无")[:280])
        mktcap = facts.get("market_cap")
        mktcap_s = (f"${mktcap/1e9:.1f}B" if isinstance(mktcap, (int, float)) and mktcap else "--")

        blocks.append(f"""
        <div class="card">
          <div class="hd">
            <div class="sym">{html.escape(sym)} {dov_html}</div>
            <div class="px">${float(cur):.2f} {chg_html}</div>
          </div>
          <div class="sub">{html.escape(str(facts.get("name") or ""))} · {html.escape(str(p.get("track_cn") or p.get("track") or "--"))}</div>
          {f'<img class="chart" src="data:image/png;base64,{img}"/>' if img else '<div class="muted sm">行情图暂不可用</div>'}
          <div class="h2">关键指标（本股 / 赛道龙头 / 参考）</div>
          <table class="mt"><tbody>{metric_rows or '<tr><td class="muted sm" colspan="4">指标加载中</td></tr>'}</tbody></table>
          <div class="h2">开盘后行动价位</div>
          <div class="act">{action_html}</div>
          <div class="h2">基本面</div>
          <div class="kv"><span>板块</span><b>{html.escape(str(facts.get("sector") or "--"))} / {html.escape(str(facts.get("industry") or "--"))}</b></div>
          <div class="kv"><span>市值</span><b>{mktcap_s}</b></div>
          <div class="biz">{biz or "（暂无业务简介）"}</div>
          <div class="h2">AI 复核：{ai_v}</div>
          <div class="ai">{ai_s}</div>
        </div>""")

    css = f"""
    @page {{ size: 414px {total_h}px; margin: 0; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: "Noto Sans CJK SC", sans-serif; margin: 0; padding: 10px; color: #0f172a; font-size: 12px; }}
    .top {{ font-size: 15px; font-weight: 700; margin-bottom: 8px; }}
    .top .sm {{ font-size: 10px; font-weight: 400; color: #64748b; }}
    .card {{ border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px; margin-bottom: 12px; }}
    .hd {{ display: flex; justify-content: space-between; align-items: baseline; }}
    .sym {{ font-size: 18px; font-weight: 700; }}
    .px {{ font-size: 16px; font-weight: 700; }}
    .up {{ color: #16a34a; }} .down {{ color: #dc2626; }}
    .g {{ color: #16a34a; }} .r {{ color: #dc2626; }}
    .sub {{ color: #64748b; font-size: 11px; margin: 2px 0 6px; }}
    .chart {{ width: 100%; border: 1px solid #f1f5f9; border-radius: 6px; }}
    .h2 {{ font-weight: 700; font-size: 12px; margin: 9px 0 4px; border-left: 3px solid #3b82f6; padding-left: 6px; }}
    table.mt {{ width: 100%; border-collapse: collapse; }}
    table.mt td {{ border-bottom: 1px solid #f1f5f9; padding: 3px 4px; vertical-align: top; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }}
    .muted {{ color: #94a3b8; }} .sm {{ font-size: 10px; }}
    .kv {{ display: flex; justify-content: space-between; padding: 2px 0; }}
    .act {{ }}
    .biz {{ color: #475569; font-size: 11px; line-height: 1.5; margin-top: 4px; }}
    .ai {{ color: #334155; font-size: 11px; line-height: 1.5; background: #f8fafc; border-radius: 6px; padding: 6px; }}
    .tag {{ font-size: 10px; padding: 1px 5px; border-radius: 4px; vertical-align: middle; }}
    .tag.deep {{ background: #fee2e2; color: #b91c1c; }} .tag.os {{ background: #fef3c7; color: #b45309; }}
    """
    html_doc = f"""<html><head><meta charset="utf-8"><style>{css}</style></head><body>
    <div class="top">回调买入榜 · 前 {len(picks)} 标的研判 <span class="sm">数据延迟~15min · 仅研究复核，非下单指令</span></div>
    {''.join(blocks)}
    </body></html>"""
    return weasyprint.HTML(string=html_doc).write_pdf()


@app.get("/priority-board/export-pdf", dependencies=[Depends(require_auth)])
async def export_priority_pdf(
    top: int = Query(3, ge=1, le=10),
    liquid_only: bool = Query(True),
    oversold_only: bool = Query(False),
):
    """One-click PDF (mobile-width single tall page) of the board's top picks —
    price chart + fundamentals + metrics + action levels + AI verdict."""
    from fastapi.responses import Response
    from priority_board_service import MAX_CACHED_PICKS, compute_priority_board, liquidity_floor

    def _run():
        board = compute_priority_board(limit=MAX_CACHED_PICKS)
        floor = liquidity_floor()
        th = float(floor.get("threshold_adv") or 0.0)
        picks = board.get("picks") or []
        if liquid_only and th > 0:
            picks = [p for p in picks if float((p.get("liquidity") or {}).get("adv") or 0.0) >= th]
        if oversold_only:
            picks = [p for p in picks if (p.get("deep_oversold") or {}).get("level") in ("deep", "oversold")]
        return _build_research_pdf(picks[:top])

    pdf = await asyncio.to_thread(_run)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="research_top{top}.pdf"'})


@app.get("/app-database/status", dependencies=[Depends(require_auth)])
async def app_database_status():
    """Return the embedded SQLite database status."""
    return database_status()


@app.get("/priority-board", dependencies=[Depends(require_auth)])
async def get_priority_board(
    limit: int = Query(8, ge=1, le=500),
    force_refresh: bool = Query(False),
    liquid_only: bool = Query(True),
    oversold_only: bool = Query(False),
):
    """Composite 'best available' priority ranking for UI prominence (every page).

    ``liquid_only`` (default on): high-liquidity floor. ``oversold_only``: keep
    only DEEP-oversold candidates (closed below the lower Bollinger band / z<-2 /
    RSI2<10) -- backtested to lift the pullback_hv edge (net +0.40%->+1.23%, hit
    53%->60%, CI>0). Both are DISPLAY filters only -- the full candidate set is
    still cached + logged to the prediction ledger.
    """
    from priority_board_service import MAX_CACHED_PICKS, compute_priority_board, liquidity_floor

    def _build():
        with external_data_scope(False):
            # Pull the full cached board, apply filters, then slice to limit.
            board = compute_priority_board(limit=MAX_CACHED_PICKS, force_refresh=force_refresh)
            floor = liquidity_floor()
            picks = board.get("picks") or []
            threshold = float(floor.get("threshold_adv") or 0.0)
            if liquid_only and threshold > 0:
                picks = [p for p in picks if float((p.get("liquidity") or {}).get("adv") or 0.0) >= threshold]
            if oversold_only:
                picks = [p for p in picks if (p.get("deep_oversold") or {}).get("level") in ("deep", "oversold")]
            board = {**board, "picks": picks[:limit],
                     "liquidity_floor": {**floor, "applied": bool(liquid_only)},
                     "oversold_only": bool(oversold_only)}
            return board

    return _json_safe(await asyncio.to_thread(_build))


@app.get("/priority-board/long-options", dependencies=[Depends(require_auth)])
async def get_priority_long_options():
    """Read only the last completed manual screen; never calls a paid feed."""
    from long_option_screen_service import read_snapshot

    return await asyncio.to_thread(read_snapshot)


@app.get("/priority-board/long-options/status", dependencies=[Depends(require_auth)])
async def get_priority_long_options_status():
    from long_option_screen_service import job_status

    return job_status()


@app.get("/priority-board/long-options/shadow", dependencies=[Depends(require_auth)])
async def get_priority_long_options_shadow(horizon: int = Query(5)):
    from long_option_shadow_service import scorecard

    try:
        return _json_safe(scorecard(horizon=horizon))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/priority-board/long-options/shadow/resolve", dependencies=[Depends(require_auth)])
async def resolve_priority_long_options_shadow():
    from long_option_shadow_service import resolve_pending, scorecard

    settled = await asyncio.to_thread(resolve_pending)
    return _json_safe({"resolve": settled, "scorecard": scorecard()})


@app.post("/priority-board/long-options/run", dependencies=[Depends(require_auth)])
async def run_priority_long_options():
    from long_option_screen_service import start_screen

    return start_screen()


@app.post("/priority-board/long-options/{symbol}/review", dependencies=[Depends(require_auth)])
async def review_priority_long_option(symbol: str):
    """DeepSeek review on explicit user action; normal snapshot reads never call an LLM."""
    from long_option_review_service import review_symbol

    return await asyncio.to_thread(review_symbol, symbol)


@app.get("/priority-board/basket", dependencies=[Depends(require_auth)])
async def get_priority_basket(
    budget: float = Query(10000, gt=0),
    n: int = Query(8, ge=2, le=20),
    liquid_only: bool = Query(True),
    weighting: str = Query("inverse_vol"),
):
    """Basket from today's board — the #1 noise-reducer for a small edge: spread
    the bet across N names instead of betting one. ``weighting``: 'inverse_vol'
    (default — size ~ 1/volatility so each name contributes roughly equal risk;
    high-vol names get a smaller slice) or 'equal'. Validation-period rules baked
    in: only names whose calibrated win-rate >= 50% and clearing the liquidity
    floor; if today has no edge, returns an empty basket. Cache-only (page read)."""
    from priority_board_service import MAX_CACHED_PICKS, compute_priority_board, liquidity_floor

    def _build():
        with external_data_scope(False):
            board = compute_priority_board(limit=MAX_CACHED_PICKS)
            floor = liquidity_floor()
            picks = board.get("picks") or []
            threshold = float(floor.get("threshold_adv") or 0.0)
            if liquid_only and threshold > 0:
                picks = [p for p in picks if float((p.get("liquidity") or {}).get("adv") or 0.0) >= threshold]
            eligible = [
                p for p in picks
                if float(p.get("calibrated_probability") or 0) >= 0.5 and float(p.get("current_price") or 0) > 0
            ]
            no_edge = bool(board.get("no_edge_today")) or not eligible
            chosen = eligible[:n]
            # Position sizing. inverse_vol: weight ~ 1/HV20 (each name contributes
            # ~equal risk; high-vol names get a smaller slice -> steadier basket).
            sigmas: Dict[str, Optional[float]] = {}
            for p in chosen:
                try:
                    frame, _s = get_daily_history(p["symbol"], period="3mo")
                    sigmas[p["symbol"]] = _single_hv(frame, 20)
                except Exception:
                    sigmas[p["symbol"]] = None
            valid = sorted(s for s in sigmas.values() if s and s > 0)
            med = valid[len(valid) // 2] if valid else None
            use_invvol = weighting == "inverse_vol" and bool(med)
            if use_invvol:
                raw = {p["symbol"]: 1.0 / (sigmas[p["symbol"]] if (sigmas.get(p["symbol"]) and sigmas[p["symbol"]] > 0) else med)
                       for p in chosen}
                total_raw = sum(raw.values()) or 1.0
                weights = {k: v / total_raw for k, v in raw.items()}
            else:
                weights = {p["symbol"]: (1.0 / len(chosen) if chosen else 0.0) for p in chosen}

            items = []
            deployed = 0.0
            for p in chosen:
                price = float(p.get("current_price") or 0)
                w = weights.get(p["symbol"], 0.0)
                target = budget * w
                shares = int(target // price) if price > 0 else 0
                cost = round(shares * price, 2)
                deployed += cost
                sig = sigmas.get(p["symbol"])
                items.append({
                    "symbol": p["symbol"],
                    "track": p.get("track"), "track_cn": p.get("track_cn"),
                    "current_price": round(price, 2),
                    "calibrated_probability": p.get("calibrated_probability"),
                    "hv20": round(sig, 4) if sig else None,
                    "weight": round(w, 4),
                    "target_amount": round(target, 2),
                    "shares": shares,
                    "est_cost": cost,
                    "stop_suggest": round(price * 0.92, 2),  # rough -8% stop; refine on the detail page
                    "liquidity": p.get("liquidity"),
                    "detail_url": p.get("detail_url"),
                })
            return {
                "available": bool(chosen),
                "no_edge_today": no_edge,
                "weighting": "inverse_vol" if use_invvol else "equal",
                "n_requested": n, "n_filled": len(chosen),
                "budget": round(budget, 2),
                "deployed": round(deployed, 2),
                "cash_left": round(budget - deployed, 2),
                "equal_weight_pct": round(100.0 / len(chosen), 2) if chosen else 0,
                "basket": items,
                "liquidity_floor": {"applied": bool(liquid_only), "threshold_adv": threshold},
                "generated_at": board.get("generated_at"),
                "note": "分散降噪(验证期核心)：别赌单票。波动率反比=高波动票仓位更小、各只风险贡献≈相等(比等权更稳)。仅含校准胜率≥50%且过流动性门槛的票。止损 -8% 为粗略建议，按个股研判页微调。参考清单，非下单指令。",
            }

    return _json_safe(await asyncio.to_thread(_build))


@app.get("/priority-board/monitor", dependencies=[Depends(require_auth)])
async def get_priority_board_monitor(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    days: int = Query(30, ge=1, le=120),
):
    """Full pullback-buying candidate monitor from persisted daily slices."""
    from prediction_ledger_service import priority_monitor

    return _json_safe(await asyncio.to_thread(priority_monitor, limit=limit, offset=offset, days=days))


def _prewarm_track_leaders(top_n: int = 30) -> Dict[str, Any]:
    """Pre-compute + cache the 赛道龙头对比 for the current top-N board picks, so
    the board-row expand serves from cache instantly. Run by the daily scan.
    Each symbol's profile (7d) + leader compare (24h) land in kv cache.
    """
    from priority_board_service import compute_priority_board

    board = compute_priority_board(limit=top_n)
    done, failed = 0, 0
    for pick in (board.get("picks") or [])[:top_n]:
        sym = str(pick.get("symbol") or "").upper()
        if not sym:
            continue
        try:
            profile = _single_company_profile(sym)
            compare = _track_leader_compare(sym, profile or {})
            if compare.get("available"):
                done += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {"prewarmed": done, "failed": failed, "top_n": top_n}


@app.get("/predictions/scorecard", dependencies=[Depends(require_auth)])
async def get_prediction_scorecard(
    force: bool = Query(False),
    board_only: bool = Query(False),
    mode: str = Query("live"),
):
    """Forward-verification track record: predicted vs realized + scoring.

    ``mode``: 'live' (real walk-forward, the honest forward record), 'backfill'
    (in-sample as-of replay used to seed the ledger), or 'any' (both).

    Passive page read: scoped cache-only (big premise — no external fetch on a
    page read; the open-position MTM serves from cache). Outcome resolution and
    fresh fetches only happen on the daily batch or the manual /resolve trigger.
    """
    from prediction_ledger_service import prediction_scorecard

    mode_arg = None if mode == "any" else mode

    def _scoped():
        with external_data_scope(False):
            return prediction_scorecard(force=force, board_only=board_only, mode=mode_arg)

    return _json_safe(await asyncio.to_thread(_scoped))


@app.post("/predictions/backfill", dependencies=[Depends(require_auth)])
async def post_predictions_backfill(
    lookback_trading_days: int = Query(30, ge=5, le=90),
    top_n: int = Query(30, ge=1, le=100),
    max_symbols: int = Query(200, ge=10, le=600),
):
    """Manual trigger: seed the ledger with an as-of historical walk-forward
    replay (mode='backfill', in-sample vs the active curve), then resolve the
    matured cohorts. Heavy (re-scores every candidate per trading day +
    per-symbol fetches), so it runs in the BACKGROUND and returns immediately;
    poll GET /predictions/backfill/status. A manual/explicit trigger, so
    external fetches are allowed — but cache is used first."""
    status = cache_get(_BACKFILL_STATUS_KEY) or {}
    if status.get("status") == "running":
        return _json_safe({"status": "running", **status})

    def _run():
        from prediction_ledger_service import (
            backfill_predictions, prediction_scorecard, resolve_predictions,
        )
        started = datetime.now(timezone.utc).isoformat()
        cache_set(_BACKFILL_STATUS_KEY, {"status": "running", "started_at": started,
                                         "lookback_trading_days": lookback_trading_days}, ttl_seconds=3600)
        try:
            bf = backfill_predictions(lookback_trading_days, top_n=top_n, max_symbols=max_symbols)
            res = resolve_predictions()
            sc = prediction_scorecard(force=True, mode="backfill")
            cache_set(_BACKFILL_STATUS_KEY, {
                "status": "done", "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "backfill": bf, "resolve": res,
                "n_resolved": sc.get("n_resolved"), "available": sc.get("available"),
            }, ttl_seconds=3600)
        except Exception as exc:
            cache_set(_BACKFILL_STATUS_KEY, {
                "status": "failed", "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc)[-500:],
            }, ttl_seconds=3600)

    threading.Thread(target=_run, daemon=True).start()
    return _json_safe({"status": "started", "lookback_trading_days": lookback_trading_days,
                       "note": "后台回放中，约1-3分钟；完成后到「历史回放」标签查看。"})


@app.get("/predictions/backfill/status", dependencies=[Depends(require_auth)])
async def get_predictions_backfill_status():
    """Progress of the most recent background backfill job."""
    return _json_safe(cache_get(_BACKFILL_STATUS_KEY) or {"status": "idle"})


@app.post("/predictions/log", dependencies=[Depends(require_auth)])
async def post_predictions_log(top_n: int = Query(30, ge=1, le=100), log_all: bool = Query(True)):
    """Log today's full board candidate slice; top_n is only the board-pick tag."""
    from prediction_ledger_service import log_predictions

    return _json_safe(log_predictions(top_n, log_all_candidates=log_all))


@app.post("/predictions/resolve", dependencies=[Depends(require_auth)])
async def post_predictions_resolve():
    """Resolve matured predictions against realized forward returns."""
    from prediction_ledger_service import prediction_scorecard, resolve_predictions

    res = resolve_predictions()
    return _json_safe({"resolve": res, "scorecard": prediction_scorecard(force=True)})


@app.get("/track-leader/{symbol}", dependencies=[Depends(require_auth)])
async def get_track_leader(symbol: str):
    """On-demand 赛道龙头对比 for one symbol (board row expand). Uses cached
    company profile (7d) + leader compare (24h), so repeat opens are instant."""
    with external_data_scope(False):
        safe = _safe_event_ticker(symbol)
        profile = _single_company_profile(safe)
        compare = _track_leader_compare(safe, profile or {})
        return _json_safe({
            "symbol": safe,
            "track_cn": (profile or {}).get("track_cn") or (profile or {}).get("segment_cn"),
            "leader_compare": compare,
        })


@app.get("/portfolio/view", dependencies=[Depends(require_auth)])
async def get_portfolio_view():
    """Manual holdings + cash scored into ADD/HOLD/TRIM/CLOSE decisions."""
    from portfolio_advisor_service import compute_portfolio_view

    with external_data_scope(False):
        return _json_safe(compute_portfolio_view())


@app.post("/portfolio/holding", dependencies=[Depends(require_auth)])
async def upsert_portfolio_holding(payload: PortfolioHoldingRequest):
    """Insert or update one holding, then return the refreshed portfolio view."""
    from portfolio_advisor_service import compute_portfolio_view

    portfolio_holding_upsert(
        symbol=payload.symbol,
        shares=payload.shares,
        avg_cost=payload.avg_cost,
        note=payload.note,
    )
    return _json_safe(compute_portfolio_view())


@app.delete("/portfolio/holding/{symbol}", dependencies=[Depends(require_auth)])
async def delete_portfolio_holding(symbol: str):
    """Remove one holding, then return the refreshed portfolio view."""
    from portfolio_advisor_service import compute_portfolio_view

    portfolio_holding_delete(symbol)
    return _json_safe(compute_portfolio_view())


@app.post("/portfolio/account", dependencies=[Depends(require_auth)])
async def set_portfolio_account(payload: PortfolioAccountRequest):
    """Set available cash, then return the refreshed portfolio view."""
    from portfolio_advisor_service import compute_portfolio_view

    portfolio_account_set(payload.available_cash, payload.currency)
    return _json_safe(compute_portfolio_view())


@app.get("/portfolio/account", dependencies=[Depends(require_auth)])
async def get_portfolio_account():
    """Return just the account cash row (lightweight)."""
    return _json_safe(portfolio_account_get())


@app.get("/signal-calibration/status", dependencies=[Depends(require_auth)])
async def signal_calibration_status():
    """Return active calibration curves, reliability buckets, and event counts."""
    curves = signal_calibration_list(include_curve=True)
    active = [row for row in curves if row.get("is_active")]
    summary = []
    for row in active:
        curve = row.get("curve") or {}
        summary.append({
            "calibration_id": row.get("calibration_id"),
            "signal_type": row.get("signal_type"),
            "horizon_days": row.get("horizon_days"),
            "source": row.get("source"),
            "universe_set": row.get("universe_set"),
            "generated_at": row.get("generated_at"),
            "cost_bps": row.get("cost_bps"),
            "event_count": row.get("event_count"),
            "cluster_count": row.get("cluster_count"),
            "global": curve.get("global"),
            "buckets": curve.get("buckets"),
        })
    return _json_safe({
        "mode": "shadow" if os.getenv("ENABLE_CALIBRATED_RANKING", "0").lower() in {"0", "false", "no"} else "active",
        "active_curves": summary,
        "all_curves": [{k: v for k, v in row.items() if k != "curve"} for row in curves],
        "events": signal_events_count(),
    })


@app.post("/signal-calibration/resolve", dependencies=[Depends(require_auth)])
async def signal_calibration_resolve(limit: int = 500, history_period: str = "2y"):
    """Resolve pending logged signal events into realized forward returns."""
    from scripts.resolve_signal_events import resolve_pending_events

    return _json_safe(resolve_pending_events(limit=limit, history_period=history_period))


@app.post("/signal-calibration/collect", dependencies=[Depends(require_auth)])
async def signal_calibration_collect(universe: str = "spx", max_symbols: int = 0):
    """Run one out-of-sample collection cycle: log today's signals + resolve matured ones."""
    from scripts.log_daily_signals import log_daily_signals
    from scripts.resolve_signal_events import resolve_pending_events

    logged = log_daily_signals(universe, max_symbols=max_symbols)
    resolved = resolve_pending_events(limit=5000)
    iv_captured = _capture_candidate_iv()
    return _json_safe({"logged": logged, "resolved": resolved, "iv_captured": iv_captured})


_CALIB_REBUILD: Dict[str, Any] = {"status": "idle", "started_at": None, "finished_at": None, "message": ""}
_CALIB_REBUILD_LOCK = threading.Lock()


def _run_calibration_rebuild(universe: str, period: str) -> None:
    cmd = [
        _sys.executable, str(AGENT_DIR / "scripts" / "build_signal_calibration.py"),
        "--universes", universe, "--period", period, "--horizons", "5,10",
        "--signals", "launch,daily_tunnel,launch_contrarian,daily_tunnel_contrarian",
    ]
    try:
        proc = subprocess.run(cmd, cwd=str(AGENT_DIR.parent), capture_output=True, text=True, timeout=1800)
        ok = proc.returncode == 0
        msg = "重建完成" if ok else (proc.stderr or proc.stdout or "")[-400:]
        with _CALIB_REBUILD_LOCK:
            _CALIB_REBUILD.update({"status": "completed" if ok else "error", "finished_at": datetime.now().isoformat(), "message": msg})
    except Exception as exc:
        with _CALIB_REBUILD_LOCK:
            _CALIB_REBUILD.update({"status": "error", "finished_at": datetime.now().isoformat(), "message": str(exc)[-400:]})


@app.get("/signal-calibration/rebuild-status", dependencies=[Depends(require_auth)])
async def signal_calibration_rebuild_status():
    """Status of the manual calibration rebuild job."""
    with _CALIB_REBUILD_LOCK:
        return dict(_CALIB_REBUILD)


@app.post("/signal-calibration/rebuild", dependencies=[Depends(require_auth)])
async def signal_calibration_rebuild(universe: str = "spx", period: str = "3y"):
    """Rebuild the active calibration curves (replay) in the background."""
    with _CALIB_REBUILD_LOCK:
        if _CALIB_REBUILD["status"] in {"running", "queued"}:
            return dict(_CALIB_REBUILD)
        _CALIB_REBUILD.update({"status": "running", "started_at": datetime.now().isoformat(), "finished_at": None, "message": f"重建 {universe} {period} ..."})
    threading.Thread(target=_run_calibration_rebuild, args=(universe, period), daemon=True).start()
    with _CALIB_REBUILD_LOCK:
        return dict(_CALIB_REBUILD)


@app.get("/home-dashboard/snapshot", dependencies=[Depends(require_auth)])
async def get_home_dashboard_snapshot():
    """Return the latest persisted home dashboard snapshot, if any."""
    snapshot = latest_home_dashboard_snapshot()
    return {"available": bool(snapshot), "snapshot": snapshot}


@app.post("/home-dashboard/snapshot", dependencies=[Depends(require_auth)])
async def post_home_dashboard_snapshot(payload: HomeDashboardSnapshotRequest):
    """Persist the current home dashboard aggregate into SQLite."""
    snapshot_id = save_home_dashboard_snapshot(payload.payload, source=payload.source)
    return {"snapshot_id": snapshot_id, "status": "saved"}


@app.get("/macro-panic-regime", dependencies=[Depends(require_auth)])
async def macro_panic_regime(force_refresh: bool = Query(False)):
    """Return current VIX or VIX-proxy macro panic regime."""
    with external_data_scope(bool(force_refresh)):
        return get_vix_regime(force_refresh=force_refresh)


@app.get("/portfolio-timing/gate", dependencies=[Depends(require_auth)])
async def portfolio_timing_gate_status(force_refresh: bool = Query(False)):
    """Return account-level gross exposure gate for R3 portfolio timing."""
    with external_data_scope(bool(force_refresh)):
        return _json_safe(portfolio_timing_gate(force_refresh=force_refresh))


@app.get("/stock-panic/{symbol}", dependencies=[Depends(require_auth)])
async def stock_panic_detail(symbol: str, universe: str = Query("")):
    """Return per-symbol Stock VIX Proxy details."""
    with external_data_scope(False):
        safe_symbol = _safe_event_ticker(symbol)
        opportunity = None
        if universe:
            observation = _stock_signal_observation_pool(universe, limit=500)
            opportunity = {item["ticker"]: item for item in observation}.get(safe_symbol)
        if opportunity is None:
            opportunity = _latest_stock_opportunity(safe_symbol)
        event = _latest_event_signals.get(safe_symbol)
        macro = get_vix_regime()
        return stock_panic_proxy(safe_symbol, opportunity, macro_regime=macro, event=event)


@app.get("/macro-panic-radar-tool", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def macro_panic_radar_tool():
    """Standalone macro panic radar page embedded by the frontend."""
    return """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>宏观恐慌雷达</title>
  <style>
    :root{color-scheme:dark;--bg:#09131b;--panel:#101e29;--line:#263947;--muted:#91a5b2;--text:#eef5f7;--good:#6dd7b2;--wait:#e9b85d;--bad:#f07b78;--gold:#d5ad5c}
    *{box-sizing:border-box}body{margin:0;padding:24px;background:var(--bg);color:var(--text);font:14px/1.55 Inter,"Microsoft YaHei",sans-serif}
    h1{margin:0 0 6px;font-size:24px}.muted{color:var(--muted)}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}button{min-height:38px;padding:8px 13px;border:1px solid var(--line);border-radius:6px;background:#16635e;color:var(--text);font-weight:700;cursor:pointer}.secondary{background:#152632}
    .grid{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:12px}.card{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px}.card span{display:block;color:var(--muted);font-size:12px}.card b{display:block;margin-top:4px;font-size:18px}.good{color:var(--good)}.wait{color:var(--wait)}.bad{color:var(--bad)}
    .section{margin-top:14px;border:1px solid var(--line);border-radius:8px;background:#0f202b;padding:14px}.section h2{margin:0 0 8px;font-size:16px}.pill{display:inline-block;padding:3px 8px;border:1px solid var(--line);border-radius:999px;margin:2px 4px 2px 0;color:#cbdde4}.warn{border-color:#71454a;color:#ffb7b4;background:#28161a}
    pre{white-space:pre-wrap;word-break:break-word;color:#bfd4dc;background:#0b1821;border:1px solid #20333f;border-radius:6px;padding:10px;max-height:260px;overflow:auto}
    @media(max-width:900px){body{padding:14px}.grid{grid-template-columns:1fr 1fr}}@media(max-width:560px){.grid{grid-template-columns:1fr}}
  </style>
</head>
<body>
  <h1>宏观恐慌雷达</h1>
  <div class="muted">真实 VIX 优先；不可用时降级为 VIX Proxy。该模块只做研究环境判断，不构成确定性买入建议。</div>
  <div class="toolbar"><button id="refresh">刷新宏观状态</button><button class="secondary" onclick="location.href='/signal-dashboard'">回到三维总览</button></div>
  <div class="grid" id="cards"></div>
  <div class="section"><h2>模式说明</h2><div id="mode"></div></div>
  <div class="section"><h2>系统性危机过滤</h2><div id="crisis"></div></div>
  <div class="section"><h2>计算口径</h2><div id="method" class="muted"></div><pre id="detail"></pre></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function tone(d){if(d.systemic_crisis||d.mode==="crisis_guard")return"bad";if(d.mode==="panic_reclaim"||d.mode==="panic_watch")return"wait";return"good"}
function pct(v){return v==null?"--":(Number(v)*100).toFixed(1)+"%"}
function render(d){
  const cls=tone(d);
  $("cards").innerHTML=[
    ["指标",`${esc(d.display_name||"VIX")} ${d.value==null?"--":Number(d.value).toFixed(2)}`,cls],
    ["状态",esc(d.label_cn||"--"),cls],
    ["模式",esc(d.mode_cn||"--"),cls],
    ["恐慌分",pct(d.panic_score),cls],
  ].map(x=>`<div class="card"><span>${x[0]}</span><b class="${x[2]}">${x[1]}</b></div>`).join("");
  $("mode").innerHTML=`<span class="pill">${esc(d.source_type==="real_vix"?"真实 VIX":"代理指标")}</span><span class="pill">${esc(d.source||"--")}</span><span class="pill">${esc(d.cache_hit?"缓存命中":"刚刚刷新")}</span>`;
  const flags=d.systemic_risk_flags||[];
  $("crisis").innerHTML=d.systemic_crisis?`<span class="pill warn">系统性危机过滤已触发</span>${flags.map(f=>`<span class="pill warn">${esc(f)}</span>`).join("")}`:`<span class="pill">未触发危机过滤</span>${flags.map(f=>`<span class="pill">${esc(f)}</span>`).join("")}`;
  $("method").textContent=d.method_note||"";
  $("detail").textContent=JSON.stringify({warnings:d.warnings||[],proxy_detail:d.proxy_detail||{},as_of:d.as_of},null,2);
}
async function load(force=false){$("cards").innerHTML='<div class="card"><b>加载中...</b></div>';let r=await fetch(`/macro-panic-regime?force_refresh=${force?"true":"false"}`),d=await r.json();if(!r.ok)throw new Error(d.detail||"加载失败");render(d)}
$("refresh").onclick=()=>load(true).catch(e=>$("cards").innerHTML=`<div class="card"><b class="bad">${esc(e.message)}</b></div>`);
load(false).catch(e=>$("cards").innerHTML=`<div class="card"><b class="bad">${esc(e.message)}</b></div>`);
</script>
</body>
</html>
"""


@app.post("/research-signal-hub/run", dependencies=[Depends(require_auth)])
async def start_research_signal_hub_run(payload: ResearchSignalHubRunRequest):
    """Start the one-click three-layer equity research workflow."""
    supported = {item["universe"] for item in _STOCK_SIGNAL_UNIVERSE_CATALOG}
    universe = payload.universe.strip().lower()
    if universe not in supported:
        raise HTTPException(status_code=400, detail=f"不支持的股票池：{universe}")
    with _research_signal_hub_lock:
        for job_id, job in _research_signal_hub_jobs.items():
            if job.get("universe") == universe and job.get("status") in {"queued", "running"}:
                return {"job_id": job_id, **job}
        job_id = f"research_hub_{uuid.uuid4().hex[:10]}"
        _research_signal_hub_jobs[job_id] = {
            "job_id": job_id,
            "universe": universe,
            "status": "running",
            "phase": "queued",
            "message": "三维研究 Agent 已启动，准备执行第一层机会筛选。",
            "progress": 0.0,
            "started_at": datetime.utcnow().isoformat() + "Z",
        }
    threading.Thread(target=_run_research_signal_hub_job, args=(job_id, payload), daemon=True).start()
    return _research_signal_hub_jobs[job_id]


@app.get("/research-signal-hub/run/{job_id}", dependencies=[Depends(require_auth)])
async def get_research_signal_hub_run(job_id: str):
    """Return progress for a three-layer workflow run."""
    _validate_path_param(job_id, "job_id")
    job = _research_signal_hub_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"三维分析任务不存在：{job_id}")
    return job


def _auto_scan_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(_AUTO_SCAN_TZ)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _auto_scan_scheduled_at(now: Optional[datetime] = None) -> datetime:
    tz = _auto_scan_timezone()
    current = now or datetime.now(tz)
    try:
        hour_raw, minute_raw = _AUTO_SCAN_TIME.split(":", 1)
        hour = max(0, min(23, int(hour_raw)))
        minute = max(0, min(59, int(minute_raw)))
    except Exception:
        hour, minute = 9, 30
    return current.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _auto_scan_today_key(now: Optional[datetime] = None) -> str:
    tz = _auto_scan_timezone()
    current = now or datetime.now(tz)
    return current.date().isoformat()


def _auto_scan_last_record() -> Dict[str, Any]:
    record = cache_get(_AUTO_SCAN_STATE_KEY)
    if not isinstance(record, dict):
        return {}
    if record.get("status") == "completed" and record.get("finalize_timed_out"):
        return {
            **record,
            "status": "failed",
            "error": "legacy scan finalize timed out; completion was not verified",
        }
    return record


def _auto_scan_universe_ids(explicit: Optional[List[str]] = None) -> List[str]:
    supported = {str(item.get("universe") or "") for item in _STOCK_SIGNAL_UNIVERSE_CATALOG}
    if explicit:
        values = [str(item or "").strip().lower() for item in explicit]
        universes = [item for item in values if item in supported]
    else:
        universes = [str(item.get("universe") or "") for item in _ranked_research_universes()]
        universes = [item for item in universes if item in supported]
    if _AUTO_SCAN_MAX_UNIVERSES > 0:
        universes = universes[:_AUTO_SCAN_MAX_UNIVERSES]
    return list(dict.fromkeys([item for item in universes if item]))


def _auto_scan_state() -> Dict[str, Any]:
    with _AUTO_SCAN_LOCK:
        state = dict(_AUTO_SCAN_JOB)
    now = datetime.now(_auto_scan_timezone())
    state.update({
        "enabled": _AUTO_SCAN_ENABLED,
        "schedule_time": _AUTO_SCAN_TIME,
        "timezone": _AUTO_SCAN_TZ,
        "today": _auto_scan_today_key(now),
        "scheduled_at": _auto_scan_scheduled_at(now).isoformat(),
        "refresh_evidence": _AUTO_SCAN_REFRESH_EVIDENCE,
        "child_timeout_seconds": _AUTO_SCAN_CHILD_TIMEOUT_SECONDS,
        "last_record": _auto_scan_last_record(),
        "daily_news": cache_get("daily_three_layer_auto:news"),
        "daily_evidence": cache_get("daily_three_layer_auto:evidence"),
    })
    try:
        from market_calendar import market_status

        ms = market_status(now)
        ms["last_synced_session"] = cache_get("market_calendar:last_synced_session")
        ms["price_sync_pending"] = ms["most_recent_session"] != ms.get("last_synced_session")
        state["market"] = ms
    except Exception:
        pass
    return _json_safe(state)


def _daily_active_worker_ids():
    with _AUTO_SCAN_LOCK:
        for key, thread in list(_DAILY_ACTIVE_WORKERS.items()):
            if not thread.is_alive():
                _DAILY_ACTIVE_WORKERS.pop(key, None)
        return list(_DAILY_ACTIVE_WORKERS)


def _home_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _home_normalize_score(value: Any) -> float:
    number = _home_number(value, 0.0)
    if -1.0 <= number <= 1.0:
        number *= 100.0
    return max(0.0, min(100.0, number))


def _home_row_blocked(row: Dict[str, Any]) -> bool:
    open_status = str(row.get("open_risk_status") or "").upper()
    exit_action = str(row.get("exit_action") or "").upper()
    event_status = str(row.get("event_radar_status") or "")
    return (
        bool(row.get("macro_systemic_crisis"))
        or "BLOCK" in open_status
        or "EXIT" in exit_action
        or "回避" in event_status
        or "告警" in event_status
    )


def _home_overnight_score(row: Dict[str, Any]) -> float:
    score = 0.0
    score += _home_normalize_score(row.get("overall_sort_score")) * 0.34
    score += _home_normalize_score(row.get("unified_probability")) * 0.22
    score += _home_normalize_score(row.get("historical_win_rate")) * 0.12
    score += _home_normalize_score(row.get("stock_signal_score")) * 0.10
    score += _home_normalize_score(row.get("launch_signal_score")) * 0.08
    score += _home_normalize_score(row.get("daily_tunnel_score")) * 0.07
    score += _home_normalize_score(row.get("pullback_rejection_score")) * 0.04
    score += _home_normalize_score(row.get("pullback_confirmation_score")) * 0.05
    confirmation = str(row.get("pullback_confirmation_status") or "")
    rejection = str(row.get("pullback_rejection_status") or "")
    if confirmation == "STRONG_CONFIRMED":
        score += 9
    elif confirmation == "CONFIRMED":
        score += 6
    elif confirmation == "WEAK_CONFIRMED":
        score += 2
    if rejection == "STRONG_WATCH":
        score += 3
    elif rejection == "WATCH":
        score += 1.5
    gex_level = str(row.get("gex_level") or "").upper()
    gex_regime = str(row.get("gex_regime") or "").lower()
    if gex_level == "HIGH" and "negative" in gex_regime:
        score -= 8
    elif gex_level == "HIGH":
        score -= 4
    if _home_row_blocked(row):
        score -= 28
    if len(row.get("risk_flags") or []) >= 3:
        score -= 3
    return max(0.0, min(100.0, score))


def _daily_min_price_coverage() -> float:
    try:
        return max(0.01, min(1.0, float(os.getenv("DAILY_MIN_PRICE_COVERAGE", "0.95"))))
    except ValueError:
        return 0.95


def _daily_report_price_coverage(universe_ids: Optional[List[str]], session: str) -> Dict[str, Any]:
    """Check the existing daily cache for the symbols in completed pool reports."""
    from market_data_service import _read_daily_cache

    report_index = _stock_signal_report_index()
    symbols: set[str] = set()
    missing_reports: List[str] = []
    for universe in universe_ids or []:
        if universe == "watchlist":
            from app_database import user_watchlist_list
            symbols.update(str(item.get("symbol") or "").upper() for item in user_watchlist_list(include_disabled=False))
            continue
        selected = report_index.get(universe)
        if not selected or not _stock_signal_report_has_results(selected[1]):
            missing_reports.append(universe)
            continue
        symbols.update(
            str(item.get("ticker") or "").upper()
            for item in _stock_signal_filter_results(selected[1], universe)
        )
    symbols.discard("")
    current = 0
    stale: List[str] = []
    for symbol in sorted(symbols):
        frame = _read_daily_cache(symbol)
        if not frame.empty and str(frame.index.max().date()) >= session:
            current += 1
        else:
            stale.append(symbol)
    return {
        "total": len(symbols), "current": current,
        "ratio": current / len(symbols) if symbols else 0.0,
        "stale_symbols": stale[:20], "missing_reports": missing_reports,
    }


def _build_home_dashboard_snapshot_payload(
    universe_ids: Optional[List[str]] = None,
    required_price_session: str = "",
    allow_partial_price_evidence: bool = False,
) -> Dict[str, Any]:
    """Build the unified home dashboard payload from latest completed hub evidence."""
    universes = _ranked_research_universes()
    portfolio_timing = portfolio_timing_gate()
    peer_relay_audit = summarize_peer_earnings_history(RUNS_DIR / "_peer_earnings_signal_history.jsonl", limit=300)
    selected = set(universe_ids or [])
    if selected:
        universes = [item for item in universes if str(item.get("universe") or "") in selected]
    row_map: Dict[str, Dict[str, Any]] = {}
    universe_top_inputs: List[Dict[str, Any]] = []
    loaded = 0
    failed = 0
    first_hub: Optional[Dict[str, Any]] = None
    try:
        market_status = data_source_status()
    except Exception:
        market_status = {
            "ohlcv_priority": ["cache", "yfinance"],
            "fundamentals_priority": ["cache", "yfinance"],
            "options_priority": ["cboe", "yfinance"],
            "cache_root": str(AGENT_DIR / "data_cache" / "market_data"),
        }
    for universe in universes:
        universe_id = str(universe.get("universe") or "")
        if not universe_id:
            continue
        universe_label = str(universe.get("label") or universe.get("name") or universe_id)
        try:
            raw_rows = _research_signal_contexts_for_universe(universe_id, limit=30, portfolio_timing=portfolio_timing)
            loaded += 1
        except Exception as exc:
            failed += 1
            console.log(f"daily auto aggregate failed for {universe_id}: {str(exc)[-500:]}")
            continue
        if first_hub is None:
            first_hub = {
                "universe": universe_id,
                "snapshot": {
                    "generated_at": datetime.utcnow().isoformat() + "Z",
                    "candidate_count": len(raw_rows),
                    "latest_run_status": "completed",
                },
            }
        for index, raw in enumerate(raw_rows[:3], start=1):
            symbol = str(raw.get("symbol") or "").upper()
            if not symbol:
                continue
            compact = _compact_research_context(raw)
            universe_top_inputs.append({
                "meta": {
                    "universe_id": universe_id,
                    "universe_label": universe_label,
                    "rank": index,
                    "score": float((compact.get("overall_sort_score") or compact.get("unified_probability") or 0) or 0),
                    "symbol": symbol,
                },
                "payload": {
                    "symbol": symbol,
                    "source_universe_ids": [universe_id],
                    "source_pools": compact.get("source_pools") or [],
                    "pullback_rejection_status": compact.get("pullback_rejection_status") or None,
                    "pullback_confirmation_status": compact.get("pullback_confirmation_status") or None,
                },
            })
        for raw in raw_rows:
            symbol = str(raw.get("symbol") or "").upper()
            if not symbol:
                continue
            existing = row_map.get(symbol)
            if existing is None or _research_context_sort_key(raw) < _research_context_sort_key(existing):
                raw = dict(raw)
                raw["_source_universe_ids"] = [universe_id]
                raw["_source_universe_labels"] = [universe_label]
                row_map[symbol] = raw
            else:
                ids = list(row_map[symbol].get("_source_universe_ids") or [])
                labels = list(row_map[symbol].get("_source_universe_labels") or [])
                if universe_id not in ids:
                    ids.append(universe_id)
                if universe_label not in labels:
                    labels.append(universe_label)
                row_map[symbol]["_source_universe_ids"] = ids
                row_map[symbol]["_source_universe_labels"] = labels
    sorted_raw_rows = sorted(row_map.values(), key=_research_context_sort_key)
    compact_rows: List[Dict[str, Any]] = []
    raw_by_symbol = {str(raw.get("symbol") or "").upper(): raw for raw in sorted_raw_rows}
    for raw in sorted_raw_rows:
        compact = _compact_research_context(raw)
        compact["source_universe_ids"] = raw.get("_source_universe_ids") or []
        compact["source_universe_labels"] = raw.get("_source_universe_labels") or []
        compact_rows.append(compact)

    stale: List[str] = []
    if required_price_session:
        stale = [
            str(row.get("symbol") or "") for row in compact_rows
            if str(row.get("price_as_of") or "") < required_price_session
        ]
        coverage = (len(compact_rows) - len(stale)) / len(compact_rows) if compact_rows else 0.0
        if coverage < _daily_min_price_coverage() and (
            not allow_partial_price_evidence or len(stale) == len(compact_rows)
        ):
            raise RuntimeError(
                f"current-session snapshot blocked: {len(stale)}/{len(compact_rows)} candidates "
                f"lack {required_price_session} price evidence; examples: {stale[:8]}"
            )
        stale_set = set(stale)
        compact_rows = [row for row in compact_rows if str(row.get("symbol") or "") not in stale_set]
        universe_top_inputs = [item for item in universe_top_inputs if item["meta"]["symbol"] not in stale_set]

    final_top_rows = sorted(compact_rows, key=_home_overnight_score, reverse=True)[:12]
    final_review_contexts = [
        raw_by_symbol[str(row.get("symbol") or "").upper()]
        for row in final_top_rows
        if str(row.get("symbol") or "").upper() in raw_by_symbol
    ]
    if final_review_contexts:
        try:
            final_reviews = review_top_contexts(
                final_review_contexts, limit=len(final_review_contexts), cache_only=not external_data_allowed(),
            )
            for row in compact_rows:
                symbol = str(row.get("symbol") or "").upper()
                if symbol in final_reviews:
                    row["llm_review"] = final_reviews[symbol]
        except Exception as exc:
            console.log(f"daily auto final Top12 DeepSeek review failed: {str(exc)[-500:]}")

    overnight_alpha = None
    universe_alpha_top: List[Dict[str, Any]] = []
    alpha_rows = [
        {
            "symbol": row.get("symbol") or "",
            "source_universe_ids": row.get("source_universe_ids") or [],
            "source_pools": row.get("source_pools") or [],
            "pullback_rejection_status": row.get("pullback_rejection_status") or None,
            "pullback_confirmation_status": row.get("pullback_confirmation_status") or None,
        }
        for row in compact_rows[:12]
        if row.get("symbol")
    ]
    if alpha_rows:
        try:
            overnight_alpha = validate_overnight_alpha_summary(alpha_rows, period="2y", min_edge=0.001, limit=12)
        except Exception as exc:
            overnight_alpha = {"available": False, "reason": f"overnight alpha failed: {str(exc)[-300:]}"}
    top_inputs = universe_top_inputs[:30]
    if top_inputs:
        try:
            alpha = validate_overnight_alpha_summary(
                [item["payload"] for item in top_inputs],
                period="2y",
                min_edge=0.001,
                limit=len(top_inputs),
            )
            results = alpha.get("results") or []
            for index, item in enumerate(top_inputs):
                universe_alpha_top.append({**item["meta"], "alpha": results[index] if index < len(results) else None})
        except Exception as exc:
            universe_alpha_top = [{"error": f"per-universe alpha failed: {str(exc)[-300:]}"}]
    return _json_safe({
        "rows": compact_rows,
        "required_price_session": required_price_session or None,
        "price_coverage": round(len(compact_rows) / (len(compact_rows) + len(stale)), 4) if compact_rows else 0.0,
        "excluded_stale_count": len(stale),
        "excluded_stale_symbols": stale[:20],
        "partial_price_evidence": bool(stale),
        "portfolio_timing": portfolio_timing,
        "universe_source_audit": {
            "universe_count": len(universes),
            "fallback_count": sum(1 for item in universes if (item.get("recent_win") or {}).get("source_is_fallback")),
            "archive_or_live_count": sum(1 for item in universes if (item.get("recent_win") or {}).get("source_kind") in {"live", "archive"}),
            "items": [
                {
                    "universe": item.get("universe"),
                    "label": item.get("label"),
                    "source_kind": (item.get("recent_win") or {}).get("source_kind"),
                    "source_is_fallback": (item.get("recent_win") or {}).get("source_is_fallback"),
                    "source_audit_note": (item.get("recent_win") or {}).get("source_audit_note"),
                }
                for item in universes
            ],
        },
        "peer_relay_audit": peer_relay_audit,
        "overnight_alpha": overnight_alpha,
        "universe_alpha_top": universe_alpha_top,
        "loaded_universe_count": loaded,
        "failed_universe_count": failed,
        "macro_hub": first_hub,
        "market_status": market_status,
        "last_loaded_at": datetime.now(_auto_scan_timezone()).strftime("%Y-%m-%d %H:%M:%S"),
    })


def _daily_sync_prices(universe_ids, out, *, allow_partial=False):
    try:
        if allow_partial:
            from market_calendar import most_recent_session
            attempt = cache_get("daily_three_layer_auto:price_source_pause")
            if (isinstance(attempt, dict) and attempt.get("session") == most_recent_session().isoformat()
                    and time.time() - float(attempt.get("failed_at") or 0) < 1800):
                raise RuntimeError("行情补洞来源处于冷却期，复用当前缓存")
        return _daily_sync_prices_strict(universe_ids, out)
    except Exception as exc:
        if not allow_partial:
            raise
        from market_calendar import most_recent_session
        session = most_recent_session().isoformat()
        pause = cache_get("daily_three_layer_auto:price_source_pause")
        if not isinstance(pause, dict) or pause.get("session") != session or time.time() - float(pause.get("failed_at") or 0) >= 1800:
            cache_set("daily_three_layer_auto:price_source_pause", {"session": session, "failed_at": time.time()})
        coverage = _daily_report_price_coverage(universe_ids, session)
        out["cached_price_coverage"] = coverage
        if not coverage.get("current"):
            raise RuntimeError(f"没有 {session} 的可用行情；已快速结束，旧榜单保留") from exc
        out["data_warnings"] = [f"行情同步未完整完成，仅继续处理 {coverage['current']}/{coverage['total']} 只有效缓存标的"]
        out["market_data_fresh"] = False
        console.log(f"[daily-degraded] price coverage {coverage['current']}/{coverage['total']} for {session}")
        return session


def _daily_sync_prices_strict(universe_ids, out):
    """Check and sync prices before spending time on per-pool scans."""
    timings = out.setdefault("timings", {})

    def _timed(label, fn):
        started = time.monotonic()
        try:
            return fn()
        finally:
            timings[label] = round(time.monotonic() - started, 2)
            console.log(f"[daily-timing] {label}: {timings[label]}s")

    # 1) Sanctioned external bulk sync, gated by the US trading calendar.
    market_data_fresh = True
    session = ""
    try:
        from market_calendar import most_recent_session

        session = most_recent_session().isoformat()
        market_data_fresh = session != cache_get("market_calendar:last_synced_session")
    except Exception as exc:
        raise RuntimeError(f"trading-calendar check failed: {str(exc)[-200:]}") from exc
    out["market_data_fresh"] = market_data_fresh
    if market_data_fresh:
        coverage = _timed("cached_price_coverage", lambda: _daily_report_price_coverage(universe_ids, session))
        out["cached_price_coverage"] = coverage
        if not coverage["missing_reports"] and coverage["ratio"] >= _daily_min_price_coverage():
            console.log(f"daily price cache covers {coverage['current']}/{coverage['total']} report symbols for {session}; skip grouped-daily request")
            cache_set("market_calendar:last_synced_session", session)
            market_data_fresh = False
            out["market_data_fresh"] = False
    if market_data_fresh:
        def _ingest():
            from scripts.ingest_grouped_daily import ingest_recent_grouped_daily
            ing = ingest_recent_grouped_daily(
                "spx", days=1, target_session=session, universe_ids=universe_ids,
            )
            console.log(f"grouped-daily ingest (session {session}): {ing}")
            coverage = _daily_report_price_coverage(universe_ids, session)
            out["cached_price_coverage"] = coverage
            if (not ing.get("ok") or not ing.get("symbols_written")
                    or str(ing.get("latest_data_date") or "") < session
                    or not ing.get("latest_session_symbols")
                    or (coverage["total"] and coverage["ratio"] < _daily_min_price_coverage())):
                # Reuse the gap repair with a zero-dollar default; no retry storm.
                repair = _daily_databento_gap_repair(session)
                out["price_repair"] = repair
                coverage = _daily_report_price_coverage(universe_ids, session)
                out["cached_price_coverage"] = coverage
                if (not repair.get("ok") or coverage["missing_reports"]
                        or coverage["ratio"] < _daily_min_price_coverage()):
                    raise RuntimeError(f"grouped-daily ingest did not cover {session}: {ing}; "
                                       f"gap repair: {repair}; cache coverage {coverage['ratio']:.1%}")
            cache_set("market_calendar:last_synced_session", session)
        _timed("grouped_daily_ingest", _ingest)
    else:
        console.log(f"market closed / no new session since {session}: skip price sync (news only)")
    return session

def _daily_databento_gap_repair(session):
    if not os.getenv("DATABENTO_API_KEY"):
        return {"ok": False, "error": "Databento key unavailable"}
    budget_key = "daily_three_layer_auto:price_repair_budget"
    today = _auto_scan_today_key()
    try:
        import sys
        cap = max(0.0, float(os.getenv("VIBE_DAILY_PRICE_REPAIR_MAX_COST_USD", "0")))
        if not math.isfinite(cap):
            raise ValueError("invalid cost cap")
        with _AUTO_SCAN_LOCK:
            budget = cache_get(budget_key) or {}
            committed = float(budget.get("committed_usd") or 0) if budget.get("date") == today else 0.0
            remaining = max(0.0, cap - committed)
            if cap > 0 and remaining <= 0:
                return {"ok": False, "error": "daily price repair budget exhausted", "max_cost_usd": cap}
            # Reserve before execution. Unknown failures retain the reservation,
            # since the vendor may have charged before the cache write failed.
            cache_set(budget_key, {"date": today, "committed_usd": committed + remaining})

        def settle(cost):
            with _AUTO_SCAN_LOCK:
                cache_set(budget_key, {"date": today, "committed_usd": committed + cost})

        end = (datetime.fromisoformat(session) + timedelta(days=1)).date().isoformat()
        result = subprocess.run(
            [sys.executable, str(AGENT_DIR / "scripts" / "backfill_daily_gap.py"),
             "--start", session, "--end", end, "--execute", "--max-cost-usd", str(remaining)],
            cwd=str(AGENT_DIR), capture_output=True, text=True, timeout=120,
        )
        if result.returncode:
            cost_error = re.search(r"Estimated cost \$([0-9.]+) exceeds cap \$([0-9.]+)", result.stderr or "")
            if cost_error:
                settle(0.0)
                return {"ok": False, "error": "Databento price repair exceeds configured cost cap",
                        "estimated_cost_usd": float(cost_error.group(1)), "max_cost_usd": cap}
            return {"ok": False, "error": "Databento gap repair unavailable or cost cap exceeded",
                    "return_code": result.returncode, "max_cost_usd": cap}
        report = json.loads(result.stdout)
        settle(max(0.0, float(report.get("estimated_cost_usd") or 0)))
        return {"ok": True, "symbols_filled": report.get("symbols_filled", 0),
                "estimated_cost_usd": report.get("estimated_cost_usd"), "max_cost_usd": cap}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def _daily_post_scan_finalize(universe_ids: Optional[List[str]]) -> Dict[str, Any]:
    """Persist the daily snapshot and full candidate ledger before enrichment."""
    out: Dict[str, Any] = {"snapshot_id": "", "snapshot_row_count": 0, "market_data_fresh": True, "timings": {}}
    try:
        return _daily_post_scan_finalize_impl(universe_ids, out)
    except Exception as exc:
        exc.daily_result = out
        raise


def _daily_post_scan_finalize_impl(universe_ids, out):
    timings: Dict[str, float] = out["timings"]

    def _timed(label: str, fn):
        t0 = time.time()
        try:
            return fn()
        finally:
            timings[label] = round(time.time() - t0, 2)
            console.log(f"[daily-timing] {label}: {timings[label]}s")

    session = _daily_sync_prices(universe_ids, out, allow_partial=True)

    # 2) Cache-only compute (no external sockets -> cannot hang / cannot burn quota).
    with external_data_scope(False):
        snapshot_payload = _timed(
            "snapshot_build",
            lambda: _build_home_dashboard_snapshot_payload(
                universe_ids, required_price_session=session, allow_partial_price_evidence=True,
            ),
        )
        rows = snapshot_payload.get("rows") or []
        out["snapshot_row_count"] = len(rows)
        out["excluded_stale_count"] = int(snapshot_payload.get("excluded_stale_count") or 0)
        out["price_coverage"] = snapshot_payload.get("price_coverage")
        if not rows:
            raise RuntimeError("daily snapshot has no candidates; keeping the previous successful snapshot")
        snapshot_payload["data_as_of"] = session
        out["snapshot_id"] = _timed("snapshot_save", lambda: save_home_dashboard_snapshot(snapshot_payload, source="daily_auto"))

        from priority_board_service import MAX_CACHED_PICKS, compute_priority_board
        board = _timed("board_build", lambda: compute_priority_board(limit=MAX_CACHED_PICKS, force_refresh=True))
        if not board.get("picks"):
            raise RuntimeError("priority board has no candidates; daily ledger was not updated")
        from prediction_ledger_service import log_predictions
        ledger = _timed("predictions_log", lambda: log_predictions(30, as_of_date=session))
        out["prediction_ledger"] = ledger
        if int(ledger.get("slice_rows_written") or 0) <= 0:
            raise RuntimeError("daily prediction ledger wrote no candidate slices")
    return out


def _daily_post_scan_optional_enrichment() -> None:
    """Run nonessential enrichment after the candidate ledger is durable."""
    threading.Thread(target=_daily_update_optional_evidence, daemon=True, name="daily-evidence-refresh").start()
    try:
        from prediction_ledger_service import prediction_scorecard, resolve_predictions
        console.log(f"predictions resolved: {resolve_predictions()}")
        prediction_scorecard(force=True)
    except Exception as exc:
        console.log(f"prediction ledger resolve/scorecard failed: {str(exc)[-200:]}")
    try:
        from priority_board_service import liquidity_floor
        with external_data_scope(False):
            liquidity_floor(force=True)
    except Exception as exc:
        console.log(f"liquidity floor refresh failed: {str(exc)[-200:]}")


def _daily_update_optional_evidence() -> None:
    """Event/LLM failure must never revoke an already persisted signal board."""
    if not _DAILY_EVIDENCE_LOCK.acquire(blocking=False):
        return
    key = "daily_three_layer_auto:evidence"
    result = {"date": _auto_scan_today_key(), "status": "running", "event_status": "pending", "llm_status": "pending"}
    try:
        cache_set(key, result)
        snapshot = latest_home_dashboard_snapshot() or {}
        payload = snapshot.get("payload") or {}
        rows = payload.get("rows") or []
        symbols = list(dict.fromkeys(str(r.get("symbol") or "") for r in rows if r.get("symbol")))[:100]
        result.update(event_symbol_count=len(symbols), event_candidate_count=len(rows))
        if not symbols:
            result.update(status="unavailable", event_status="unknown", llm_status="skipped")
            return
        try:
            _run_event_driven_scan_sync(EventDrivenScanRequest(
                universe="daily_unified", tickers=symbols, top=len(symbols),
                news_per_ticker=3, lookback_hours=96, workers=12, sleep_seconds=0,
                include_prices=False, auto_calibration=True, use_llm_scoring=False,
                timeout_seconds=max(30, min(300, int(os.getenv("VIBE_DAILY_EVENT_TIMEOUT", "120")))),
            ))
            result["event_status"] = "completed"
        except Exception as exc:
            result.update(event_status="unavailable", event_error=f"事件证据暂不可用：{type(exc).__name__}")
        cache_set(key, result)
        try:
            import sys
            with external_data_scope(False):
                contexts = [_build_symbol_research_context(
                    str(r["symbol"]), opportunity=_latest_stock_opportunity(str(r["symbol"])),
                    scan_universe=(r.get("source_universe_ids") or [None])[0],
                ) for r in rows[:12]]
            proc = subprocess.run(
                [sys.executable, "-m", "scripts.review_daily_contexts"], cwd=str(AGENT_DIR),
                input=json.dumps(_json_safe(contexts)), capture_output=True, text=True,
                timeout=max(10, min(300, int(os.getenv("VIBE_DAILY_LLM_TIMEOUT", "60")))),
            )
            reviews = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else {}
            available = sum(1 for r in reviews.values() if r.get("available"))
            result.update(llm_status="completed" if available else "unavailable", llm_available_count=available)
            # Optional text only: keep prices, rankings and calibration unchanged.
            current = latest_home_dashboard_snapshot() or {}
            if available and current.get("snapshot_id") == snapshot.get("snapshot_id"):
                for row in rows:
                    if row.get("symbol") in reviews:
                        row["llm_review"] = reviews[row["symbol"]]
                save_home_dashboard_snapshot(payload, source="daily_optional_evidence")
        except Exception as exc:
            result.update(llm_status="unavailable", llm_error=f"AI复核暂不可用：{type(exc).__name__}")
        result["status"] = "completed" if result["event_status"] == result["llm_status"] == "completed" else "partial"
    except Exception as exc:
        result.update(status="unavailable", error=f"后台补充暂不可用：{type(exc).__name__}")
    finally:
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        cache_set(key, result)
        _DAILY_EVIDENCE_LOCK.release()


def _daily_finalize_outcome(finalize: Dict[str, Any], timed_out: bool, failed_universes: int) -> tuple[str, str]:
    error = str(finalize.get("error") or "")
    if timed_out:
        error = f"daily finalize timed out after {_AUTO_SCAN_FINALIZE_TIMEOUT_SECONDS}s"
    elif not finalize.get("snapshot_id") or int(finalize.get("snapshot_row_count") or 0) <= 0:
        error = error or "daily snapshot was not saved"
    return ("failed" if error else "partial" if failed_universes or finalize.get("excluded_stale_count") or finalize.get("data_warnings") else "completed", error)


def _run_daily_three_layer_auto_scan(
    *,
    reason: str,
    force: bool = False,
    universes: Optional[List[str]] = None,
    top: int = _AUTO_SCAN_TOP,
    llm_review_top: int = _AUTO_SCAN_LLM_REVIEW_TOP,
    refresh_evidence: bool = _AUTO_SCAN_REFRESH_EVIDENCE,
) -> None:
    today = _auto_scan_today_key()
    started_at = datetime.now(_auto_scan_timezone()).isoformat()
    universe_ids = _auto_scan_universe_ids(universes)
    with _AUTO_SCAN_LOCK:
        _AUTO_SCAN_JOB.update({
            "status": "running",
            "message": "daily three-layer auto scan running",
            "progress": 0.01,
            "current_universe": "",
            "started_at": started_at,
            "finished_at": None,
            "reason": reason,
            "universe_count": len(universe_ids),
            "results": [],
        })
    completed = 0
    failed = 0
    llm_available = 0
    results: List[Dict[str, Any]] = []
    universe_timings: Dict[str, float] = {}
    loop_started = time.time()
    preflight: Dict[str, Any] = {"timings": {}}
    phase = "price_preflight"
    checkpoint_key = "daily_three_layer_auto:checkpoint"
    signature = {"top": top, "llm_review_top": llm_review_top, "refresh_evidence": refresh_evidence}
    try:
        with _AUTO_SCAN_LOCK:
            _AUTO_SCAN_JOB.update({"phase": phase, "message": "先检查最新交易日行情；通过后才运行三层扫描"})
        _start_daily_news_update()
        session = _daily_sync_prices(universe_ids, preflight, allow_partial=True)
        checkpoint = cache_get(checkpoint_key) or {}
        reusable = (not force and checkpoint.get("date") == today
                    and checkpoint.get("session") == session and checkpoint.get("config") == signature)
        saved_results = dict(checkpoint.get("results") or {}) if reusable else {}
        checkpoint = {"date": today, "session": session, "config": signature, "results": saved_results}
        phase = "pool_scan"
        for index, universe in enumerate(universe_ids, start=1):
            saved = saved_results.get(universe) or {}
            if saved.get("status") == "completed":
                results.append({**saved, "reused": True})
                completed += 1
                llm_available += int((saved.get("result") or {}).get("llm_review_available_count") or 0)
                universe_timings[universe] = 0.0
                with _AUTO_SCAN_LOCK:
                    _AUTO_SCAN_JOB["results"] = copy.deepcopy(results)
                continue
            with _AUTO_SCAN_LOCK:
                _AUTO_SCAN_JOB.update({
                    "current_universe": universe,
                    "message": f"running {universe} ({index}/{len(universe_ids)})",
                    "progress": round(0.02 + 0.82 * (index - 1) / max(1, len(universe_ids)), 3),
                })
            child_job_id = f"daily_auto_{universe}_{uuid.uuid4().hex[:8]}"
            payload = ResearchSignalHubRunRequest(
                universe=universe,
                refresh_evidence=refresh_evidence,
                top=top,
                enable_llm_review=True,
                llm_review_top=llm_review_top,
                enable_event_llm_review=True,
                reuse_session_results=True,
            )
            _research_signal_hub_jobs[child_job_id] = {
                "job_id": child_job_id,
                "universe": universe,
                "status": "running",
                "phase": "queued",
                "message": "started by daily auto scan",
                "progress": 0.0,
                "started_at": datetime.utcnow().isoformat() + "Z",
                "deadline_monotonic": time.monotonic() + _AUTO_SCAN_CHILD_TIMEOUT_SECONDS,
                "daily_price_cache_only": True,
            }
            child_thread = threading.Thread(
                target=_run_research_signal_hub_job,
                args=(child_job_id, payload),
                daemon=True,
            )
            child_thread.start()
            child_started = time.time()
            with _AUTO_SCAN_LOCK:
                _DAILY_ACTIVE_WORKERS[child_job_id] = child_thread
            child_timed_out = False
            while child_thread.is_alive():
                child_live = dict(_research_signal_hub_jobs.get(child_job_id) or {})
                child_progress = float(child_live.get("progress", 0.0) or 0.0)
                with _AUTO_SCAN_LOCK:
                    _AUTO_SCAN_JOB.update({
                        "current_child_job_id": child_job_id,
                        "current_child_phase": child_live.get("phase"),
                        "current_child_message": child_live.get("message"),
                        "message": f"{universe}: {child_live.get('message') or 'running three-layer scan'}",
                        "progress": round(0.02 + 0.82 * ((index - 1) + child_progress) / max(1, len(universe_ids)), 3),
                    })
                if time.time() - child_started > _AUTO_SCAN_CHILD_TIMEOUT_SECONDS:
                    child_timed_out = True
                    _research_signal_hub_jobs[child_job_id]["cancel_requested"] = True
                    stock_job_id = child_live.get("stock_signal_job_id")
                    if stock_job_id in _stock_signal_update_jobs:
                        _stock_signal_update_jobs[stock_job_id]["cancel_requested"] = True
                    _research_signal_hub_jobs[child_job_id].update({
                        "status": "failed",
                        "phase": "timeout",
                        "message": f"daily auto scan child timeout after {_AUTO_SCAN_CHILD_TIMEOUT_SECONDS}s",
                        "progress": 1.0,
                        "finished_at": datetime.utcnow().isoformat() + "Z",
                    })
                    child_thread.join(timeout=10)
                    break
                time.sleep(1.5)
            if not child_timed_out:
                child_thread.join()
            child = dict(_research_signal_hub_jobs.get(child_job_id) or {})
            result = child.get("result") or {}
            if child.get("status") == "completed":
                completed += 1
                llm_available += int(result.get("llm_review_available_count", 0) or 0)
            else:
                failed += 1
            results.append({
                "universe": universe,
                "job_id": child_job_id,
                "status": child.get("status"),
                "message": child.get("message"),
                "result": result,
            })
            universe_timings[universe] = round(time.time() - child_started, 1)
            saved_results[universe] = {
                **results[-1], "result": {
                    key: value for key, value in result.items() if key.endswith("count")
                }, "elapsed_seconds": universe_timings[universe],
            }
            cache_set(checkpoint_key, copy.deepcopy(checkpoint))
            with _AUTO_SCAN_LOCK:
                _AUTO_SCAN_JOB["results"] = copy.deepcopy(results)
            if child_thread.is_alive() or _daily_active_worker_ids():
                raise RuntimeError(f"{universe} cancellation still winding down; stopped batch to avoid overlapping scans")
        universe_loop_total = round(time.time() - loop_started, 1)
        phase = "finalize"
        with _AUTO_SCAN_LOCK:
            _AUTO_SCAN_JOB.update({
                "message": "building unified SQLite home snapshot",
                "progress": 0.9,
                "current_universe": "",
            })
        # The watchdog bounds critical persistence. A timed-out worker must not
        # be reported as a completed scan, even if it finishes later.
        finalize: Dict[str, Any] = {}

        def _finalize_worker() -> None:
            try:
                finalize.update(_daily_post_scan_finalize(universe_ids))
            except Exception as exc:  # never let the worker die silently
                finalize.update(getattr(exc, "daily_result", {}))
                finalize["error"] = str(exc)[-1200:]
                console.log(f"[daily-error] finalize: {traceback.format_exc()}")

        fth = threading.Thread(target=_finalize_worker, daemon=True)
        fth.start()
        with _AUTO_SCAN_LOCK:
            _DAILY_ACTIVE_WORKERS["finalize"] = fth
        fth.join(_AUTO_SCAN_FINALIZE_TIMEOUT_SECONDS)
        finalize_timed_out = fth.is_alive()
        if finalize_timed_out:
            console.log(
                f"daily finalize exceeded {_AUTO_SCAN_FINALIZE_TIMEOUT_SECONDS}s; "
                "marking failed (orphaned finalize may still wind down)"
            )
        snapshot_id = finalize.get("snapshot_id", "")
        snapshot_row_count = int(finalize.get("snapshot_row_count", 0) or 0)
        finalize["data_warnings"] = list(dict.fromkeys([*(preflight.get("data_warnings") or []), *(finalize.get("data_warnings") or [])]))
        status, finalize_error = _daily_finalize_outcome(finalize, finalize_timed_out, failed)
        finished_at = datetime.now(_auto_scan_timezone()).isoformat()
        summary = {
            "date": today,
            "status": status,
            "reason": reason,
            "force": force,
            "started_at": started_at,
            "finished_at": finished_at,
            "universe_count": len(universe_ids),
            "completed_universe_count": completed,
            "failed_universe_count": failed,
            "llm_review_available_count": llm_available,
            "snapshot_id": snapshot_id,
            "snapshot_row_count": snapshot_row_count,
            "excluded_stale_count": int(finalize.get("excluded_stale_count") or 0),
            "price_coverage": finalize.get("price_coverage"),
            "cached_price_coverage": finalize.get("cached_price_coverage"),
            "finalize_timed_out": finalize_timed_out,
            "error": finalize_error or None,
            "phase": phase,
            "results": [copy.deepcopy(saved_results[u]) for u in universe_ids if u in saved_results],
            "resumed_universe_count": sum(1 for item in results if item.get("reused")),
            "price_repair": preflight.get("price_repair"),
            "data_warnings": finalize.get("data_warnings") or [],
            "prediction_ledger": finalize.get("prediction_ledger"),
            "market_data_fresh": bool(finalize.get("market_data_fresh", True)),
            "phase_timings": {
                "universe_loop_total": universe_loop_total,
                "per_universe": universe_timings,
                "finalize_total": round(time.time() - loop_started - universe_loop_total, 1),
                **(finalize.get("timings") or {}),
                "price_preflight": round(sum(preflight["timings"].values()), 1),
                "total": round(time.time() - loop_started, 1),
            },
        }
        cache_set(_AUTO_SCAN_STATE_KEY, summary)
        with _AUTO_SCAN_LOCK:
            _AUTO_SCAN_JOB.update({
                "status": status,
                "message": f"daily three-layer auto scan {status}" + (f": {finalize_error}" if finalize_error else ""),
                "progress": 1.0,
                "finished_at": finished_at,
                "last_result": summary,
                "results": results[-20:],
            })
        if status != "failed":
            threading.Thread(target=_daily_post_scan_optional_enrichment, daemon=True).start()
    except Exception as exc:
        finished_at = datetime.now(_auto_scan_timezone()).isoformat()
        summary = {
            "date": today,
            "status": "failed",
            "reason": reason,
            "force": force,
            "started_at": started_at,
            "finished_at": finished_at,
            "universe_count": len(universe_ids),
            "completed_universe_count": completed,
            "failed_universe_count": failed + max(0, len(universe_ids) - completed - failed),
            "error": str(exc)[-1200:],
            "phase": phase,
            "results": copy.deepcopy(results),
            "cached_price_coverage": preflight.get("cached_price_coverage"),
            "price_repair": preflight.get("price_repair"),
            "phase_timings": {
                **preflight["timings"], "per_universe": universe_timings,
                "total": round(time.time() - loop_started, 1),
            },
        }
        console.log(f"[daily-error] {phase}: {traceback.format_exc()}")
        cache_set(_AUTO_SCAN_STATE_KEY, summary)
        with _AUTO_SCAN_LOCK:
            _AUTO_SCAN_JOB.update({
                "status": "failed",
                "message": f"daily auto scan failed: {str(exc)[-500:]}",
                "progress": 1.0,
                "finished_at": finished_at,
                "last_result": summary,
                "results": results[-20:],
            })


def _start_daily_three_layer_auto_scan(
    *,
    reason: str,
    force: bool = False,
    universes: Optional[List[str]] = None,
    top: int = _AUTO_SCAN_TOP,
    llm_review_top: int = _AUTO_SCAN_LLM_REVIEW_TOP,
    refresh_evidence: bool = _AUTO_SCAN_REFRESH_EVIDENCE,
) -> Dict[str, Any]:
    with _AUTO_SCAN_LOCK:
        if _AUTO_SCAN_JOB.get("status") in {"queued", "running"}:
            return _auto_scan_state()
        if _daily_active_worker_ids():
            return {**_auto_scan_state(), "blocked_by_active_worker": True,
                    "message": "上次超时任务正在退出，请稍后接续；未启动重叠扫描"}
        if not force:
            last_record = _auto_scan_last_record()
            if last_record.get("date") == _auto_scan_today_key() and last_record.get("status") == "completed":
                _AUTO_SCAN_JOB.update({
                    "status": last_record["status"],
                    "message": "today's daily auto scan already attempted; manual retry is available",
                    "progress": 1.0,
                    "last_result": last_record,
                })
                return _auto_scan_state()
        _AUTO_SCAN_JOB.update({
            "status": "queued",
            "message": "daily three-layer auto scan queued",
            "progress": 0.0,
            "current_universe": "",
        })
    thread = threading.Thread(
        target=_run_daily_three_layer_auto_scan,
        kwargs={
            "reason": reason,
            "force": force,
            "universes": universes,
            "top": top,
            "llm_review_top": llm_review_top,
            "refresh_evidence": refresh_evidence,
        },
        daemon=True,
    )
    thread.start()
    return _auto_scan_state()


def _run_daily_finalize_only() -> None:
    """Recover the full daily snapshot from existing per-pool evidence."""
    universe_ids = _auto_scan_universe_ids()
    _start_daily_news_update()
    previous = _auto_scan_last_record()
    if previous.get("date") != _auto_scan_today_key():
        previous = {}
    completed_pools = int(previous.get("completed_universe_count") or 0)
    failed_pools = int(previous.get("failed_universe_count") or 0)
    started_at = datetime.now(_auto_scan_timezone()).isoformat()
    with _AUTO_SCAN_LOCK:
        _AUTO_SCAN_JOB.update({
            "status": "running", "message": "reusing completed pools; syncing one session and building snapshot",
            "progress": 0.9, "current_universe": "", "started_at": started_at,
            "finished_at": None, "reason": "finalize_only", "universe_count": len(universe_ids),
        })
    started = time.time()
    finalize: Dict[str, Any] = {}

    def worker() -> None:
        try:
            finalize.update(_daily_post_scan_finalize(universe_ids))
        except Exception as exc:
            finalize.update(getattr(exc, "daily_result", {}))
            finalize["error"] = str(exc)[-500:]

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    with _AUTO_SCAN_LOCK:
        _DAILY_ACTIVE_WORKERS["finalize"] = thread
    thread.join(_AUTO_SCAN_FINALIZE_TIMEOUT_SECONDS)
    timed_out = thread.is_alive()
    status, error = _daily_finalize_outcome(finalize, timed_out, failed_pools)
    finished_at = datetime.now(_auto_scan_timezone()).isoformat()
    summary = {
        "date": _auto_scan_today_key(), "status": status, "reason": "finalize_only",
        "started_at": started_at, "finished_at": finished_at,
        "universe_count": len(universe_ids),
        "completed_universe_count": completed_pools,
        "failed_universe_count": failed_pools, "snapshot_id": finalize.get("snapshot_id", ""),
        "results": previous.get("results") or [],
        "snapshot_row_count": int(finalize.get("snapshot_row_count", 0) or 0),
        "excluded_stale_count": int(finalize.get("excluded_stale_count") or 0),
        "price_coverage": finalize.get("price_coverage"),
        "cached_price_coverage": finalize.get("cached_price_coverage"),
        "finalize_timed_out": timed_out, "error": error or None,
        "market_data_fresh": finalize.get("market_data_fresh"),
        "price_repair": finalize.get("price_repair"),
        "data_warnings": finalize.get("data_warnings") or [],
        "phase_timings": {"total": round(time.time() - started, 1), **(finalize.get("timings") or {})},
    }
    cache_set(_AUTO_SCAN_STATE_KEY, summary)
    with _AUTO_SCAN_LOCK:
        _AUTO_SCAN_JOB.update({
            "status": status,
            "message": error or (
                f"daily snapshot rebuilt; excluded {finalize['excluded_stale_count']} stale-price candidates"
                if finalize.get("excluded_stale_count") else "daily snapshot rebuilt from existing pool results"
            ),
            "progress": 1.0, "finished_at": finished_at, "last_result": summary,
        })
    if status != "failed":
        threading.Thread(target=_daily_post_scan_optional_enrichment, daemon=True).start()


def _daily_auto_retry_due(now, record):
    if record.get("date") != _auto_scan_today_key(now) or record.get("status") not in {"failed", "partial"}:
        return False
    if record.get("phase") not in {"price_preflight", "finalize"}:
        return False
    retry = cache_get("daily_three_layer_auto:retries") or {}
    count = int(retry.get("count", 0)) if retry.get("date") == record["date"] else 0
    limit = max(0, int(os.getenv("VIBE_DAILY_AUTO_RETRY_LIMIT", "2")))
    if count >= limit:
        return False
    try:
        finished = datetime.fromisoformat(str(record.get("finished_at") or ""))
        interval = max(60, int(os.getenv("VIBE_DAILY_AUTO_RETRY_SECONDS", "1800")))
        return (now - finished).total_seconds() >= interval
    except (ValueError, TypeError):
        return False


def _daily_three_layer_auto_loop() -> None:
    if not _AUTO_SCAN_ENABLED:
        with _AUTO_SCAN_LOCK:
            _AUTO_SCAN_JOB.update({"status": "disabled", "message": "daily auto scan disabled by env"})
        return
    if _AUTO_SCAN_STARTUP_DELAY_SECONDS:
        time.sleep(_AUTO_SCAN_STARTUP_DELAY_SECONDS)
    while True:
        try:
            now = datetime.now(_auto_scan_timezone())
            scheduled = _auto_scan_scheduled_at(now)
            last_record = _auto_scan_last_record()
            with _AUTO_SCAN_LOCK:
                running = _AUTO_SCAN_JOB.get("status") in {"queued", "running"}
                if (
                    not running
                    and last_record.get("date") == _auto_scan_today_key(now)
                    and last_record.get("status") in {"completed", "partial", "failed"}
                ):
                    _AUTO_SCAN_JOB.update({
                        "status": last_record["status"],
                        "message": "today's daily auto scan already attempted; manual retry is available",
                        "progress": 1.0,
                        "last_result": last_record,
                        "finished_at": last_record.get("finished_at"),
                    })
            if (
                not running
                and now >= scheduled
                and not (last_record.get("date") == _auto_scan_today_key(now) and last_record.get("status") in {"completed", "partial", "failed"})
            ):
                _start_daily_three_layer_auto_scan(
                    reason="scheduled_or_startup_catchup",
                    refresh_evidence=_AUTO_SCAN_REFRESH_EVIDENCE,
                )
            elif not running and not _daily_active_worker_ids() and _daily_auto_retry_due(now, last_record):
                retry = cache_get("daily_three_layer_auto:retries") or {}
                count = int(retry.get("count", 0)) if retry.get("date") == _auto_scan_today_key(now) else 0
                cache_set("daily_three_layer_auto:retries", {"date": _auto_scan_today_key(now), "count": count + 1})
                _start_daily_three_layer_auto_scan(reason="automatic_checkpoint_retry")
            sleep_seconds = _AUTO_SCAN_POLL_SECONDS
            if now < scheduled:
                sleep_seconds = max(30, min(_AUTO_SCAN_POLL_SECONDS, int((scheduled - now).total_seconds())))
            time.sleep(sleep_seconds)
        except Exception as exc:
            console.log(f"daily auto scan loop error: {str(exc)[-500:]}")
            time.sleep(_AUTO_SCAN_POLL_SECONDS)


@app.on_event("startup")
async def _start_daily_three_layer_auto_scheduler() -> None:
    """Start the in-container daily three-layer scanner."""
    threading.Thread(target=_daily_three_layer_auto_loop, daemon=True).start()


def _signal_oos_loop() -> None:
    """Daily out-of-sample accrual: log today's signals + resolve matured ones.

    Both steps are idempotent (log dedups per symbol/date/horizon; resolve only
    touches unresolved rows), so a coarse interval is safe.  This is what turns
    the calibration from in-sample replay into genuine OOS over time.
    """
    if os.getenv("VIBE_OOS_COLLECTION_ENABLED", "1").lower() in {"0", "false", "no"}:
        console.log("OOS collection disabled by env")
        return
    universe = os.getenv("VIBE_OOS_UNIVERSE", "spx")
    try:
        interval = max(3600, int(os.getenv("VIBE_OOS_INTERVAL_SECONDS", str(6 * 3600))))
    except (TypeError, ValueError):
        interval = 6 * 3600
    time.sleep(min(900, _AUTO_SCAN_STARTUP_DELAY_SECONDS or 300))  # let the app settle first
    while True:
        try:
            from long_option_shadow_service import record_daily_from_cache, resolve_pending as resolve_long_option_shadow
            shadow_log = record_daily_from_cache()
            shadow_resolve = resolve_long_option_shadow()
            console.log(f"long option shadow: logged={shadow_log} settled={shadow_resolve}")
        except Exception as exc:
            console.log(f"long option shadow cycle failed: {str(exc)[-300:]}")
        try:
            from scripts.log_daily_signals import log_daily_signals
            from scripts.resolve_signal_events import resolve_pending_events

            logged = log_daily_signals(universe)
            resolved = resolve_pending_events(limit=5000)
            iv_logged = _capture_candidate_iv()
            console.log(
                f"OOS cycle: logged_new={logged.get('events_logged_new')} "
                f"resolved={resolved.get('resolved')} iv_captured={iv_logged} counts={resolved.get('counts')}"
            )
        except Exception as exc:
            console.log(f"OOS collection loop error: {str(exc)[-400:]}")
        time.sleep(interval)


def _capture_candidate_iv() -> int:
    """Log ATM IV for the top snapshot candidates (for the IV-rise launch signal)."""
    try:
        from iv_signal_service import log_candidate_iv

        rows = (((latest_home_dashboard_snapshot() or {}).get("payload") or {}).get("rows") or [])
        symbols = [str(r.get("symbol") or "").upper() for r in rows if r.get("symbol")][:30]
        return int(log_candidate_iv(symbols, limit=30).get("captured", 0))
    except Exception:
        return 0


@app.on_event("startup")
async def _start_signal_oos_scheduler() -> None:
    """Start the in-container daily out-of-sample signal collector."""
    threading.Thread(target=_signal_oos_loop, daemon=True).start()


@app.get("/daily-three-layer-auto/status", dependencies=[Depends(require_auth)])
async def daily_three_layer_auto_status():
    """Return the in-container daily auto scan status."""
    return _auto_scan_state()


@app.post("/daily-three-layer-auto/run", dependencies=[Depends(require_auth)])
async def daily_three_layer_auto_run(payload: DailyThreeLayerAutoRunRequest):
    """Manually start the all-universe daily three-layer scan."""
    return _start_daily_three_layer_auto_scan(
        reason="manual",
        force=payload.force,
        universes=payload.universes,
        top=payload.top,
        llm_review_top=payload.llm_review_top,
        refresh_evidence=payload.refresh_evidence,
    )


@app.get("/stock-signals/reports", dependencies=[Depends(require_auth)])
async def list_stock_signal_reports():
    """List recent options-screening reports available as stock-signal evidence."""
    reports = []
    seen = set()
    for path in _stock_signal_report_candidates():
        payload = _load_json_file(path) or {}
        if not _stock_signal_report_has_results(payload):
            continue
        item = _stock_signal_report_summary(path)
        key = (item["run_id"], item["universe"])
        if key in seen:
            continue
        seen.add(key)
        reports.append(item)
        if len(reports) >= 20:
            break
    combined = next((item for item in reports if item["universe"] == "sp100_komp_soxx"), None)
    if combined:
        combined_path, combined_payload, _ = _stock_signal_find_report("sp100_komp_soxx")
        if combined_path:
            for universe, label in _STOCK_SIGNAL_SPLIT_POOLS.items():
                split_results = _stock_signal_filter_results(combined_payload, universe)
                split_bullish = [
                    r for r in split_results
                    if str(r.get("primary_strategy")) in _STOCK_SIGNAL_BULLISH_STRATEGIES
                ]
                reports.append({
                    **combined,
                    "universe": universe,
                    "source": f"derived:{label};{combined.get('source', '')}",
                    "ticker_count": len(split_results),
                    "valid_tickers": len(split_results),
                    "bullish_signals": len(split_bullish),
                    "derived_from": "sp100_komp_soxx",
                })
    return {"reports": reports, "supported_universes": _STOCK_SIGNAL_UNIVERSE_CATALOG}


@app.get("/stock-signals/latest", dependencies=[Depends(require_auth)])
async def get_latest_stock_signals(universe: str = Query("spx"), limit: int = Query(20, ge=1, le=100)):
    """Reframe bullish options-screening results as cash-equity research signals."""
    if universe == _SPECULATIVE_PEER_EARNINGS_UNIVERSE:
        resolved = resolve_research_universe(universe)
        signals = _peer_relay_stock_signal_items(limit=limit)
        if not signals:
            raise HTTPException(status_code=404, detail="投机-同行财报接力池尚未运行专项扫描。请点击“运行专项接力扫描”。")
        return {
            "run_id": "peer_earnings_relay_cache",
            "universe": universe,
            "universe_label": "投机-同行财报接力池",
            "source": resolved.get("source", ""),
            "timestamp": max(
                str(item.get("scan_time") or "")
                for item in _latest_launch_signals.values()
                if item.get("scan_universe") == universe
            ),
            "ticker_count": int(resolved.get("ticker_count", 0) or 0),
            "valid_tickers": len(_peer_relay_stock_signal_items(limit=1000)),
            "bullish_count": len(_peer_relay_stock_signal_items(limit=1000)),
            "signals": signals,
            "report_pdf_url": "",
            "derived_from": None,
            "method_note": "专项投机池只运行同行财报接力模型，不与常规实股机会池混跑，也不生成期权底层 PDF。候选需要人工复核财报原文、披露日期、量价和流动性。",
        }
    selected_path, selected_payload, derived = _stock_signal_find_report(universe)
    if selected_path is None:
        raise HTTPException(status_code=404, detail=f"股票池 {universe} 尚未生成报告。请点击“更新所选股票池”运行一次筛选。")
    run_id = _stock_signal_run_id(selected_path)
    universe_info = selected_payload.get("universe") or {}
    memberships = universe_info.get("memberships") or {}
    pool_results = _stock_signal_filter_results(selected_payload, universe)
    signals = _stock_signal_observation_pool(universe, limit=limit)
    return {
        "run_id": run_id,
        "universe": universe,
        "source": universe_info.get("source", ""),
        "timestamp": selected_payload.get("timestamp", ""),
        "ticker_count": len(pool_results) if derived else selected_payload.get("ticker_count", 0),
        "valid_tickers": len(pool_results) if derived else selected_payload.get("valid_tickers", 0),
        "bullish_count": len(signals),
        "signals": signals,
        "report_pdf_url": f"/runs/{run_id}/report.pdf?download=1",
        "derived_from": "sp100_komp_soxx" if derived else None,
        "method_note": "机会评分采用“严格候选 + 备选观察”两层：强趋势和看多辅助证据排前；若池子偏弱，会保留研究分较高的备选标的继续交给启动信号和事件雷达复核，避免整池被筛空。",
    }


@app.post("/stock-signals/update", dependencies=[Depends(require_auth)])
async def start_stock_signal_update(payload: StockSignalUpdateRequest):
    """Start a cached background refresh for one stock-signal evidence pool."""
    supported = {item["universe"] for item in _STOCK_SIGNAL_UNIVERSE_CATALOG}
    universe = payload.universe.strip().lower()
    if universe not in supported:
        raise HTTPException(status_code=400, detail=f"不支持的股票池：{universe}")
    with _stock_signal_update_lock:
        for job_id, job in _stock_signal_update_jobs.items():
            if job.get("universe") == universe and job.get("status") in {"queued", "running"}:
                return {"job_id": job_id, **job}
        job_id = f"stock_signal_{uuid.uuid4().hex[:10]}"
        run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:2]}_{uuid.uuid4().hex[:6]}"
        _stock_signal_update_jobs[job_id] = {
            "job_id": job_id,
            "run_id": run_id,
            "universe": universe,
            "status": "queued",
            "message": "任务已排队，准备运行筛选模型...",
            "progress": 0.0,
            "started_at": datetime.utcnow().isoformat() + "Z",
        }
    threading.Thread(target=_run_stock_signal_update_job, args=(job_id, universe), daemon=True).start()
    return _stock_signal_update_jobs[job_id]


@app.get("/stock-signals/update/{job_id}", dependencies=[Depends(require_auth)])
async def get_stock_signal_update(job_id: str):
    """Return current background refresh progress."""
    _validate_path_param(job_id, "job_id")
    job = _stock_signal_update_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"更新任务不存在：{job_id}")
    return job


@app.get("/stock-signal-tool", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def stock_signal_tool():
    """Embedded cash-equity buy-signal workbench backed by options research reports."""
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>实股信号</title>
  <style>
    :root{color-scheme:dark;--bg:#09131b;--panel:#101e29;--line:#263947;--muted:#91a5b2;--text:#eef5f7;--cyan:#43c6b7;--blue:#65a7ff;--amber:#e9b85d;--red:#f07b78}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Inter,"Microsoft YaHei",Arial,sans-serif}
    main{padding:22px 24px 34px;max-width:1700px;margin:auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:14px;margin:0}.sub,.muted{color:var(--muted)}
    .toolbar,.metrics,.grid{display:grid;gap:10px}.toolbar{grid-template-columns:minmax(220px,1fr) 110px minmax(150px,max-content) minmax(100px,max-content) max-content;margin:18px 0 12px}.metrics{grid-template-columns:repeat(5,minmax(120px,1fr));margin-bottom:14px}
    select,button{background:#152632;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:9px 11px;white-space:nowrap}button{cursor:pointer}button.primary{background:#16635e;border-color:#21887f;font-weight:700}button.update{background:#6b4d18;border-color:#9c7631;font-weight:700}button:hover{filter:brightness(1.12)}button:disabled{cursor:not-allowed;opacity:.55;filter:none}
    .metric,.panel{background:var(--panel);border:1px solid var(--line);border-radius:7px}.metric{padding:11px 13px}.metric b{display:block;font-size:19px;margin-top:3px}.layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:12px}.framework{margin:12px 0 4px;padding:10px 12px;border-left:3px solid var(--cyan);background:#10252d;color:#bcd7db;font-size:12px}
    .panel-head{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-bottom:1px solid var(--line)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #20333f;vertical-align:middle}th{font-size:12px;color:#b9cbd3;background:#12232e;position:sticky;top:0}tbody tr{cursor:pointer}tbody tr:hover,tbody tr.active{background:#16303b}
    .scroll{overflow:auto;max-height:calc(100vh - 235px)}.badge{display:inline-block;border:1px solid;padding:2px 6px;border-radius:999px;font-size:11px;white-space:nowrap}.strong{color:#7de0c3;border-color:#327f70;background:#153e3a}.watch{color:#8fc0ff;border-color:#386b9c;background:#17314b}.neutral{color:#d9bd83;border-color:#7a6132;background:#3d321d}
    .sources{color:#a6d7ff;font-size:12px}.spark{width:104px;height:28px;display:block}.detail{padding:14px}.detail h3{margin:0 0 12px;font-size:18px}.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.kv{background:#142631;border:1px solid #213744;border-radius:6px;padding:8px}.kv span{display:block;color:var(--muted);font-size:11px}.kv b{display:block;margin-top:2px}.section{margin-top:14px}.section b{display:block;margin-bottom:6px}.list{margin:0;padding-left:17px;color:#c2d1d8}.note{margin-top:12px;padding:10px;background:#132b32;border-left:3px solid var(--cyan);color:#bcd7db}.empty{padding:24px;color:var(--muted)}a{color:#86baff;text-decoration:none}.risk-高{color:#ff9693}.risk-中{color:#ebc16b}.risk-低{color:#7de0c3}
    @media(max-width:1000px){.layout{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}.toolbar{grid-template-columns:1fr 100px 100px}.toolbar .pdf{grid-column:1/-1}.scroll{max-height:520px}}@media(max-width:650px){main{padding:14px}.hide-sm{display:none}.toolbar{grid-template-columns:1fr 1fr}.toolbar select{grid-column:1/-1}}
  </style>
</head>
<body>
<main>
  <h1>实股机会筛选</h1>
  <div class="sub">第一层：从统一股票池筛选中短线机会质量。期权数据仅作为辅助证据，不等同于买入信号。</div>
  <div class="framework">当前股票池已重构为 10 个指数池：SPX、IXIC、DJIA、NDX、RUT、RUA、MID、SML、W5000、SOX。各模块统一从这些指数池中选择，不再混用旧主题池。</div>
  <div class="toolbar">
    <select id="universe"></select>
    <select id="limit"><option value="10">Top 10</option><option value="20" selected>Top 20</option><option value="50">Top 50</option></select>
    <select id="pullbackFilter"><option value="all">全部信号</option><option value="pullback">仅看回调承接</option><option value="strong">仅看 STRONG_WATCH</option><option value="near">距离突破价较近</option><option value="invalidated">仅看已失效</option></select>
    <button class="update" id="update">更新所选股票池</button>
    <button class="primary" id="load">刷新信号</button>
    <a class="pdf" id="pdf" href="#" target="_blank"><button>下载底层 PDF</button></a>
  </div>
  <section class="metrics" id="metrics"></section>
  <section class="layout">
    <div class="panel"><div class="panel-head"><h2>机会观察清单</h2><span class="muted" id="stamp"></span></div><div class="scroll"><table><thead><tr><th>标的</th><th>机会层级</th><th>回调承接</th><th>来源池</th><th>现价</th><th>趋势 / 接力涨幅</th><th class="hide-sm">走势</th><th>机会分</th><th>风险</th></tr></thead><tbody id="rows"></tbody></table></div></div>
    <aside class="panel"><div class="panel-head"><h2>信号详情</h2></div><div class="detail" id="detail"><div class="empty">点击左侧标的查看研究依据。</div></div></aside>
  </section>
  <div class="note" id="note"></div>
</main>
<script>
const $=id=>document.getElementById(id), esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct=v=>(Number(v||0)*100).toFixed(1)+"%", money=v=>"$"+Number(v||0).toFixed(2);
let current=[], selected="", latestReports={}, updateTimer=null;
function universeOptions(items){let groups=[];(items||[]).forEach(x=>{let key=x.group_label||"其他股票池",g=groups.find(y=>y.key===key);if(!g){g={key,items:[]};groups.push(g)}g.items.push(x)});return groups.map(g=>`<optgroup label="${esc(g.key)}">${g.items.map(x=>{let r=latestReports[x.universe];return `<option value="${esc(x.universe)}">${esc(x.label)} · ${r?esc(r.ticker_count)+"只":"尚未生成报告"} · ${esc(x.tier)} · 成本${esc(x.run_cost||"中")}</option>`}).join("")}</optgroup>`).join("")}
function spark(values){if(!values||values.length<2)return "-";let mn=Math.min(...values),mx=Math.max(...values),span=mx-mn||1, pts=values.map((v,i)=>`${(i/(values.length-1)*100).toFixed(1)},${(26-(v-mn)/span*22).toFixed(1)}`).join(" ");return `<svg class="spark" viewBox="0 0 100 28" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="#43c6b7" stroke-width="2"/></svg>`}
function metrics(d){return [["股票池",d.universe_label||d.universe],["扫描标的",d.ticker_count],["有效结果",d.valid_tickers],["展示候选",d.bullish_count],["运行时间",(d.timestamp||"").slice(0,16).replace("T"," ")]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><b>${esc(x[1])}</b></div>`).join("")}
function pullbackStageText(s){return s==="STRONG_WATCH"?"强回调承接":s==="WATCH"?"回调承接":s==="INVALIDATED"?"已失效":s||"--"}
function pullbackConfirmText(s){return s==="STRONG_CONFIRMED"?"强确认":s==="CONFIRMED"?"已确认":s==="WEAK_CONFIRMED"?"弱确认":s==="WAITING_CONFIRMATION"?"等待确认":s==="INVALIDATED"?"已失效":"--"}
function pullbackCell(r){let pr=r.pullback_rejection||{},pc=r.pullback_confirmation||{};let stage=pc.signal_stage&&pc.signal_stage!=="NONE"?pullbackConfirmText(pc.signal_stage):pullbackStageText(pr.signal_stage);return `<div class="stack"><span class="badge ${pr.signal_stage==="STRONG_WATCH"||pc.signal_stage==="STRONG_CONFIRMED"?"strong":"watch"}">${esc(stage)}</span><small class="muted">突破 ${money(pc.waiting_breakout_price||pr.waiting_breakout_price)}</small><small class="muted">失效 ${money(pc.invalidation_price||pr.invalidation_price)}</small></div>`}
function pullbackDetail(r){let pr=r.pullback_rejection||{},pc=r.pullback_confirmation||{};if(!pr.signal_stage&&!pc.signal_stage)return "";return `<div class="section"><b>回调承接</b><div class="detail-grid"><div class="kv"><span>第一层状态</span><b>${esc(pullbackStageText(pr.signal_stage))}</b></div><div class="kv"><span>第二层确认</span><b>${esc(pullbackConfirmText(pc.signal_stage))}</b></div><div class="kv"><span>等待突破价</span><b>${money(pc.waiting_breakout_price||pr.waiting_breakout_price)}</b></div><div class="kv"><span>失效价</span><b>${money(pc.invalidation_price||pr.invalidation_price)}</b></div><div class="kv"><span>最近支撑</span><b>${money(pr.nearest_support)}</b></div><div class="kv"><span>评分</span><b>${Number(pr.score||pc.confirmation_score||0).toFixed(1)}</b></div></div><div class="muted" style="margin-top:8px">${esc(pc.human_readable_reason||pr.human_readable_reason||"等待后续确认。")}</div></div>`}
function filterSignals(rows){let mode=$("pullbackFilter")?.value||"all";return rows.filter(r=>{let pr=r.pullback_rejection||{},pc=r.pullback_confirmation||{};if(mode==="pullback")return pr.signal_stage==="WATCH"||pr.signal_stage==="STRONG_WATCH"||pc.signal_stage==="WAITING_CONFIRMATION";if(mode==="strong")return pr.signal_stage==="STRONG_WATCH";if(mode==="invalidated")return pc.signal_stage==="INVALIDATED";if(mode==="near"){let spot=Number(r.spot||0),bo=Number(pc.waiting_breakout_price||pr.waiting_breakout_price||0);return spot>0&&bo>0&&Math.abs(bo-spot)/spot<=0.03}return pc.signal_stage!=="INVALIDATED"}).sort((a,b)=>{let rank=s=>s==="STRONG_CONFIRMED"?5:s==="CONFIRMED"?4:s==="WAITING_CONFIRMATION"?3:s==="STRONG_WATCH"?2:s==="WATCH"?1:s==="INVALIDATED"?-5:0;let ra=rank((a.pullback_confirmation||{}).signal_stage)||rank((a.pullback_rejection||{}).signal_stage),rb=rank((b.pullback_confirmation||{}).signal_stage)||rank((b.pullback_rejection||{}).signal_stage);return rb!==ra?rb-ra:Number(b.opportunity_score||0)-Number(a.opportunity_score||0)})}
function details(r){let risks=[...(r.risk_flags||[]),...(r.rejection_reasons||[])],p=r.peer_earnings;let evidence=p?`<div class="section"><b>同行财报接力证据</b><div class="muted">${esc(p.leader_symbol)} 已先发 · ${esc(p.group_label)}；配对相似度 ${pct(p.peer_similarity)}；模型研究概率 ${pct(p.research_probability)}；相对滞涨差 ${pct(p.relative_lag_gap)}。该概率仅用于专项排序，不是实盘胜率。</div></div>`:`<div class="section"><b>期权辅助证据</b><div class="muted">${esc(r.option_evidence.strategy_desc||r.option_evidence.strategy)}；模型胜率 ${pct(r.option_evidence.pop)}；流动性 ${esc(r.liquidity_grade)} / ${Number(r.liquidity_score).toFixed(2)}</div></div>`;return `<h3>${esc(r.ticker)} <span class="badge ${esc(r.signal_tone)}">${esc(r.signal)}</span></h3><div class="detail-grid"><div class="kv"><span>候选类型</span><b>${esc(r.candidate_label||"严格候选")}</b></div><div class="kv"><span>现价</span><b>${money(r.spot)}</b></div><div class="kv"><span>参考观察区</span><b>${money(r.entry_zone_low)} - ${money(r.entry_zone_high)}</b></div><div class="kv"><span>机会评分</span><b>${Number(r.opportunity_score).toFixed(1)}</b></div><div class="kv"><span>风险级别</span><b class="risk-${esc(r.risk_level)}">${esc(r.risk_level)}</b></div><div class="kv"><span>${p?"接力窗口涨幅":"30日趋势"}</span><b>${pct(r.trend_30d)}</b></div><div class="kv"><span>底层研究分</span><b>${Number(r.research_score).toFixed(1)}</b></div><div class="kv"><span>来源池</span><b>${esc((r.source_pools||[]).join(" / "))}</b></div></div>${evidence}${pullbackDetail(r)}<div class="section"><b>风险与复核项</b>${risks.length?`<ul class="list">${risks.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:`<div class="muted">暂无额外风险标记。</div>`}</div>`}
function render(d){current=filterSignals(d.signals||[]);$("metrics").innerHTML=metrics(d);$("stamp").textContent="Run "+d.run_id;$("pdf").href=d.report_pdf_url||"#";$("pdf").style.display=d.report_pdf_url?"":"none";$("note").textContent=d.method_note;$("rows").innerHTML=current.map(r=>`<tr data-t="${esc(r.ticker)}"><td><b>${esc(r.ticker)}</b></td><td><span class="badge ${esc(r.signal_tone)}">${esc(r.signal)}</span></td><td>${pullbackCell(r)}</td><td class="sources">${esc((r.source_pools||[]).join(" / "))}</td><td>${money(r.spot)}</td><td>${pct(r.trend_30d)}</td><td class="hide-sm">${spark(r.recent_closes)}</td><td>${Number(r.opportunity_score).toFixed(1)}</td><td class="risk-${esc(r.risk_level)}">${esc(r.risk_level)}</td></tr>`).join("")||`<tr><td colspan="9" class="empty">当前没有达到门槛的机会候选。</td></tr>`;document.querySelectorAll("tbody tr[data-t]").forEach(tr=>tr.onclick=()=>{document.querySelectorAll("tbody tr").forEach(x=>x.classList.remove("active"));tr.classList.add("active");selected=tr.dataset.t;let r=current.find(x=>x.ticker===selected);$("detail").innerHTML=details(r)});let first=document.querySelector("tbody tr[data-t]");if(first)first.click()}
function empty(message){current=[];$("metrics").innerHTML="";$("stamp").textContent="";$("pdf").href="#";$("pdf").style.display="none";$("detail").innerHTML=`<div class="empty">该股票池还没有可展示的信号详情。</div>`;$("rows").innerHTML=`<tr><td colspan="9" class="empty">${esc(message)}</td></tr>`}
async function loadReports(){let selectedUniverse=$("universe").value,d=await fetch("/stock-signals/reports").then(r=>r.json());latestReports={};(d.reports||[]).forEach(r=>{if(!latestReports[r.universe])latestReports[r.universe]=r});let catalog=d.supported_universes||[];$("universe").innerHTML=universeOptions(catalog);if(selectedUniverse&&catalog.some(x=>x.universe===selectedUniverse))$("universe").value=selectedUniverse;applyUniverseMode()}
async function load(){let u=$("universe").value,l=$("limit").value;$("rows").innerHTML=`<tr><td colspan="9" class="empty">正在载入最近一次完成的筛选结果...</td></tr>`;let r=await fetch(`/stock-signals/latest?universe=${encodeURIComponent(u)}&limit=${l}`);if(!r.ok){let d=await r.json();throw new Error(d.detail||"加载失败")}render(await r.json())}
async function pollUpdate(jobId){let r=await fetch(`/stock-signals/update/${encodeURIComponent(jobId)}`),d=await r.json();if(!r.ok)throw new Error(d.detail||"无法读取更新进度");$("note").textContent=`${d.message} 进度 ${(Number(d.progress||0)*100).toFixed(0)}%`;if(d.status==="completed"){clearTimeout(updateTimer);$("update").disabled=false;await loadReports();await load();return}if(d.status==="failed"){clearTimeout(updateTimer);$("update").disabled=false;throw new Error(d.message)}updateTimer=setTimeout(()=>pollUpdate(jobId).catch(showError),1500)}
function showError(e){empty(e.message);$("note").textContent=e.message}
function applyUniverseMode(){let peer=$("universe").value==="speculative_peer_earnings";$("update").textContent=peer?"运行专项接力扫描":"更新所选股票池";$("pdf").style.display=peer?"none":""}
async function update(){let u=$("universe").value;$("update").disabled=true;$("note").textContent=u==="speculative_peer_earnings"?"正在创建专项接力扫描任务...":"正在创建更新任务...";let r=await fetch("/stock-signals/update",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({universe:u})}),d=await r.json();if(!r.ok)throw new Error(d.detail||"更新启动失败");pollUpdate(d.job_id).catch(showError)}
$("load").onclick=()=>load().catch(showError);
$("update").onclick=()=>update().catch(e=>{$("update").disabled=false;showError(e)});
$("universe").onchange=()=>{applyUniverseMode();load().catch(showError)};
$("pullbackFilter").onchange=()=>load().catch(showError);
loadReports().then(load).catch(e=>$("rows").innerHTML=`<tr><td colspan="9" class="empty">${esc(e.message)}</td></tr>`);
</script>
</body>
</html>
        """
    )


@app.get("/signal-dashboard-tool", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def signal_dashboard_tool():
    """Embedded three-dimensional equity research overview."""
    return HTMLResponse(signal_dashboard_html())

    return HTMLResponse(
        """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>easymoneysniper · 三维信号总览</title><style>
:root{color-scheme:dark;--bg:#09131b;--panel:#101e29;--line:#263947;--muted:#91a5b2;--text:#eef5f7;--good:#6dd7b2;--wait:#e9b85d;--bad:#f07b78;--gold:#d5ad5c}
*{box-sizing:border-box}body{margin:0;padding:24px;background:var(--bg);color:var(--text);font:13px/1.45 Inter,"Microsoft YaHei",sans-serif}h1{margin:0;font-size:22px}p{margin:5px 0 12px;color:var(--muted)}.brand{display:flex;align-items:center;gap:9px;color:var(--gold);font-size:11px;font-weight:800;letter-spacing:1px;text-transform:uppercase;margin-bottom:7px}.ball{display:inline-grid;place-items:center;width:24px;height:24px;border:2px solid var(--gold);border-radius:50%;font-size:9px;letter-spacing:0}.flow{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}.flow span{padding:5px 8px;border:1px solid var(--line);border-radius:5px;background:#102532;color:#bcd7db;font-size:12px}.toolbar{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px}select,button{min-height:40px;padding:9px 13px;border:1px solid var(--line);border-radius:6px;background:#152632;color:var(--text);white-space:nowrap}select{min-width:300px}button{cursor:pointer;background:#16635e;font-weight:700}button.secondary{background:#152632}button.agent{background:#8a5b18;border-color:#b47d2d}button:disabled{opacity:.55;cursor:not-allowed}.progress{display:none;margin:0 0 14px;padding:12px 14px;border:1px solid var(--line);border-radius:7px;background:#102532}.progress.show{display:block}.progress-head{display:flex;justify-content:space-between;gap:12px;margin-bottom:8px}.bar{height:7px;border-radius:9px;background:#203641;overflow:hidden}.bar i{display:block;height:100%;background:#55c5b5;transition:width .25s}.summary{margin-top:8px;color:var(--muted);font-size:12px}.panel{border:1px solid var(--line);border-radius:7px;background:var(--panel);overflow-y:auto;overflow-x:hidden;max-height:calc(100vh - 240px)}table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{padding:9px 8px;border-bottom:1px solid #20333f;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{color:#b9cbd3;background:#12232e;font-size:11px;position:sticky;top:0;z-index:1}
tr.highlight{background:#1a3a2a !important;border-left:3px solid var(--good)}
tr.highlight td{color:#fff}
.lamp{display:flex;align-items:center;gap:5px}.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none}.good{background:var(--good)}.wait{background:var(--wait)}.bad{background:var(--bad)}.muted{color:var(--muted)}.stack{display:grid;gap:4px}.stack b,.stack small{display:block}.stack small{color:var(--muted);line-height:1.3}.relay{color:#f1d48e}.relay b{color:#f1d48e}.score{color:#82e4c4;font-size:15px}a{color:#8fc0ff;text-decoration:none}.empty{padding:28px;text-align:center;color:var(--muted)}.notice{margin:12px 2px 0;color:var(--muted);font-size:12px}@media(max-width:1000px){body{padding:14px;font-size:12px}th,td{padding:7px 5px}.panel{max-height:calc(100vh - 225px)}}
</style></head><body><div class="brand"><span class="ball">35</span> easymoneysniper · research board</div><h1>三维量化信号总览</h1><p>胜率口径排序：历史验证胜率优先 → 统一研究概率 → 风险收益比 → 可执行性。所有候选都在一张总表中完成复核。</p>
<div class="flow"><span>第一层 · 机会质量</span><span>第二层 · 启动择时 + 日隧道协商</span><span>第三层 · 事件风险</span><span>汇总 · 统一证据与失效退出</span></div>
<div class="toolbar"><select id="universe"></select><button class="agent" id="run">运行三层分析</button><button class="secondary" id="load">刷新总览</button></div>
<div class="progress" id="progress"><div class="progress-head"><b id="phase">准备运行</b><span id="pct">0%</span></div><div class="bar"><i id="bar" style="width:0%"></i></div><div class="summary" id="summary">依次更新机会筛选、运行启动择时，并汇总个股决策卡片。</div></div>
<div class="panel"><table><colgroup><col style="width:8%"><col style="width:20%"><col style="width:13%"><col style="width:10%"><col style="width:10%"><col style="width:11%"><col style="width:10%"><col style="width:13%"><col style="width:5%"></colgroup><thead><tr><th>标的</th><th>同行财报接力</th><th>日隧道协商</th><th>检验筹码</th><th>统一证据</th><th>三层状态</th><th>失效退出</th><th>研究建议</th><th>操作</th></tr></thead><tbody id="rows"><tr><td colspan="9" class="empty">正在载入...</td></tr></tbody></table></div>
<div class="notice">排序分是研究胜算分：历史验证胜率、统一研究概率、风险收益比和可执行性分层计算；不等于实盘确定胜率。</div>
<script>
const $=id=>document.getElementById(id),esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct=v=>(Number(v||0)*100).toFixed(1)+"%";
const money=v=>v==null?"--":"$"+Number(v).toFixed(2);
const cap=v=>{let n=Number(v||0);return !n?"--":n>=1e12?"$"+(n/1e12).toFixed(1)+"T":n>=1e9?"$"+(n/1e9).toFixed(1)+"B":"$"+(n/1e6).toFixed(0)+"M"};
const pe=v=>v==null?"--":Number(v).toFixed(1)+"x";
function lamp(ok,label){return `<span class="lamp"><i class="dot ${ok?"good":"wait"}"></i>${esc(label)}</span>`}
function universeOptions(items){let groups=[];(items||[]).forEach(x=>{let key=x.group_label||"其他股票池",g=groups.find(y=>y.key===key);if(!g){g={key,items:[]};groups.push(g)}g.items.push(x)});return groups.map(g=>`<optgroup label="${esc(g.key)}">${g.items.map(x=>`<option value="${esc(x.universe)}">${esc(x.label)} · ${esc(x.tier)} · 成本${esc(x.run_cost||"中")}</option>`).join("")}</optgroup>`).join("")}
function getPoolType(u){if(u==="pre_earnings_revision")return"pre";if(u==="speculative_peer_earnings")return"peer";return"std"}
function updateHeader(t){var tb=document.querySelector("#rows");if(!tb)return;var th=tb.closest("table").querySelector("thead");if(!th)return;var h="";if(t==="pre"){h="<tr><th>标的</th><th>预增信号</th><th>日隧道协商</th><th>检验筹码</th><th>统一证据</th><th>三层状态</th><th>研究建议</th><th>操作</th></tr>"}else if(t==="peer"){h="<tr><th>标的</th><th>同行财报接力</th><th>日隧道协商</th><th>检验筹码</th><th>统一证据</th><th>三层状态</th><th>研究建议</th><th>操作</th></tr>"}else{h="<tr><th>标的</th><th>机会质量</th><th>日隧道协商</th><th>检验筹码</th><th>统一证据</th><th>三层状态</th><th>研究建议</th><th>操作</th></tr>"}th.innerHTML=h}
function render(d){var universe=$("universe").value;var pt=getPoolType(universe);updateHeader(pt);$("rows").innerHTML=(d.rows||[]).map(c=>{let o=c.opportunity||{},t=c.timing||{},u=t.daily_tunnel||{},p=c.peer_earnings,e=c.event||{},x=c.dimensions||{},h=c.hypothesis_test||{},r=c.research_evidence||{},pe2=c.pre_earnings||{};let dateNote=p?.target_report_date_requires_manual_check?"披露日待人工确认":"披露日 "+(p?.target_report_date||"--");let targetPrice=p?.target_latest_price??o.spot,targetCap=p?.target_market_cap??o.market_cap,targetPe=p?.target_trailing_pe??o.trailing_pe??o.forward_pe;let capRatio=p?.leader_to_target_market_cap_ratio==null?"--":Number(p.leader_to_target_market_cap_ratio).toFixed(2)+"x";let col1Html="";if(pt==="pre"&&pe2&&pe2.report){var dd=pe2.report.trading_days_to_report||0;var iw=pe2.in_target_window||false;var sc=pe2.score?pe2.score.final||0:0;var td2=pe2.report.target_date||"--";col1Html='<div class="stack"><b class="score '+(iw?"good":"wait")+'">'+(iw?"IN":"远期")+'</b><small>'+esc(td2)+' '+dd+'天</small><small>评分 '+pct(sc)+'</small></div>'}else if(pt==="pre"){col1Html='<div class="stack"><b class="wait">无预增</b></div>'}else if(p){col1Html='<div class="stack"><b>'+esc(p.leader_symbol)+' 先发</b><small>'+pct(p.peer_similarity)+' 配对</small><small>'+pct(p.relative_lag_gap)+' 滞涨</small></div>'}else{col1Html='<div class="stack"><b class="wait">无</b></div>'}var hlCls=(pe2&&pe2.in_target_window)?' class="highlight"':'';return `<tr${hlCls}><td><div class="stack"><b>${esc(c.symbol)}</b><small>${money(targetPrice)}</small><small>市值 ${cap(targetCap)}<br>PE ${pe(targetPe)}</small></div></td><td>${col1Html}</td><td><div class="stack">${p?`<b>${esc(p.leader_symbol)} 先发 · ${esc(p.group_label)}</b><small>${esc(p.sublane_relation_display||p.sublane_relation||"同行")}</small><small>配对 ${pct(p.peer_similarity)} · ${esc(dateNote)}</small><small>${esc(p.transmission_direction||"同行传导")} · 龙头/标的市值 ${esc(capRatio)}</small><small>同行 ${cap(p.leader_market_cap)} · PE ${pe(p.leader_trailing_pe)}</small><small>财报前 ${money(p.leader_pre_report_price)} → 现价 ${money(p.leader_latest_price)} · 实际 ${pct(p.leader_post_report_return)}</small><small>标的 ${money(p.target_start_price)} → ${money(p.target_latest_price)} · 实际 ${pct(p.target_return)}</small><small>模型预期补涨 ${pct(p.expected_catch_up_return)} → ${money(p.expected_target_price)}</small>`:"<b>暂无高质量同行接力</b><small>等待子赛道、客群、规模方向与风格匹配证据</small>"}</div></td><td><div class="stack"><b class="score">${u.score==null?"--":Number(u.score).toFixed(1)+" 分"}</b><small>${esc(u.label||"尚未计算")}</small><small>${esc(u.current_zone||"--")}</small><small>拐点 ${pct(u.turning_point_score)} · 当前上行 ${esc(u.current_up_cycle_days??0)} 日</small><small>最长上行 ${esc(u.longest_up_cycle_days??0)} 日 · 上行力量 ${pct(u.up_cycle_power)}</small></div></td><td><div class="stack">${h.eligible?`<b>${esc(h.test_shares)} 股 · ${esc(h.sizing_mode==="micro_probe"?"微型试仓":"研究试仓")}</b><small>占用 ${esc("$"+Number(h.capital_required||0).toFixed(0))} · 最大检验风险 ${esc("$"+Number(h.max_test_loss||0).toFixed(0))}</small><small>${esc(h.reason||"")}</small>`:`<b>${esc(h.sizing_mode==="paper_track"?"纸面跟踪":"暂不试仓")}</b><small>${esc(h.reason||"等待证据")}</small>`}</div></td><td><div class="stack"><b class="score">${pct(r.probability)}</b><small>区间 ${pct(r.probability_low)} - ${pct(r.probability_high)}</small><small>胜算分 ${pct(r.win_sort?.sort_score)} · 历史 ${pct(r.win_sort?.historical_win_rate)}</small><small>${esc(r.win_sort?.historical_source||"历史代理")} · 风报比 ${Number(r.win_sort?.risk_reward||0).toFixed(2)} · 执行 ${pct(r.win_sort?.execution_score)}</small><small>${esc((r.components||[]).map(x=>x.label).join(" + ")||"证据不足")}</small></div></td><td><div class="stack">${lamp(x.opportunity,`机会 ${Number(o.opportunity_score||0).toFixed(1)}`)}${lamp(x.timing,t.signal_cn||"等待启动")}${lamp(Boolean(u.sample_days),u.label||"日线待算")}${lamp(x.peer_earnings,p?"同行接力":"无接力")}${lamp(x.event_clear,e.signal_cn||e.signal||"暂无告警")}</div></td><td><div class="stack"><b>${h.invalidation_price?esc("$"+Number(h.invalidation_price).toFixed(2)):"--"}</b><small>${esc(h.exit_rule||"--")}</small></div></td><td class="muted"><div class="stack"><b>${esc(c.decision_grade)}</b><small>${esc(c.decision)}</small><small>${esc(h.reason||"")}</small></div></td><td><a href="/daily-stock-analysis?symbol=${encodeURIComponent(c.symbol)}" target="_top">复核</a></td></tr>`}).join("")||`<tr><td colspan="9" class="empty">该股票池尚无机会候选，请先在“实股信号”中更新股票池。</td></tr>`}
async function loadUniverses(){let d=await fetch("/research-universes").then(r=>r.json());$("universe").innerHTML=universeOptions(d.universes||[])}
async function load(){let r=await fetch(`/research-signal-hub?universe=${encodeURIComponent($("universe").value)}&limit=100`),d=await r.json();if(!r.ok)throw new Error(d.detail||"加载失败");render(d)}
function showProgress(d){$("progress").classList.add("show");$("phase").textContent=d.message||"正在运行...";$("pct").textContent=`${Math.round(Number(d.progress||0)*100)}%`;$("bar").style.width=`${Math.round(Number(d.progress||0)*100)}%`;if(d.result){let g=d.result.decision_grades||{},fresh=`行情${d.result.market_evidence_refreshed?"已更新":"复用缓存"} · 公告/新闻${d.result.event_evidence_refreshed?"已更新":"复用缓存"}`;$("summary").textContent=`${fresh} · 机会候选 ${d.result.candidate_count||0} 只 · 启动信号 ${d.result.timing_signal_count||0} 只 · 同行接力 ${d.result.peer_earnings_signal_count||0} 只 · 决策卡片 ${d.result.decision_count||0} 张 · ${Object.entries(g).map(([k,v])=>`${k} ${v}`).join(" / ")}`}}
async function poll(jobId){let r=await fetch(`/research-signal-hub/run/${encodeURIComponent(jobId)}`),d=await r.json();if(!r.ok)throw new Error(d.detail||"无法读取分析进度");showProgress(d);if(d.status==="completed"){$("run").disabled=false;await load();return}if(d.status==="failed"){$("run").disabled=false;throw new Error(d.message)}setTimeout(()=>poll(jobId).catch(showError),1500)}
function showError(e){$("run").disabled=false;$("progress").classList.add("show");$("phase").textContent=e.message||"运行失败";$("pct").textContent="";$("bar").style.width="100%"}
async function run(){let universe=$("universe").value;$("run").disabled=true;$("progress").classList.add("show");$("phase").textContent="三维研究 Agent 正在启动...";$("summary").textContent="超过 24 小时的行情与公告/新闻事件会自动更新；仍在有效期内的证据将复用缓存。";let r=await fetch("/research-signal-hub/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({universe,refresh_evidence:true,top:100})}),d=await r.json();if(!r.ok)throw new Error(d.detail||"无法启动三层分析");poll(d.job_id).catch(showError)}
$("load").onclick=()=>load().catch(e=>$("rows").innerHTML=`<tr><td colspan="8" class="empty">${esc(e.message)}</td></tr>`);
$("run").onclick=()=>run().catch(showError);
loadUniverses().then(load).catch(e=>$("rows").innerHTML=`<tr><td colspan="8" class="empty">${esc(e.message)}</td></tr>`);
</script></body></html>
        """
    )


@app.post("/research-chain/run", dependencies=[Depends(require_auth)])
async def run_research_chain(payload: ResearchChainRunRequest, http_request: Request):
    """Start the five-layer research chain as a Swarm run."""
    symbol = re.sub(r"[^A-Za-z0-9._\\-]", "", payload.symbol.upper()).strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    preset_name = payload.preset_name or "five_layer_research_chain"
    if preset_name not in {"five_layer_research_chain", "quality_investment_panel"}:
        raise HTTPException(status_code=400, detail="unsupported research chain preset")

    runtime = _get_swarm_runtime()
    upstream_context = payload.goal or (
        f"Source universe: {payload.universe}. Use the latest Vibe-Trading three-layer signal context "
        f"if available. Treat this as research support, not investment advice."
    )
    try:
        run = runtime.start_run(
            preset_name,
            {
                "target": symbol,
                "market": payload.market,
                "goal": upstream_context,
                "context": upstream_context,
            },
            include_shell_tools=_shell_tools_enabled_for_request(http_request),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": run.id,
        "status": run.status.value,
        "preset_name": run.preset_name,
        "target": symbol,
        "swarm_url": f"/swarm/runs/{run.id}",
    }


@app.get("/runs/{run_id}/code", dependencies=[Depends(require_auth)])
async def get_run_code(run_id: str):
    """Return strategy source files for a run.

    Args:
        run_id: Run identifier.

    Returns:
        Map filename -> source text.
    """
    _validate_path_param(run_id, "run_id")
    base_dir = RUNS_DIR / run_id
    run_dir = base_dir / "code"
    if not base_dir.exists():
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    result = {}
    candidates = []
    if run_dir.exists():
        candidates.extend(sorted(run_dir.glob("*.py")))
    candidates.extend(sorted(base_dir.glob("*.py")))
    for p in candidates:
        if not p.exists() or p.stat().st_size > 512_000:
            continue
        name = p.relative_to(base_dir).as_posix()
        result[name] = p.read_text(encoding="utf-8", errors="replace")
    return result


@app.get("/runs/{run_id}/pine", dependencies=[Depends(require_auth)])
async def get_run_pine(run_id: str):
    """Return Pine Script file for a run.

    Args:
        run_id: Run identifier.

    Returns:
        Object with pine script content and exists flag.
    """
    _validate_path_param(run_id, "run_id")
    pine_path = RUNS_DIR / run_id / "artifacts" / "strategy.pine"
    if not pine_path.exists():
        return {"exists": False, "content": None}
    return {
        "exists": True,
        "content": pine_path.read_text(encoding="utf-8"),
    }


@app.get("/runs/{run_id}/report.pdf", dependencies=[Depends(require_auth)])
async def get_run_report_pdf(run_id: str, download: bool = Query(True)):
    """Generate and return a downloadable PDF report for a run."""
    _validate_path_param(run_id, "run_id")
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    pdf_path = _ensure_run_report_pdf(run_dir)
    disposition = "attachment" if download else "inline"
    filename = pdf_path.name if pdf_path.name != "report.pdf" else f"{run_id}_report.pdf"
    return FileResponse(
        str(pdf_path),
        media_type="application/pdf",
        filename=filename,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@app.get("/runs/{run_id}", response_model=RunResponse, dependencies=[Depends(require_auth)])
async def get_run_result(run_id: str):
    """Fetch full details for a historical run by ``run_id``."""
    _validate_path_param(run_id, "run_id")
    run_dir = RUNS_DIR / run_id

    if not run_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found"
        )

    response = _build_response_from_run_dir(run_dir, elapsed=0.0, include_analysis=True)

    return response


@app.get("/runs", response_model=List[RunInfo], dependencies=[Depends(require_auth)])
async def list_runs(limit: int = 20):
    """List recent runs with summary fields."""
    limit = min(max(1, limit), 100)
    runs_dir = RUNS_DIR
    
    if not runs_dir.exists():
        return []
    
    run_dirs = sorted(
        [d for d in runs_dir.iterdir() if d.is_dir()],
        key=lambda x: x.name,
        reverse=True
    )
    
    results = []
    for d in run_dirs[:limit]:
        run_id = d.name
        
        # Status from state.json or artifacts
        status_val = "unknown"
        state_file = _load_json_file(d / "state.json")
        if state_file:
            status_val = str(state_file.get("status") or "unknown").lower()
        elif (d / "artifacts" / "equity.csv").exists():
            status_val = "success"
        elif (d / "review_report.json").exists():
            status_val = "success"
        
        # Parse created_at from run_id (YYYYMMDD_HHMMSS or run_YYYYMMDD_HHMMSS)
        created_at = "Unknown"
        if run_id.startswith("run_"):
            parts = run_id.split('_')
            if len(parts) >= 3:
                d_str, t_str = parts[1], parts[2]
                if len(d_str) == 8 and len(t_str) == 6:
                    created_at = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:8]} {t_str[:2]}:{t_str[2:4]}:{t_str[4:6]}"
        elif "_" in run_id:
            parts = run_id.split('_')
            if len(parts) >= 2:
                d_str, t_str = parts[0], parts[1]
                if len(d_str) == 8 and len(t_str) == 6:
                    created_at = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:8]} {t_str[:2]}:{t_str[2:4]}:{t_str[4:6]}"
        
        if created_at == "Unknown":
            mtime = datetime.fromtimestamp(d.stat().st_mtime)
            created_at = mtime.strftime("%Y-%m-%d %H:%M:%S")
        
        prompt = None
        req_file = d / "req.json"
        planner_file = d / "planner_output.json"
        if req_file.exists():
            try:
                req_data = json.loads(req_file.read_text(encoding="utf-8"))
                prompt = req_data.get("prompt")
            except (json.JSONDecodeError, OSError):
                pass

        if not prompt and planner_file.exists():
            try:
                planner_data = json.loads(planner_file.read_text(encoding="utf-8"))
                prompt = planner_data.get("user_goal") or planner_data.get("goal")
            except (json.JSONDecodeError, OSError):
                pass
            
        if not prompt:
            prompt_file = d / "user_prompt.txt"
            if prompt_file.exists():
                prompt = prompt_file.read_text(encoding="utf-8").strip()
        
        total_return = None
        sharpe = None
        metrics_file = d / "artifacts" / "metrics.csv"
        if metrics_file.exists():
            try:
                import csv
                with open(metrics_file, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        total_return = float(row.get('total_return', 0) or 0)
                        sharpe = float(row.get('sharpe', 0) or 0)
                        break
            except (OSError, ValueError):
                pass
        
        run_context = load_run_context(d)
        results.append(RunInfo(
            run_id=run_id,
            status=status_val,
            created_at=created_at,
            prompt=prompt or "Manual Analysis",
            total_return=total_return,
            sharpe=sharpe,
            codes=run_context.get("codes") or [],
            start_date=run_context.get("start_date"),
            end_date=run_context.get("end_date"),
        ))
        
    return results


@app.get(
    "/settings/llm",
    response_model=LLMSettingsResponse,
    dependencies=[Depends(require_local_or_auth)],
)
async def get_llm_settings():
    """Return project-local LLM settings for the Web UI."""
    return _build_llm_settings_response()


@app.put("/settings/llm", response_model=LLMSettingsResponse, dependencies=[Depends(require_local_or_auth)])
async def update_llm_settings(payload: UpdateLLMSettingsRequest):
    """Persist project-local LLM settings and update the running process."""
    provider_name = payload.provider.strip().lower()
    provider = LLM_PROVIDER_BY_NAME.get(provider_name)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported LLM provider")

    model_name = payload.model_name.strip()
    if not model_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Model name is required")

    if payload.temperature < 0 or payload.temperature > 2:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Temperature must be between 0 and 2")

    reasoning_effort = (payload.reasoning_effort or "").strip().lower()
    if reasoning_effort not in LLM_REASONING_EFFORTS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reasoning effort must be low, medium, high, or max")

    current_values = _read_settings_env_values()
    for key in ("TUSHARE_TOKEN", "TWELVE_DATA_API_KEY", "FMP_API_KEY"):
        if os.environ.get(key):
            current_values[key] = os.environ[key]
    base_url = (payload.base_url if payload.base_url is not None else provider.default_base_url).strip()
    if provider.auth_type == "oauth":
        try:
            from src.providers.openai_codex import validate_codex_base_url

            base_url = validate_codex_base_url(base_url)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    updates: Dict[str, str] = {
        "LANGCHAIN_PROVIDER": provider.name,
        "LANGCHAIN_MODEL_NAME": model_name,
        provider.base_url_env: base_url,
        "LANGCHAIN_TEMPERATURE": str(payload.temperature),
        "TIMEOUT_SECONDS": str(payload.timeout_seconds),
        "MAX_RETRIES": str(payload.max_retries),
    }
    if reasoning_effort or "LANGCHAIN_REASONING_EFFORT" in current_values:
        updates["LANGCHAIN_REASONING_EFFORT"] = reasoning_effort

    if provider.api_key_env:
        if payload.clear_api_key:
            updates[provider.api_key_env] = ""
        elif payload.api_key is not None and payload.api_key.strip():
            api_key = payload.api_key.strip()
            updates[provider.api_key_env] = api_key if _is_configured_secret(api_key, LLM_API_KEY_PLACEHOLDERS) else ""
        elif provider.api_key_env in current_values and _is_configured_secret(
            current_values[provider.api_key_env],
            LLM_API_KEY_PLACEHOLDERS,
        ):
            updates[provider.api_key_env] = current_values[provider.api_key_env]
    elif payload.clear_api_key:
        os.environ.pop("OPENAI_API_KEY", None)

    _write_env_values(ENV_PATH, updates)
    _sync_runtime_env(provider, updates)
    return _build_llm_settings_response(_read_env_values(ENV_PATH))


@app.get(
    "/settings/data-sources",
    response_model=DataSourceSettingsResponse,
    dependencies=[Depends(require_local_or_auth)],
)
async def get_data_source_settings():
    """Return project-local data source credentials for the Web UI."""
    return _build_data_source_settings_response()


@app.put(
    "/settings/data-sources",
    response_model=DataSourceSettingsResponse,
    dependencies=[Depends(require_local_or_auth)],
)
async def update_data_source_settings(payload: UpdateDataSourceSettingsRequest):
    """Persist project-local data source credentials and update the running process."""
    current_values = _read_settings_env_values()
    updates: Dict[str, str] = {}

    if payload.clear_tushare_token:
        updates["TUSHARE_TOKEN"] = ""
    elif payload.tushare_token is not None and payload.tushare_token.strip():
        updates["TUSHARE_TOKEN"] = payload.tushare_token.strip()
    elif "TUSHARE_TOKEN" in current_values:
        updates["TUSHARE_TOKEN"] = current_values["TUSHARE_TOKEN"]
    for key, value, clear in (
        ("TWELVE_DATA_API_KEY", payload.twelve_data_api_key, payload.clear_twelve_data_api_key),
        ("FMP_API_KEY", payload.fmp_api_key, payload.clear_fmp_api_key),
    ):
        if clear:
            updates[key] = ""
        elif value is not None and value.strip():
            updates[key] = value.strip()
        elif key in current_values:
            updates[key] = current_values[key]

    if updates:
        _write_env_values(ENV_PATH, updates)
        token = updates.get("TUSHARE_TOKEN", "").strip()
        if _is_configured_secret(token, TUSHARE_TOKEN_PLACEHOLDERS):
            os.environ["TUSHARE_TOKEN"] = token
        else:
            os.environ.pop("TUSHARE_TOKEN", None)
        for key in ("TWELVE_DATA_API_KEY", "FMP_API_KEY"):
            value = updates.get(key, "").strip()
            if _is_configured_secret(value, MARKET_DATA_KEY_PLACEHOLDERS):
                os.environ[key] = value
            else:
                os.environ.pop(key, None)

    return _build_data_source_settings_response(_read_env_values(ENV_PATH))


@app.get("/soxl-quant/status", dependencies=[Depends(require_auth)])
async def get_soxl_quant_status():
    return soxl_quant_service.service_status()


@app.get("/soxl-quant/backtest/latest", dependencies=[Depends(require_auth)])
async def get_soxl_quant_latest(include_curve: bool = True, trade_limit: int = Query(default=100, ge=0, le=1000)):
    result = soxl_quant_service.latest_backtest(include_curve=include_curve, trade_limit=trade_limit)
    return {"available": result is not None, "result": result}


@app.post("/soxl-quant/backtest", dependencies=[Depends(require_auth)])
async def start_soxl_quant_backtest(
    force_download: bool = False,
    initial_capital: float = Query(default=2000.0, ge=100.0, le=1_000_000.0),
    source: str = Query(default="databento", pattern="^(databento|yfinance_proxy)$"),
):
    return soxl_quant_service.start_backtest_job(
        force_download=force_download,
        initial_capital=initial_capital,
        source=source,
    )


@app.post("/daily-three-layer-auto/finalize", dependencies=[Depends(require_auth)])
async def daily_three_layer_auto_finalize():
    """Retry price sync and snapshot persistence without restarting pool scans."""
    with _AUTO_SCAN_LOCK:
        if _AUTO_SCAN_JOB.get("status") in {"queued", "running"}:
            return _auto_scan_state()
        if _daily_active_worker_ids():
            return {**_auto_scan_state(), "blocked_by_active_worker": True,
                    "message": "上次超时任务正在退出，请稍后接续；未启动重叠扫描"}
        _AUTO_SCAN_JOB.update({"status": "queued", "message": "daily snapshot recovery queued", "progress": 0.0})
    threading.Thread(target=_run_daily_finalize_only, daemon=True).start()
    return _auto_scan_state()


@app.get("/soxl-quant/backtest/status", dependencies=[Depends(require_auth)])
async def get_soxl_quant_backtest_status():
    return soxl_quant_service.backtest_job_status()


@app.get("/soxl-quant/trades", dependencies=[Depends(require_auth)])
async def get_soxl_quant_trades(limit: int = Query(default=100, ge=1, le=1000)):
    return {"symbol": "SOXL", "paper_only": True, "items": soxl_quant_service.list_paper_trades(limit)}


@app.post("/soxl-quant/live/start", dependencies=[Depends(require_auth)])
async def start_soxl_quant_live():
    return soxl_quant_service.LIVE_ENGINE.start()


@app.post("/soxl-quant/live/stop", dependencies=[Depends(require_auth)])
async def stop_soxl_quant_live():
    return soxl_quant_service.LIVE_ENGINE.stop()


@app.get("/market-data/status", dependencies=[Depends(require_local_or_auth)])
async def get_market_data_status():
    """Return non-secret routing status for troubleshooting."""
    from market_data_service import data_source_status

    return data_source_status()


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Liveness probe."""
    return HealthResponse(
        status="healthy",
        service="easymoneysniper API",
        timestamp=datetime.now().isoformat()
    )


# Path written by scripts/start-feishu-mobile.ps1, exposed read-only into the
# container via the ``./.runtime`` bind mount (see docker-compose.yml).
_FEISHU_URL_FILE = Path(__file__).resolve().parent.parent / ".runtime" / "feishu-mobile-url.txt"
# cloudflared metrics server (Docker-network only) exposes the *live* quick-tunnel
# hostname. Reading this keeps the URL correct even if cloudflared self-restarts
# and Cloudflare hands out a new hostname (the file would otherwise go stale).
_CLOUDFLARED_QUICKTUNNEL_URL = "http://cloudflared:2000/quicktunnel"


def _live_tunnel_url() -> Optional[str]:
    """Read the current quick-tunnel URL from the cloudflared metrics endpoint."""
    try:
        with urllib.request.urlopen(_CLOUDFLARED_QUICKTUNNEL_URL, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        host = str(data.get("hostname") or "").strip().strip("/")
        if host.startswith("https://"):
            host = host[len("https://"):]
        if host.endswith(".trycloudflare.com"):
            return f"https://{host}"
    except Exception:  # noqa: BLE001 - metrics may be down; caller falls back
        return None
    return None


@app.get("/feishu-mobile-url")
async def feishu_mobile_url():
    """Return the current Cloudflare Quick Tunnel public URL for the Feishu
    mobile homepage.

    Prefers the *live* hostname from cloudflared's metrics endpoint (always
    current), and falls back to the runtime file the start script writes.
    Display-only convenience for the 任务中心 page. ``available: false`` when no
    tunnel is reachable.
    """
    live = _live_tunnel_url()
    if live:
        return {
            "available": True,
            "url": live,
            "mobile_url": f"{live.rstrip('/')}/m",
            "updated_at": datetime.now().isoformat(),
            "source": "live",
        }
    try:
        if not _FEISHU_URL_FILE.exists():
            return {"available": False, "url": None, "mobile_url": None, "updated_at": None}
        url = _FEISHU_URL_FILE.read_text(encoding="utf-8").strip()
        if not url.startswith("https://"):
            return {"available": False, "url": None, "mobile_url": None, "updated_at": None}
        updated_at = datetime.fromtimestamp(_FEISHU_URL_FILE.stat().st_mtime).isoformat()
        return {
            "available": True,
            "url": url,
            "mobile_url": f"{url.rstrip('/')}/m",
            "updated_at": updated_at,
            "source": "file",
        }
    except Exception as exc:  # noqa: BLE001 - never break the page over this
        return {"available": False, "url": None, "mobile_url": None, "updated_at": None, "error": str(exc)}


@app.get("/correlation")
async def get_correlation_matrix(
    codes: str = Query(..., description="Comma-separated asset codes, e.g. BTC-USDT,ETH-USDT,SPY"),
    days: int = Query(90, description="Lookback window in days", ge=7, le=365),
    method: str = Query("pearson", description="Correlation method: pearson or spearman"),
):
    """Compute cross-asset correlation matrix from daily returns.

    Fetches price data for each code via available data loaders,
    computes pairwise correlation of daily returns over the lookback window.
    """
    from backtest.correlation import compute_correlation_matrix

    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if len(code_list) < 2:
        raise HTTPException(status_code=400, detail="At least 2 asset codes required")
    if len(code_list) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 assets per request")
    if method not in ("pearson", "spearman"):
        raise HTTPException(status_code=400, detail="method must be 'pearson' or 'spearman'")

    try:
        result = compute_correlation_matrix(codes=code_list, days=days, method=method)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Correlation computation failed: {exc}")


def _terminate_current_process() -> None:
    """Stop the current API process after the response has been sent."""
    time.sleep(0.25)
    os.kill(os.getpid(), signal.SIGTERM)


@app.post("/system/shutdown", dependencies=[Depends(require_auth)])
async def shutdown_local_api(background_tasks: BackgroundTasks, request: Request):
    """Shut down the local API server when requested from loopback clients."""
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Local access only")

    background_tasks.add_task(_terminate_current_process)
    return {
        "status": "shutting-down",
        "service": "easymoneysniper API",
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/skills")
async def list_skills():
    """List registered skills (name and description)."""
    from src.agent.skills import SkillsLoader

    loader = SkillsLoader()
    return [
        {
            "name": s.name,
            "description": s.description,
        }
        for s in loader.skills
    ]


@app.get("/api")
async def api_info():
    """Service metadata."""
    return {
        "service": "easymoneysniper API",
        "version": "5.0.0",
        "docs": "/docs",
        "health": "/health",
    }


# ============================================================================
# Session API
# ============================================================================

_session_service = None


def _get_session_service():
    """Lazy-init session service when ENABLE_SESSION_RUNTIME=true."""
    global _session_service
    if _session_service is not None:
        return _session_service

    if os.getenv("ENABLE_SESSION_RUNTIME", "true").lower() != "true":
        return None

    import asyncio
    from src.session.store import SessionStore
    from src.session.events import EventBus
    from src.session.service import SessionService

    store = SessionStore(base_dir=SESSIONS_DIR)
    event_bus = EventBus()

    try:
        loop = asyncio.get_event_loop()
        event_bus.set_loop(loop)
    except RuntimeError:
        pass

    _session_service = SessionService(
        store=store,
        event_bus=event_bus,
        runs_dir=RUNS_DIR,
    )
    return _session_service


@app.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_auth)])
async def create_session(request: CreateSessionRequest):
    """Create a chat session."""
    svc = _get_session_service()
    if not svc:
        raise HTTPException(status_code=501, detail="Session runtime not enabled")
    session = svc.create_session(title=request.title, config=request.config)
    return SessionResponse(
        session_id=session.session_id,
        title=session.title,
        status=session.status.value,
        created_at=session.created_at,
        updated_at=session.updated_at,
        last_attempt_id=session.last_attempt_id,
    )


@app.get("/sessions", response_model=List[SessionResponse], dependencies=[Depends(require_auth)])
async def list_sessions(limit: int = Query(50, ge=1, le=200)):
    """List sessions."""
    svc = _get_session_service()
    if not svc:
        raise HTTPException(status_code=501, detail="Session runtime not enabled")
    sessions = svc.list_sessions(limit=limit)
    return [
        SessionResponse(
            session_id=s.session_id,
            title=s.title,
            status=s.status.value,
            created_at=s.created_at,
            updated_at=s.updated_at,
            last_attempt_id=s.last_attempt_id,
        )
        for s in sessions
    ]


@app.get("/sessions/{session_id}", response_model=SessionResponse, dependencies=[Depends(require_auth)])
async def get_session(session_id: str):
    """Get one session by id."""
    _validate_path_param(session_id, "session_id")
    svc = _get_session_service()
    if not svc:
        raise HTTPException(status_code=501, detail="Session runtime not enabled")
    session = svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return SessionResponse(
        session_id=session.session_id,
        title=session.title,
        status=session.status.value,
        created_at=session.created_at,
        updated_at=session.updated_at,
        last_attempt_id=session.last_attempt_id,
    )


@app.delete("/sessions/{session_id}", dependencies=[Depends(require_auth)])
async def delete_session(session_id: str):
    """Delete a session."""
    _validate_path_param(session_id, "session_id")
    svc = _get_session_service()
    if not svc:
        raise HTTPException(status_code=501, detail="Session runtime not enabled")
    deleted = svc.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return {"status": "deleted", "session_id": session_id}


class UpdateSessionRequest(BaseModel):
    """Session update fields."""
    title: Optional[str] = None


@app.patch("/sessions/{session_id}", dependencies=[Depends(require_auth)])
async def update_session(session_id: str, req: UpdateSessionRequest):
    """Update session fields (e.g. title)."""
    _validate_path_param(session_id, "session_id")
    svc = _get_session_service()
    if not svc:
        raise HTTPException(status_code=501, detail="Session runtime not enabled")
    session = svc.store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    if req.title is not None:
        session.title = req.title
    from datetime import datetime
    session.updated_at = datetime.now().isoformat()
    svc.store.update_session(session)
    return {"status": "updated", "session_id": session_id}


@app.post("/sessions/{session_id}/messages", dependencies=[Depends(require_auth)])
async def send_message(session_id: str, payload: SendMessageRequest, http_request: Request):
    """Send a user message and start the agent loop (natural language strategy)."""
    _validate_path_param(session_id, "session_id")
    svc = _get_session_service()
    if not svc:
        raise HTTPException(status_code=501, detail="Session runtime not enabled")
    try:
        result = await svc.send_message(
            session_id=session_id,
            content=payload.content,
            include_shell_tools=_shell_tools_enabled_for_request(http_request),
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/sessions/{session_id}/cancel", dependencies=[Depends(require_auth)])
async def cancel_session(session_id: str):
    """Cancel the in-flight agent loop for this session."""
    _validate_path_param(session_id, "session_id")
    svc = _get_session_service()
    if not svc:
        raise HTTPException(status_code=501, detail="Session runtime not enabled")
    cancelled = svc.cancel_current(session_id)
    if not cancelled:
        return {"status": "no_active_loop"}
    return {"status": "cancelled"}


@app.get("/sessions/{session_id}/messages", response_model=List[MessageResponse], dependencies=[Depends(require_auth)])
async def get_messages(session_id: str, limit: int = Query(100, ge=1, le=1000)):
    """List messages for a session."""
    _validate_path_param(session_id, "session_id")
    svc = _get_session_service()
    if not svc:
        raise HTTPException(status_code=501, detail="Session runtime not enabled")
    messages = svc.get_messages(session_id, limit=limit)
    return [
        MessageResponse(
            message_id=m.message_id,
            session_id=m.session_id,
            role=m.role,
            content=m.content,
            created_at=m.created_at,
            linked_attempt_id=m.linked_attempt_id,
            metadata=m.metadata if m.metadata else None,
        )
        for m in messages
    ]


@app.get("/sessions/{session_id}/events", dependencies=[Depends(require_event_stream_auth)])
async def session_events(
    session_id: str,
    request: Request,
    last_event_id: Optional[str] = Query(None, alias="Last-Event-ID"),
):
    """SSE stream for agent events."""
    _validate_path_param(session_id, "session_id")
    svc = _get_session_service()
    if not svc:
        raise HTTPException(status_code=501, detail="Session runtime not enabled")
    session = svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    header_id = request.headers.get("Last-Event-ID")
    event_id = header_id or last_event_id

    async def event_generator():
        async for event in svc.event_bus.subscribe(session_id, last_event_id=event_id):
            if await request.is_disconnected():
                break
            yield event.to_sse()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ============================================================================
# File Upload
# ============================================================================

_BLOCKED_UPLOAD_EXT = {
    # binaries / executables we should never accept
    ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".app", ".dmg",
    ".so", ".dll", ".dylib",
    # executable-adjacent source, shell, config, and template files
    ".py", ".pyw", ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".yaml", ".yml", ".j2", ".jinja", ".jinja2", ".template",
    # archives — don't auto-extract; user can unpack locally
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz",
}

_BLOCKED_UPLOAD_NAMES = {
    "dockerfile",
    "containerfile",
}


_SHADOW_ID_RE = __import__("re").compile(r"^shadow_[0-9a-f]{8}$")


@app.get("/shadow-reports/{shadow_id}", dependencies=[Depends(require_auth)])
async def get_shadow_report(shadow_id: str, format: str = "html"):
    """Serve a rendered Shadow Account report (HTML by default, PDF if available).

    Reports live under ``~/.vibe-trading/shadow_reports/<shadow_id>.{html,pdf}``.
    """
    if not _SHADOW_ID_RE.match(shadow_id):
        raise HTTPException(status_code=400, detail="invalid shadow_id")
    if format not in ("html", "pdf"):
        raise HTTPException(status_code=400, detail="format must be html or pdf")

    reports_dir = Path.home() / ".vibe-trading" / "shadow_reports"
    path = reports_dir / f"{shadow_id}.{format}"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Shadow report not found: {shadow_id}.{format}")

    media_type = "text/html; charset=utf-8" if format == "html" else "application/pdf"
    # Inline so browsers render HTML/PDF directly instead of forcing download.
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{shadow_id}.{format}"'},
    )


@app.post("/upload", dependencies=[Depends(require_auth)])
async def upload_file(file: UploadFile):
    """Upload any document or data file (max 50MB).

    Accepts most common formats: PDF, Word, Excel, PowerPoint, images,
    CSV/TSV, plain text, JSON, and TOML. Executables, executable-adjacent
    source/config/template files, and archives are rejected.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    filename = Path(file.filename).name
    ext = Path(file.filename).suffix.lower()
    if ext in _BLOCKED_UPLOAD_EXT or filename.lower() in _BLOCKED_UPLOAD_NAMES:
        raise HTTPException(
            status_code=400,
            detail="This file type is not allowed for upload.",
        )

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    safe_name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOADS_DIR / safe_name
    total_size = 0

    try:
        with dest.open("wb") as handle:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > MAX_UPLOAD_SIZE:
                    handle.close()
                    if dest.exists():
                        dest.unlink()
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large (limit {MAX_UPLOAD_SIZE // (1024 * 1024)} MB)",
                    )
                handle.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        if dest.exists():
            dest.unlink()
        raise HTTPException(status_code=500, detail=f"Failed to store upload: {exc}") from exc
    finally:
        await file.close()

    return {
        "status": "ok",
        "file_path": str(dest.resolve()),
        "filename": file.filename,
    }


# ============================================================================
# Swarm API
# ============================================================================

_swarm_runtime = None


def _get_swarm_runtime():
    """Lazy-init SwarmRuntime singleton."""
    global _swarm_runtime
    if _swarm_runtime is not None:
        return _swarm_runtime
    from src.swarm.store import SwarmStore
    from src.swarm.runtime import SwarmRuntime
    swarm_dir = Path(__file__).resolve().parent / ".swarm" / "runs"
    store = SwarmStore(base_dir=swarm_dir)
    _swarm_runtime = SwarmRuntime(store=store)
    return _swarm_runtime


@app.get("/swarm/presets")
async def list_swarm_presets():
    """List Swarm YAML presets."""
    from src.swarm.presets import list_presets
    return list_presets()


@app.post("/swarm/runs", dependencies=[Depends(require_auth)])
async def create_swarm_run(payload: dict, http_request: Request):
    """Start a swarm run: body must include preset_name and user_vars."""
    runtime = _get_swarm_runtime()
    preset_name = payload.get("preset_name", "")
    user_vars = payload.get("user_vars", {})
    try:
        run = runtime.start_run(
            preset_name,
            user_vars,
            include_shell_tools=_shell_tools_enabled_for_request(http_request),
        )
        return {"id": run.id, "status": run.status.value, "preset_name": run.preset_name}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/swarm/runs", dependencies=[Depends(require_auth)])
async def list_swarm_runs(limit: int = Query(20, ge=1, le=100)):
    """List swarm runs (newest first), reconciled."""
    runtime = _get_swarm_runtime()
    runs = runtime._store.list_runs(limit=limit)
    items = []
    for r in runs:
        # Reconcile each row: a zombie running run will be auto-finalized so
        # the dashboard never shows a permanent "running" stuck row.
        reconciled = runtime._store.reconcile_run(r, write=True)
        items.append(
            {
                "id": reconciled.id,
                "preset_name": reconciled.preset_name,
                "status": reconciled.status.value,
                "is_stale": runtime._store.is_run_stale(reconciled),
                "created_at": reconciled.created_at,
                "completed_at": reconciled.completed_at,
                "task_count": len(reconciled.tasks),
                "completed_count": sum(1 for t in reconciled.tasks if t.status.value == "completed"),
            }
        )
    return items


@app.get("/swarm/runs/{run_id}", dependencies=[Depends(require_auth)])
async def get_swarm_run(run_id: str):
    """Swarm run detail including task statuses (reconciled)."""
    _validate_path_param(run_id, "run_id")
    runtime = _get_swarm_runtime()
    loaded = runtime._store.load_run(run_id)
    if not loaded:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    run = runtime._store.reconcile_run(loaded, write=True)

    return {
        "id": run.id,
        "preset_name": run.preset_name,
        "status": run.status.value,
        "is_stale": runtime._store.is_run_stale(run),
        "user_vars": run.user_vars,
        "agents": [a.model_dump() for a in run.agents],
        "tasks": [t.model_dump() for t in run.tasks],
        "created_at": run.created_at,
        "completed_at": run.completed_at,
        "final_report": run.final_report,
    }


@app.get("/swarm/runs/{run_id}/events", dependencies=[Depends(require_event_stream_auth)])
async def swarm_run_events(run_id: str, request: Request, last_index: int = Query(0, ge=0)):
    """SSE stream for a swarm run."""
    import asyncio

    _validate_path_param(run_id, "run_id")
    runtime = _get_swarm_runtime()

    async def event_stream():
        idx = last_index
        while True:
            if await request.is_disconnected():
                break
            events = runtime._store.read_events(run_id, after_index=idx)
            for evt in events:
                idx += 1
                yield f"id: {idx}\nevent: {evt.type}\ndata: {json.dumps(evt.model_dump(), ensure_ascii=False)}\n\n"
            run = runtime._store.load_run(run_id)
            if run:
                # Reconcile so a zombie running run can still close this SSE
                # stream cleanly — without it, a dead host would keep the
                # stream open forever and block the dashboard's "done" state.
                reconciled = runtime._store.reconcile_run(run, write=True)
                if reconciled.status.value in ("completed", "failed", "cancelled"):
                    yield f"event: done\ndata: {{\"status\": \"{reconciled.status.value}\"}}\n\n"
                    break
            await asyncio.sleep(2)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/swarm/runs/{run_id}/cancel", dependencies=[Depends(require_auth)])
async def cancel_swarm_run(run_id: str):
    """Cancel an active swarm run."""
    _validate_path_param(run_id, "run_id")
    runtime = _get_swarm_runtime()
    ok = runtime.cancel_run(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"No active run {run_id}")
    return {"status": "cancelled"}


# ============================================================================
# Alpha Zoo routes (Web UI) — defined in src/api/alpha_routes.py
# ============================================================================

# ============================================================================
# Creator Opinion Radar
# ============================================================================

@app.get("/creator-opinions/channels", dependencies=[Depends(require_auth)])
async def creator_opinion_channels():
    """List configured YouTube creator channels for the opinion radar."""
    seed_default_channels()
    return {"channels": list_creator_channels()}


@app.post("/creator-opinions/refresh", dependencies=[Depends(require_auth)])
async def creator_opinion_refresh(payload: CreatorOpinionRefreshRequest):
    """Refresh latest creator videos and extract soft opinion/sector signals."""
    try:
        return await asyncio.to_thread(
            refresh_creator_opinions,
            handles=payload.handles,
            limit_per_channel=payload.limit_per_channel,
            use_llm=payload.use_llm,
            force=payload.force,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"creator opinion refresh failed: {str(exc)[:500]}") from exc


@app.get("/creator-opinions/feed", dependencies=[Depends(require_auth)])
async def creator_opinion_feed(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    """Latest creator videos and structured opinions."""
    return list_opinion_feed(limit=limit, offset=offset)


@app.get("/creator-opinions/sector-signals", dependencies=[Depends(require_auth)])
async def creator_opinion_sector_signals(days: int = Query(7, ge=1, le=30), refresh: bool = Query(False)):
    """Creator-derived sector heat and transmission summaries."""
    if refresh:
        save_sector_signals_from_opinions(days=min(days, 7))
    return {"signals": list_creator_sector_signals(days=days)}


@app.get("/creator-opinions/symbol/{symbol}", dependencies=[Depends(require_auth)])
async def creator_opinion_symbol(symbol: str, days: int = Query(3, ge=1, le=30)):
    """Soft creator-opinion tags for one symbol."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "", symbol).upper()
    if not safe:
        raise HTTPException(status_code=400, detail="invalid symbol")
    return {"symbol": safe, "tags": creator_opinion_tags_for_symbol(safe, days=days)}


@app.get("/premarket-news/source-audit", dependencies=[Depends(require_auth)])
async def premarket_news_source_audit():
    """Data-source audit for the premarket news queue."""
    return google_finance_source_audit()


@app.get("/premarket-news/auto-status", dependencies=[Depends(require_auth)])
async def premarket_news_auto_status():
    """Status of the 15-minute background news refresher."""
    return dict(_PREMARKET_NEWS_AUTO_STATUS)


@app.post("/premarket-news/refresh", dependencies=[Depends(require_auth)])
async def premarket_news_refresh(payload: PremarketNewsRefreshRequest):
    """Refresh the stock-level premarket news review queue."""
    try:
        return await asyncio.to_thread(
            _refresh_premarket_news_bounded,
            universes=payload.universes,
            start_utc=payload.start_utc,
            end_utc=payload.end_utc,
            translate_top=payload.translate_top,
            max_pages=payload.max_pages,
            refresh_universe=payload.refresh_universe,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"premarket news refresh failed: {str(exc)[:500]}") from exc


@app.get("/premarket-news", dependencies=[Depends(require_auth)])
def premarket_news_queue(
    limit: int = Query(80, ge=1, le=200),
    offset: int = Query(0, ge=0),
    reviewed: str = Query("unreviewed", pattern="^(all|unreviewed|important|not_important)$"),
    symbol: Optional[str] = Query(None),
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    recent_days: int = Query(0, ge=0, le=90),
):
    """Read the queued news items. Page reads do not trigger a refresh."""
    safe_symbol = re.sub(r"[^A-Za-z0-9._-]", "", symbol).upper() if symbol else None
    return list_premarket_news(
        limit=limit,
        offset=offset,
        reviewed=reviewed,
        symbol=safe_symbol,
        min_score=min_score,
        recent_days=recent_days,
        hydrate=False,
    )


@app.post("/premarket-news/{news_id}/feedback", dependencies=[Depends(require_auth)])
async def premarket_news_feedback(news_id: str, payload: PremarketNewsFeedbackRequest):
    """Persist human feedback and optionally add important items to watchlist."""
    try:
        return mark_premarket_news_feedback(
            news_id,
            decision=payload.decision,
            note=payload.note,
            weight_multiplier=payload.weight_multiplier,
            add_to_watchlist=payload.add_to_watchlist,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


from src.api.alpha_routes import register_alpha_routes  # noqa: E402
register_alpha_routes(app)


# ============================================================================
# Main Entry Point
# ============================================================================

def serve_main(argv: list[str] | None = None) -> int:
    """Start the API server from CLI-style arguments."""
    import argparse
    import subprocess
    import uvicorn
    from fastapi.staticfiles import StaticFiles
    from starlette.exceptions import HTTPException as StarletteHTTPException

    class SPAStaticFiles(StaticFiles):
        """Serve index.html for browser refreshes on client-side routes."""

        async def get_response(self, path: str, scope: Dict[str, Any]):
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                if exc.status_code != status.HTTP_404_NOT_FOUND:
                    raise
                return await super().get_response("index.html", scope)

    parser = argparse.ArgumentParser(description="easymoneysniper Server")
    parser.add_argument("--port", type=int, default=8000, help="Listen port (default 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--dev", action="store_true", help="Dev mode: spawn Vite on :5173")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    frontend_root = Path(__file__).resolve().parent.parent / "frontend"

    vite_proc = None
    if args.dev and frontend_root.exists():
        print("[dev] Starting Vite dev server on :5173 ...")
        vite_proc = subprocess.Popen(
            ["npx", "vite", "--host", "0.0.0.0"],
            cwd=str(frontend_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[dev] Vite PID={vite_proc.pid}")
        print("[dev] Frontend: http://localhost:5173")
        print(f"[dev] API: http://localhost:{args.port}")
    elif frontend_dist.exists():
        if not any(route.path == "/" for route in app.routes):
            app.mount("/", SPAStaticFiles(directory=str(frontend_dist), html=True), name="frontend")
        print(f"[prod] Frontend served from {frontend_dist}")
    else:
        print(f"[warn] No frontend build found at {frontend_dist}")
        print("[warn] Run: cd frontend && npm run build")

    print("=" * 50)
    print("  easymoneysniper Server")
    print(f"  http://127.0.0.1:{args.port}")
    print("=" * 50)

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        if vite_proc:
            vite_proc.terminate()
            print("[dev] Vite stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(serve_main())
