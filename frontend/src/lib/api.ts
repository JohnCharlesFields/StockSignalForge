import { authHeaders, withAuthQuery } from "@/lib/apiAuth";
import type { SupplyGraph } from "@/components/SupplyChainGraph";
import type { CallPlanData } from "@/components/CallPlan";

const BASE = "";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const AUTH_REQUIRED_MESSAGE =
  "Remote API access requires an API key. Add it in Settings, or run the backend on localhost for local-only use.";

export function isAuthRequiredError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

async function errorFromResponse(res: Response): Promise<ApiError> {
  let detail = `HTTP ${res.status}`;
  try {
    const body = await res.json();
    detail = body.detail || body.message || detail;
  } catch { /* ignore */ }
  if (res.status === 401 || res.status === 403) {
    detail = AUTH_REQUIRED_MESSAGE;
  }
  return new ApiError(detail, res.status);
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const { headers, ...rest } = options ?? {};
  const mergedHeaders: Record<string, string> = { "Content-Type": "application/json", ...authHeaders() };
  if (headers) {
    new Headers(headers).forEach((value, key) => {
      mergedHeaders[key] = value;
    });
  }
  const res = await fetch(`${BASE}${path}`, {
    ...rest,
    headers: mergedHeaders,
  });
  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  const text = await res.text();
  return text ? JSON.parse(text) : ({} as T);
}

export interface UploadResult {
  status: string;
  file_path: string;
  filename: string;
}

async function uploadFile(file: File): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/upload`, { method: "POST", headers: authHeaders(), body: form });
  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  return res.json();
}

export const api = {
  getCallPlan: (symbol: string, signal?: AbortSignal) => request<CallPlanData>(`/single-stock-overnight/${encodeURIComponent(symbol)}/call-plan`, { signal }),
  runCallPlan: (symbol: string, options: { capital: number; risk_pct: number; hold_days: number; max_cost: number }, signal?: AbortSignal) => request<{ started: boolean; status: string }>(`/single-stock-overnight/${encodeURIComponent(symbol)}/call-plan?${new URLSearchParams(Object.entries(options).map(([key, value]) => [key, String(value)]))}`, { method: "POST", signal }),
  getStockNewsResearch: (symbol: string, signal?: AbortSignal) => request<StockNewsResearchData>(`/single-stock-overnight/${encodeURIComponent(symbol)}/news-research`, { signal }),
  summarizeStockNews: (symbol: string, signal?: AbortSignal) => request<{ started: boolean; status: string }>(`/single-stock-overnight/${encodeURIComponent(symbol)}/news-research/summary`, { method: "POST", signal }),
  getSupplyChain: (symbol: string, signal?: AbortSignal) => request<SupplyGraph>(`/single-stock-overnight/${encodeURIComponent(symbol)}/supply-chain`, { signal }),
  refreshSupplyChain: (symbol: string, signal?: AbortSignal) => request<{ started: boolean; status: string }>(`/single-stock-overnight/${encodeURIComponent(symbol)}/supply-chain/refresh`, { method: "POST", signal }),
  uploadFile,
  listRuns: () => request<RunListItem[]>("/runs"),
  getRun: (id: string) => request<RunData>(`/runs/${id}`),
  getRunCode: (id: string) => request<Record<string, string>>(`/runs/${id}/code`),
  getRunReportPdfUrl: (id: string) => withAuthQuery(`${BASE}/runs/${id}/report.pdf?download=1`),
  getRunPine: (id: string) => request<PineScriptResult>(`/runs/${id}/pine`),
  listSessions: () => request<SessionItem[]>("/sessions"),
  createSession: (title?: string) => request<SessionItem>("/sessions", { method: "POST", body: JSON.stringify({ title: title || "" }) }),
  deleteSession: (sid: string) => request<{ status: string }>(`/sessions/${sid}`, { method: "DELETE" }),
  renameSession: (sid: string, title: string) => request<{ status: string }>(`/sessions/${sid}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  sendMessage: (sid: string, content: string) => request<{ message_id: string; attempt_id: string }>(`/sessions/${sid}/messages`, { method: "POST", body: JSON.stringify({ content }) }),
  cancelSession: (sid: string) => request<{ status: string }>(`/sessions/${sid}/cancel`, { method: "POST" }),
  getSessionMessages: (sid: string) => request<MessageItem[]>(`/sessions/${sid}/messages`),
  sseUrl: (sid: string) => withAuthQuery(`${BASE}/sessions/${sid}/events`),

  // Swarm API
  listSwarmPresets: () => request<SwarmPreset[]>("/swarm/presets"),
  createSwarmRun: (preset_name: string, user_vars: Record<string, string>) =>
    request<{ id: string; status: string }>("/swarm/runs", {
      method: "POST",
      body: JSON.stringify({ preset_name, user_vars }),
    }),
  listSwarmRuns: () => request<SwarmRunSummary[]>("/swarm/runs"),
  getSwarmRun: (id: string) => request<Record<string, unknown>>(`/swarm/runs/${id}`),
  swarmSseUrl: (id: string) => withAuthQuery(`${BASE}/swarm/runs/${id}/events`),
  cancelSwarmRun: (id: string) =>
    request<{ status: string }>(`/swarm/runs/${id}/cancel`, { method: "POST" }),
  getLLMSettings: () => request<LLMSettings>("/settings/llm"),
  updateLLMSettings: (settings: UpdateLLMSettingsRequest) =>
    request<LLMSettings>("/settings/llm", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  getDataSourceSettings: () => request<DataSourceSettings>("/settings/data-sources"),
  getMarketDataStatus: () => request<{ configuration_only: boolean; daily_cache_price_basis: string;
    price_basis_note: string; ohlcv_priority: string[]; provider_configuration: Record<string, string> }>("/market-data/status"),
  updateDataSourceSettings: (settings: UpdateDataSourceSettingsRequest) =>
    request<DataSourceSettings>("/settings/data-sources", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),

  // Alpha Zoo API
  listAlphas: (params: AlphaListParams = {}) => {
    const q = new URLSearchParams();
    if (params.zoo) q.set("zoo", params.zoo);
    if (params.theme) q.set("theme", params.theme);
    if (params.universe) q.set("universe", params.universe);
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<AlphaListResponse>(`/alpha/list${qs ? `?${qs}` : ""}`);
  },
  getAlpha: (alphaId: string) =>
    request<AlphaDetailResponse>(`/alpha/${encodeURIComponent(alphaId)}`),
  createAlphaBench: (body: AlphaBenchRequest) =>
    request<{ status: string; job_id: string }>("/alpha/bench", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  alphaBenchStreamUrl: (jobId: string) =>
    withAuthQuery(`${BASE}/alpha/bench/${encodeURIComponent(jobId)}/stream`),

  // Event-driven research API
  listEventCalibrations: (params: { usable_only?: boolean; min_events?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.usable_only !== undefined) q.set("usable_only", String(params.usable_only));
    if (params.min_events !== undefined) q.set("min_events", String(params.min_events));
    const qs = q.toString();
    return request<EventCalibrationListResponse>(`/event-driven/calibrations${qs ? `?${qs}` : ""}`);
  },
  scanEventDrivenSp500: (body: EventDrivenScanRequest) =>
    request<EventDrivenScanResponse>("/event-driven/sp500/scan", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listResearchUniverses: () =>
    request<{ universes: ResearchUniverseOption[] }>("/research-universes"),
  listCreatorChannels: () =>
    request<{ channels: CreatorChannel[] }>("/creator-opinions/channels"),
  refreshCreatorOpinions: (body: CreatorOpinionRefreshInput) =>
    request<CreatorOpinionRefreshResponse>("/creator-opinions/refresh", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getCreatorOpinionFeed: (limit = 50, offset = 0) =>
    request<CreatorOpinionFeedResponse>(`/creator-opinions/feed?limit=${limit}&offset=${offset}`),
  getCreatorRefreshStatus: () => request<CreatorRefreshStatus>("/creator-opinions/status"),
  getCreatorSectorSignals: (days = 7, refresh = false) =>
    request<{ signals: CreatorSectorSignal[] }>(`/creator-opinions/sector-signals?days=${days}&refresh=${refresh}`),
  getCreatorSymbolTags: (symbol: string, days = 3) =>
    request<{ symbol: string; tags: CreatorSymbolTag[] }>(`/creator-opinions/symbol/${encodeURIComponent(symbol)}?days=${days}`),
  getPremarketNewsSourceAudit: () =>
    request<PremarketNewsSourceAudit>("/premarket-news/source-audit"),
  getPremarketNewsAutoStatus: () =>
    request<PremarketNewsAutoStatus>("/premarket-news/auto-status"),
  enrichPremarketNews: () => request<{ started: boolean; status: string }>("/premarket-news/enrich", { method: "POST" }),
  refreshPremarketNews: (body: PremarketNewsRefreshInput) =>
    request<PremarketNewsRefreshResponse>("/premarket-news/refresh", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getPremarketNews: (params: { limit?: number; offset?: number; reviewed?: string; symbol?: string; min_score?: number; recent_days?: number } = {}) => {
    const q = new URLSearchParams();
    q.set("limit", String(params.limit ?? 80));
    q.set("offset", String(params.offset ?? 0));
    q.set("reviewed", params.reviewed ?? "unreviewed");
    if (params.symbol) q.set("symbol", params.symbol);
    if (params.min_score !== undefined) q.set("min_score", String(params.min_score));
    if (params.recent_days !== undefined) q.set("recent_days", String(params.recent_days));
    return request<PremarketNewsResponse>(`/premarket-news?${q.toString()}`);
  },
  markPremarketNewsFeedback: (newsId: string, body: PremarketNewsFeedbackInput) =>
    request<PremarketNewsFeedbackResponse>(`/premarket-news/${encodeURIComponent(newsId)}/feedback`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getSoxlQuantStatus: () => request<SoxlQuantStatus>("/soxl-quant/status"),
  getSoxlQuantLatest: (includeCurve = true, tradeLimit = 100) =>
    request<{ available: boolean; result?: SoxlBacktestResult | null }>(
      `/soxl-quant/backtest/latest?include_curve=${includeCurve}&trade_limit=${tradeLimit}`,
    ),
  startSoxlQuantBacktest: (initialCapital = 2000, forceDownload = false, source: "databento" | "yfinance_proxy" = "databento") =>
    request<SoxlBacktestJob>(
      `/soxl-quant/backtest?initial_capital=${initialCapital}&force_download=${forceDownload}&source=${source}`,
      { method: "POST" },
    ),
  getSoxlQuantBacktestStatus: () => request<SoxlBacktestJob>("/soxl-quant/backtest/status"),
  startSoxlQuantLive: () => request<SoxlLiveStatus>("/soxl-quant/live/start", { method: "POST" }),
  stopSoxlQuantLive: () => request<SoxlLiveStatus>("/soxl-quant/live/stop", { method: "POST" }),
  getSoxlQuantTrades: (limit = 100) =>
    request<{ symbol: "SOXL"; paper_only: true; items: SoxlPaperTrade[] }>(`/soxl-quant/trades?limit=${limit}`),
  getWatchlist: (enrich = true, includeDisabled = true) =>
    request<WatchlistResponse>(`/api/watchlist?enrich=${enrich}&include_disabled=${includeDisabled}`),
  getWatchlistItem: (symbol: string) =>
    request<{ symbol: string; exists: boolean; item?: WatchlistItem | null }>(`/api/watchlist/${encodeURIComponent(symbol)}`),
  addWatchlistItem: (body: WatchlistItemInput) =>
    request<{ status: string; item: WatchlistItem }>("/api/watchlist/items", { method: "POST", body: JSON.stringify(body) }),
  addWatchlistItems: (body: WatchlistBatchInput) =>
    request<{ status: string; count: number; items: WatchlistItem[] }>("/api/watchlist/items/batch", { method: "POST", body: JSON.stringify(body) }),
  setWatchlistEnabled: (symbol: string, enabled: boolean) =>
    request<{ status: string; item: WatchlistItem }>(`/api/watchlist/items/${encodeURIComponent(symbol)}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  deleteWatchlistItem: (symbol: string) =>
    request<{ status: string; deleted: boolean; symbol: string }>(`/api/watchlist/items/${encodeURIComponent(symbol)}`, { method: "DELETE" }),
  runResearchSignalHub: (body: ResearchSignalHubRunInput) =>
    request<ResearchSignalHubJob>("/research-signal-hub/run", { method: "POST", body: JSON.stringify(body) }),
  getResearchSignalHubRun: (jobId: string) =>
    request<ResearchSignalHubJob>(`/research-signal-hub/run/${encodeURIComponent(jobId)}`),

  // Composite priority board (global Top Pick source for every page)
  getPriorityBoard: (limit = 8, liquidOnly = true, oversoldOnly = false, forceRefresh = false) =>
    request<PriorityBoardResponse>(`/priority-board?limit=${limit}&liquid_only=${liquidOnly}&oversold_only=${oversoldOnly}&force_refresh=${forceRefresh}`),
  getLongOptionScreen: () => request<LongOptionScreenResponse>("/priority-board/long-options"),
  getVSwingStatus: () => request<VSwingStatus>("/v-swing/status"),
  runVSwing: () => request<{ status: string; job_id?: string }>("/v-swing/run", { method: "POST" }),
  getLongOptionScreenStatus: () => request<LongOptionScreenJob>("/priority-board/long-options/status"),
  getLongOptionShadow: () => request<LongOptionShadowScorecard>("/priority-board/long-options/shadow"),
  resolveLongOptionShadow: () => request<{ resolve: { resolved: number; pending_or_immature: number }; scorecard: LongOptionShadowScorecard }>("/priority-board/long-options/shadow/resolve", { method: "POST" }),
  runLongOptionScreen: () => request<LongOptionScreenJob>("/priority-board/long-options/run", { method: "POST" }),
  reviewLongOption: (symbol: string) => request<LongOptionReview>(`/priority-board/long-options/${encodeURIComponent(symbol)}/review`, { method: "POST" }),
  getPriorityBasket: (budget = 10000, n = 8, weighting: "inverse_vol" | "equal" = "inverse_vol") =>
    request<PriorityBasketResponse>(`/priority-board/basket?budget=${budget}&n=${n}&weighting=${weighting}`),
  getPriorityMonitor: (limit = 100, offset = 0, days = 30) =>
    request<PriorityMonitorResponse>(`/priority-board/monitor?limit=${limit}&offset=${offset}&days=${days}`),
  getTrackLeader: (symbol: string) =>
    request<TrackLeaderResponse>(`/track-leader/${encodeURIComponent(symbol)}`),

  // Forward-verification ledger (predicted vs realized track record)
  getPredictionScorecard: (mode: "live" | "backfill" | "any" = "live", version = "legacy") =>
    request<PredictionScorecard>(`/predictions/scorecard?mode=${mode}&evaluation_version=${version}`),
  getPullbackParameterStatus: () => request<{enabled: boolean; interval_days: number; auto_activate: boolean; latest?: {status?: string; candidate_version?: string; rejection_reasons?: string[]}; active?: unknown}>("/pullback-parameters/status"),
  trainPullbackParameters: () => request<{status: string}>("/pullback-parameters/train", {method: "POST"}),
  logPredictionsNow: () => request<unknown>("/predictions/log?log_all=true", { method: "POST" }),
  resolvePredictionsNow: () =>
    request<{ resolve: unknown; scorecard: PredictionScorecard }>("/predictions/resolve", { method: "POST" }),
  backfillPredictionsNow: (lookback = 30) =>
    request<{ status: string; note?: string }>(
      `/predictions/backfill?lookback_trading_days=${lookback}`,
      { method: "POST" },
    ),
  getBackfillStatus: () =>
    request<{ status: string; n_resolved?: number; error?: string; finished_at?: string }>(
      "/predictions/backfill/status",
    ),

  // Scheduled batch jobs (task center)
  getFeishuMobileUrl: () => request<FeishuMobileUrl>("/feishu-mobile-url"),
  getDailyAutoStatus: () => request<DailyAutoStatus>("/daily-three-layer-auto/status"),
  finalizeDailyAuto: () => request<DailyAutoStatus>("/daily-three-layer-auto/finalize", { method: "POST" }),
  runDailyAuto: (force = false) =>
    request<DailyAutoStatus>("/daily-three-layer-auto/run", {
      method: "POST",
      body: JSON.stringify({ force, refresh_evidence: true }),
    }),
  getSignalCalibrationStatus: () => request<SignalCalibrationStatus>("/signal-calibration/status"),
  collectOos: () =>
    request<unknown>("/signal-calibration/collect?universe=spx", { method: "POST" }),
  rebuildCalibration: () =>
    request<CalibRebuildStatus>("/signal-calibration/rebuild?universe=spx&period=3y", { method: "POST" }),
  getCalibRebuildStatus: () => request<CalibRebuildStatus>("/signal-calibration/rebuild-status"),

  // Portfolio holdings tracker -> add/hold/trim/close decisions
  getPortfolioView: () => request<PortfolioView>("/portfolio/view"),
  upsertPortfolioHolding: (body: { symbol: string; shares: number; avg_cost: number; note?: string }) =>
    request<PortfolioView>("/portfolio/holding", { method: "POST", body: JSON.stringify(body) }),
  deletePortfolioHolding: (symbol: string) =>
    request<PortfolioView>(`/portfolio/holding/${encodeURIComponent(symbol)}`, { method: "DELETE" }),
  setPortfolioAccount: (available_cash: number, currency = "USD") =>
    request<PortfolioView>("/portfolio/account", { method: "POST", body: JSON.stringify({ available_cash, currency }) }),

  // Launch Signal API
  scanLaunchSignal: (body: LaunchScanRequest) =>
    request<LaunchScanResponse>("/launch-signal/scan", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  scanLaunchSignalAsync: (body: LaunchScanRequest) =>
    request<{ job_id: string }>("/launch-signal/scan-async", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  launchSignalScanStreamUrl: (jobId: string) =>
    withAuthQuery(`${BASE}/launch-signal/scan/${encodeURIComponent(jobId)}/stream`),
  calibrateLaunchSignalAsync: (body: LaunchCalibrationRequest) =>
    request<{ job_id: string }>("/launch-signal/calibrate-async", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  launchSignalCalibrateStreamUrl: (jobId: string) =>
    withAuthQuery(`${BASE}/launch-signal/calibrate/${encodeURIComponent(jobId)}/stream`),
  listLaunchSignalModels: () =>
    request<{ models: LaunchModelInfo[] }>("/launch-signal/models"),
  listLaunchSignalPeerGroups: () =>
    request<{ groups: LaunchPeerGroup[] }>("/launch-signal/peer-groups"),
};

// --- Swarm types ---

export interface SwarmPreset {
  name: string;
  title: string;
  description: string;
  agent_count: number;
  variables: { name: string; description: string; required: boolean }[];
}

export interface SwarmRunSummary {
  id: string;
  preset_name: string;
  status: string;
  created_at: string;
  task_count: number;
  completed_count: number;
}

export interface CreatorChannel {
  handle: string;
  name: string;
  channel_id?: string;
  url?: string;
  theme?: string;
  enabled?: number;
  weight?: number;
  last_checked_at?: string;
  metadata?: Record<string, unknown>;
}

export interface CreatorOpinionRefreshInput {
  background?: boolean;
  handles?: string[] | null;
  limit_per_channel?: number;
  use_llm?: boolean;
  force?: boolean;
}

export interface CreatorOpinionRefreshResponse {
  started?: boolean;
  status?: string;
  retry_after_seconds?: number;
  started_at?: string;
  finished_at?: string;
  new_videos?: number;
  opinions_ready?: number;
  transcripts_ready?: number;
  channels?: Array<{
    handle: string;
    error?: string;
    videos?: Array<{
      video_id: string;
      title: string;
      published_at: string;
      url?: string;
      is_new?: boolean;
      transcript_available?: boolean;
      opinion_available?: boolean;
      opinion_status?: string;
      reason?: string;
    }>;
  }>;
}

export interface CreatorRefreshStatus extends CreatorOpinionRefreshResponse {
  phase?: string;
  processed_channels?: number;
  total_channels?: number;
  current_video?: string;
  pending_analysis?: number;
  error?: string;
  message?: string;
  auto_enabled?: boolean;
  auto_interval_seconds?: number;
  max_analyses_per_run?: number;
}

export interface CreatorTickerView {
  ticker?: string;
  stance?: string;
  confidence?: number;
  horizon?: string;
  evidence?: string;
  risk?: string;
}

export interface CreatorSectorView {
  sector_key?: string;
  sector_name?: string;
  stance?: string;
  confidence?: number;
  leader_tickers?: string[];
  transmission?: string;
  evidence?: string;
  risk?: string;
}

export interface CreatorOpinionPayload {
  available?: boolean;
  status?: string;
  model?: string;
  creator?: string;
  video_id?: string;
  title?: string;
  published_at?: string;
  summary?: string;
  macro_view?: string;
  ticker_views?: CreatorTickerView[];
  sector_views?: CreatorSectorView[];
  risk_flags?: string[];
  method_note?: string;
  reason?: string;
  llm_error?: string;
}

export interface CreatorOpinionFeedItem {
  video_id: string;
  handle: string;
  title: string;
  url: string;
  published_at: string;
  transcript_status: string;
  opinion_status: string;
  error?: string;
  language?: string;
  chars?: number;
  opinion?: CreatorOpinionPayload;
}

export interface CreatorOpinionFeedResponse {
  items: CreatorOpinionFeedItem[];
  latest_date?: string | null;
  previous_date?: string | null;
  latest_count?: number;
  previous_count?: number;
}

export interface CreatorSectorSignal {
  signal_id?: string;
  as_of_date?: string;
  sector_key: string;
  sector_name: string;
  mentions?: number;
  bullish?: number;
  bearish?: number;
  neutral?: number;
  creators?: string[];
  leader_tickers?: string[];
  evidence?: string[];
  sentiment_score?: number;
  stance?: string;
  lookback_days?: number;
}

export interface CreatorSymbolTag {
  label: string;
  tone?: string;
  creator?: string;
  video_id?: string;
  evidence?: string;
}

export interface PremarketNewsSourceAudit {
  source: string;
  url?: string;
  usable_as_primary?: boolean;
  status?: string;
  reason?: string;
  preferred_sources?: Array<{ source: string; role: string }>;
}

export interface PremarketNewsRefreshInput {
  universes?: string[] | null;
  start_utc?: string | null;
  end_utc?: string | null;
  translate_top?: number;
  max_pages?: number;
  refresh_universe?: boolean;
}

export interface PremarketNewsRefreshResponse {
  status?: string;
  error?: string;
  generated_at: string;
  window?: { start_utc?: string; end_utc?: string };
  requested_universes?: string[];
  universe_failures?: Array<{ universe: string; error: string }>;
  universe_symbol_count?: number;
  raw_news_count?: number;
  matched_item_count?: number;
  written?: number;
  source_audit?: Record<string, unknown>;
}

export interface PremarketNewsAutoStatus {
  gildata_supplement?: { status?: string; accepted?: number } | null;
  gildata_news?: { status?: string; reason?: string; primary_enabled?: boolean } | null;
  enabled?: boolean;
  interval_seconds?: number;
  status?: string;
  started_at?: string | null;
  last_run_at?: string | null;
  last_success_at?: string | null;
  last_error?: string;
  last_result?: {
    matched_item_count?: number;
    raw_news_count?: number;
    written?: number;
    universe_symbol_count?: number;
    window?: { start_utc?: string; end_utc?: string };
    source_audit?: { yahoo_raw_count?: number; yahoo?: { failures?: { symbol: string; error_type: string }[] } };
  } | null;
}

export interface PremarketNewsAlternative {
  symbol: string;
  reason?: string;
  action_hint?: string;
}

export interface PremarketNewsFeedback {
  decision: "important" | "not_important" | "skip";
  weight_multiplier?: number | null;
  note?: string;
  added_to_watchlist?: boolean;
  updated_at?: string | null;
}

export interface PremarketNewsItem {
  news_id: string;
  symbol: string;
  company_name?: string;
  source_universes?: string[];
  source_pools?: string[];
  title_original: string;
  description_original?: string;
  title_cn?: string;
  description_cn?: string;
  translation_status?: string;
  publisher?: string;
  article_url?: string;
  published_utc?: string;
  source?: string;
  source_provenance?: { reported_time?: string; time_status?: string; queue_time_basis?: string; original_verified?: boolean; note?: string };
  sentiment?: "positive" | "negative" | "neutral" | string;
  sentiment_score?: number;
  sentiment_reasoning?: string;
  event_type?: string;
  event_type_cn?: string;
  importance_score?: number;
  adjusted_importance_score?: number;
  estimated_gap_pct?: number | null;
  impact_band_pct?: number | null;
  current_price?: number | null;
  previous_close?: number | null;
  change_pct?: number | null;
  atr_pct?: number | null;
  metadata_status?: "ok" | "price_missing" | "price_unavailable" | string;
  quote_as_of?: string | null;
  quote_source?: string | null;
  quote_is_realtime?: boolean;
  impact_basis?: string | null;
  attribution?: { primary?: string; channels?: string[]; promotion?: boolean };
  sector_key?: string;
  sector_name?: string;
  sector_effect?: boolean;
  related_symbols?: string[];
  alternatives?: PremarketNewsAlternative[];
  source_tier?: string;
  source_tier_cn?: string;
  source_tier_tone?: "good" | "neutral" | "caution" | "warn";
  feedback?: PremarketNewsFeedback | null;
  preference?: { multiplier: number; logit: number; label: string; reasons: string[] } | null;
  personalized_score?: number;
}

export interface PremarketNewsResponse {
  items: PremarketNewsItem[];
  count: number;
  limit: number;
  offset: number;
  reviewed: string;
  recent_days?: number;
  source_mix_degraded?: boolean;
  source_mix_warning?: string;
  latest_update?: { updated_at?: string | null; latest_news?: string | null };
  source_audit?: PremarketNewsSourceAudit;
}

export interface PremarketNewsFeedbackInput {
  decision: "important" | "not_important" | "skip";
  note?: string;
  weight_multiplier?: number | null;
  add_to_watchlist?: boolean | null;
}

export interface PremarketNewsFeedbackResponse {
  news_id: string;
  symbol: string;
  decision: string;
  weight_multiplier?: number;
  adjusted_importance_score?: number;
  added_to_watchlist?: boolean;
}

export interface LLMProviderOption {
  name: string;
  label: string;
  api_key_env?: string | null;
  base_url_env: string;
  default_model: string;
  default_base_url: string;
  api_key_required: boolean;
  auth_type?: string;
  login_command?: string | null;
}

export interface LLMSettings {
  provider: string;
  model_name: string;
  base_url: string;
  api_key_env?: string | null;
  api_key_configured: boolean;
  api_key_hint?: string | null;
  api_key_required: boolean;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort: string;
  env_path: string;
  providers: LLMProviderOption[];
}

export interface UpdateLLMSettingsRequest {
  provider: string;
  model_name: string;
  base_url: string;
  api_key?: string;
  clear_api_key?: boolean;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort?: string;
}

export interface DataSourceSettings {
  tushare_token_configured: boolean;
  tushare_token_hint?: string | null;
  twelve_data_configured: boolean;
  fmp_configured: boolean;
  baostock_supported: boolean;
  baostock_installed: boolean;
  baostock_message: string;
  env_path: string;
}

export interface UpdateDataSourceSettingsRequest {
  tushare_token?: string;
  clear_tushare_token?: boolean;
  twelve_data_api_key?: string;
  clear_twelve_data_api_key?: boolean;
  fmp_api_key?: string;
  clear_fmp_api_key?: boolean;
}

// --- Types matching backend API contracts ---

export interface RunListItem {
  run_id: string;
  status: string;
  created_at: string;
  prompt?: string;
  total_return?: number;
  sharpe?: number;
  codes?: string[];
  start_date?: string;
  end_date?: string;
}

export interface PriceBar {
  time: string;
  timestamp?: string;
  code?: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TradeMarker {
  time: string;
  timestamp?: string;
  code?: string;
  side: "BUY" | "SELL";
  price: number;
  qty?: number;
  reason?: string;
  text?: string;
  live?: boolean;
}

export interface EquityPoint {
  time: string;
  equity: string | number;
  drawdown: string | number;
}

export interface ValidationData {
  monte_carlo?: {
    actual_sharpe: number;
    actual_max_dd: number;
    p_value_sharpe: number;
    p_value_max_dd: number;
    simulated_sharpe_mean: number;
    simulated_sharpe_std: number;
    simulated_sharpe_p5: number;
    simulated_sharpe_p95: number;
    n_simulations: number;
    n_trades: number;
    error?: string;
  };
  bootstrap?: {
    observed_sharpe: number;
    ci_lower: number;
    ci_upper: number;
    median_sharpe: number;
    prob_positive: number;
    confidence: number;
    n_bootstrap: number;
    error?: string;
  };
  walk_forward?: {
    n_windows: number;
    windows: Array<{
      window: number;
      start: string;
      end: string;
      return: number;
      sharpe: number;
      max_dd: number;
      trades: number;
      win_rate: number;
    }>;
    profitable_windows: number;
    consistency_rate: number;
    return_mean: number;
    return_std: number;
    sharpe_mean: number;
    sharpe_std: number;
    error?: string;
  };
}

export interface RunData {
  status: string;
  run_id: string;
  prompt?: string;
  elapsed_seconds?: number;
  run_directory?: string;
  run_stage?: string;
  run_context?: Record<string, unknown>;
  generated_files?: ArtifactInfo[];
  file_previews?: Record<string, string>;

  metrics?: BacktestMetrics;
  artifacts?: ArtifactInfo[];
  run_card?: RunCard;
  validation?: ValidationData;

  price_series?: Record<string, PriceBar[]>;
  indicator_series?: Record<string, Record<string, IndicatorPoint[]>>;
  trade_markers?: TradeMarker[];
  equity_curve?: EquityPoint[];
  trade_log?: Array<Record<string, string>>;
  run_logs?: Array<{ source?: string; line_number?: number; message?: string }>;
}

export interface RunCard {
  schema_version?: string;
  generated_at?: string;
  run_dir?: string;
  backtest?: Record<string, unknown>;
  reproducibility?: Record<string, unknown>;
  data_sources?: string[];
  metrics?: Record<string, unknown>;
  validation?: unknown;
  warnings?: string[];
  artifacts?: RunCardArtifact[];
  [key: string]: unknown;
}

export interface RunCardArtifact {
  path: string;
  size_bytes: number;
  sha256: string;
}

export interface BacktestMetrics {
  final_value: number;
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe: number;
  win_rate: number;
  trade_count: number;
  [key: string]: number;
}


export interface IndicatorPoint {
  time: string;
  value: number;
}

export interface ArtifactInfo {
  name: string;
  path: string;
  type: string;
  size: number;
  exists: boolean;
}

export interface PineScriptResult {
  exists: boolean;
  content: string | null;
}

export interface SessionItem {
  session_id: string;
  title?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
  last_attempt_id?: string;
}

// --- Alpha Zoo types ---

export interface AlphaListParams {
  zoo?: string;
  theme?: string;
  universe?: string;
  limit?: number;
}

export interface AlphaSummary {
  id: string;
  zoo: string;
  theme: string[];
  universe: string[];
  nickname?: string;
  decay_horizon?: number | null;
  min_warmup_bars?: number | null;
  requires_sector?: boolean;
}

export interface AlphaListResponse {
  status: string;
  alphas: AlphaSummary[];
  total: number;
  returned: number;
  truncated: boolean;
}

export interface AlphaDetail {
  id: string;
  zoo: string;
  module_path?: string;
  meta: Record<string, unknown>;
}

export interface AlphaDetailResponse {
  status: string;
  alpha: AlphaDetail;
  source_code: string;
}

export interface AlphaBenchRequest {
  zoo: string;
  universe: string;
  period: string;
  top?: number;
}

export interface AlphaBenchTopRow {
  id: string;
  ic_mean: number;
  ir: number;
  theme: string[];
  formula_latex: string;
  category: "alive" | "reversed" | "dead";
}

export interface AlphaBenchResult {
  alive: number;
  reversed: number;
  dead: number;
  skipped?: number;
  top5_by_ir: AlphaBenchTopRow[];
  dead_examples: AlphaBenchTopRow[];
  by_theme: Record<string, { alive: number; reversed: number; dead: number }>;
}

// --- Event-driven research types ---

export interface EventCalibrationInfo {
  run_id: string;
  summary_path: string;
  event_count: number;
  usable: boolean;
  primary_horizon?: string | null;
  primary_beta?: number | null;
  primary_ic?: number | null;
  updated_at: string;
}

export interface EventCalibrationListResponse {
  calibrations: EventCalibrationInfo[];
}

export interface EventDrivenScanRequest {
  universe?: string;
  tickers?: string[];
  limit?: number;
  top?: number;
  news_per_ticker?: number;
  lookback_hours?: number;
  min_relevance?: number;
  cache_ttl_minutes?: number;
  sleep_seconds?: number;
  workers?: number;
  include_prices?: boolean;
  calibration_run_id?: string;
  calibration_json?: string;
  auto_calibration?: boolean;
  min_calibration_events?: number;
  timeout_seconds?: number;
}

export interface ResearchUniverseOption {
  universe: string;
  label: string;
  tier: string;
  group?: string;
  group_label?: string;
  priority?: number;
  purpose?: string;
  run_cost?: string;
  recommended?: boolean;
}

export interface WatchlistItem {
  symbol: string;
  name?: string;
  note?: string;
  enabled?: boolean;
  source?: string;
  created_at?: string;
  updated_at?: string;
}

export interface WatchlistItemInput {
  symbol: string;
  name?: string;
  note?: string;
  enabled?: boolean;
}

export interface WatchlistBatchInput {
  symbols: string[];
  note?: string;
  enabled?: boolean;
}

export interface DistributionRisk {
  available?: boolean;
  triggered?: boolean;
  level?: "none" | "medium" | "high" | string;
  label?: string;
  score?: number | null;
  volume_ratio?: number | null;
  day_return?: number | null;
  intraday_return?: number | null;
  close_location?: number | null;
  upper_shadow_pct?: number | null;
  failed_breakout?: boolean;
  high_volume_churn?: boolean;
  reasons?: string[];
  method?: string;
}

export interface WatchlistMetricRow extends WatchlistItem {
  current_price?: number | null;
  day_change_pct?: number | null;
  market_cap?: number | null;
  pe?: number | null;
  sector?: string | null;
  industry?: string | null;
  return_20d?: number | null;
  return_60d?: number | null;
  benchmark_return_60d?: number | null;
  relative_to_qqq_60d?: number | null;
  trend_status?: string | null;
  research_probability?: number | null;
  overall_score?: number | null;
  pullback_rejection_status?: string | null;
  pullback_confirmation_status?: string | null;
  gex_level?: string | null;
  gex_regime?: string | null;
  earnings?: EarningsInfo | null;
  alpha_win_rate?: number | null;
  mean_beta_adjusted_alpha?: number | null;
  distribution_risk?: DistributionRisk | null;
  source_universe_ids?: string[];
  source_universe_labels?: string[];
  overnight_alpha?: Record<string, unknown>;
  detail_url?: string;
}

export interface WatchlistResponse {
  universe: "watchlist" | string;
  label: string;
  benchmark: string;
  benchmark_label?: string;
  count: number;
  items: WatchlistMetricRow[];
  refreshing?: boolean;
  pending_count?: number;
  generated_at?: string;
}

export interface ResearchSignalHubRunInput {
  universe: string;
  refresh_evidence?: boolean;
  top?: number;
  enable_llm_review?: boolean;
  llm_review_top?: number;
  enable_event_llm_review?: boolean;
}

export interface ResearchSignalHubJob {
  job_id: string;
  universe: string;
  status: "queued" | "running" | "completed" | "failed" | string;
  phase?: string;
  message?: string;
  progress?: number;
  started_at?: string;
  finished_at?: string;
  result?: Record<string, unknown>;
}

export interface EventDrivenSignal {
  symbol: string;
  signal: string;
  signal_cn?: string;
  launch_score?: string | number;
  event_score?: string | number;
  tech_score?: string | number;
  expected_abret?: string | number;
  calibration_horizon?: string;
  calibration_ic?: string | number;
  calibration_type_coverage?: string | number;
  calibration_shrinkage?: string | number;
  calibrated_signal?: string;
  last_price?: string | number;
  ret_5d?: string | number;
  ret_20d?: string | number;
  volume_ratio?: string | number;
  event_count?: string | number;
  positive_events?: string | number;
  negative_events?: string | number;
  reason?: string;
  top_event?: string;
  pullback_rejection?: PullbackRejectionSignal;
  pullback_confirmation?: PullbackConfirmationSignal;
}

export interface EventDrivenScanResponse {
  status: string;
  run_id: string;
  elapsed_seconds: number;
  event_count: number;
  signal_count: number;
  signals: EventDrivenSignal[];
  report_markdown?: string | null;
  run_directory: string;
  artifacts: ArtifactInfo[];
  report_pdf_url: string;
  detail_url: string;
  calibration?: EventCalibrationInfo | null;
}

// --- Launch Signal types ---

export interface LaunchScanRequest {
  universe?: string;
  symbols?: string[];
  model_path?: string;
  threshold?: number;
  top?: number;
  use_stock_observation_pool?: boolean;
  scan_mode?: "technical" | "peer_earnings" | "combined";
  peer_group?: string;
}

export interface LaunchPeerGroup {
  id: string;
  label: string;
  symbols: string[];
}

export interface DailyTunnelScore {
  score: number;
  label: string;
  current_zone: string;
  cycle_state?: string;
  sample_days: number;
  ma5?: number | null;
  ma10?: number | null;
  ma15?: number | null;
  ma20?: number | null;
  above_ma5_rate: number;
  above_ma20_rate: number;
  bullish_alignment_rate: number;
  current_above_ma5_streak: number;
  max_ma5_break_pct: number;
  current_up_cycle_days?: number;
  prior_down_cycle_days?: number;
  longest_up_cycle_days?: number;
  average_up_cycle_days?: number;
  up_cycle_power?: number;
  turning_point_score?: number;
  components?: Record<string, number>;
  note?: string;
}

export interface PullbackRejectionSignal {
  ticker?: string;
  signal_name?: string;
  signal_stage?: "NONE" | "WATCH" | "STRONG_WATCH" | string;
  signal_date?: string | null;
  score?: number | null;
  close?: number | null;
  pullback_pct?: number | null;
  pullback_days?: number | null;
  lower_shadow_ratio?: number | null;
  lower_shadow_pct_of_range?: number | null;
  close_location?: number | null;
  trend_status?: string;
  tunnel_status?: string;
  nearest_support?: number | null;
  support_distance_pct?: number | null;
  waiting_breakout_price?: number | null;
  invalidation_price?: number | null;
  risk_flags?: string[];
  human_readable_reason?: string;
}

export interface PullbackConfirmationSignal {
  ticker?: string;
  signal_name?: string;
  signal_stage?: "NONE" | "WAITING_CONFIRMATION" | "WEAK_CONFIRMED" | "CONFIRMED" | "STRONG_CONFIRMED" | "INVALIDATED" | string;
  rejection_date?: string | null;
  confirmation_date?: string | null;
  confirmation_type?: string;
  confirmation_score?: number | null;
  rejection_high?: number | null;
  rejection_low?: number | null;
  current_close?: number | null;
  waiting_breakout_price?: number | null;
  invalidation_price?: number | null;
  risk_flags?: string[];
  human_readable_reason?: string;
}

export interface PeerEarningsEvidence {
  peer_match_version?: number;
  group_id: string;
  group_label: string;
  peer_similarity: number;
  peer_similarity_breakdown?: Record<string, number>;
  transmission_direction?: string;
  sublane_relation?: string;
  sublane_relation_display?: string;
  leader_sublane?: string;
  target_sublane?: string;
  leader_sublane_label?: string;
  target_sublane_label?: string;
  leader_to_target_market_cap_ratio?: number | null;
  leader_symbol: string;
  leader_report_date: string;
  target_report_date?: string | null;
  days_to_target_report?: number | null;
  target_report_date_requires_manual_check?: boolean;
  target_report_date_note?: string;
  leader_eps_surprise_pct: number;
  leader_revenue_yoy?: number | null;
  leader_start_date: string;
  leader_market_cap?: number | null;
  leader_trailing_pe?: number | null;
  leader_forward_pe?: number | null;
  leader_pre_report_price?: number;
  leader_latest_price?: number;
  leader_post_report_return?: number;
  leader_return: number;
  leader_event_volume_ratio: number;
  leader_dollar_volume_ratio: number;
  target_return: number;
  target_start_price?: number;
  target_latest_price: number;
  target_market_cap?: number | null;
  target_trailing_pe?: number | null;
  target_forward_pe?: number | null;
  target_latest_volume_ratio: number;
  relative_lag_gap: number;
  expected_catch_up_return?: number;
  expected_target_price?: number;
  relay_score: number;
  research_probability: number;
  daily_tunnel?: DailyTunnelScore;
  flow_proxy_note: string;
}

export interface LaunchCalibrationRequest {
  universe?: string;
  symbols?: string[];
  days?: number;
  fwd_horizon?: number;
  launch_threshold?: number;
  contamination?: number;
}

export interface LaunchSignalItem {
  symbol: string;
  launch_score: number;
  signal: string;
  signal_cn: string;
  last_price: number;
  ret_5d: number;
  ret_10d: number;
  ret_20d: number;
  vol_ratio: number;
  rsi_14: number;
  bb_position: number;
  roc_10: number;
  breakout_20: number;
  opportunity_score?: number | null;
  opportunity_tier?: string | null;
  source_pools?: string[];
  reason: string;
  technical_launch_score?: number;
  base_launch_score?: number;
  daily_tunnel?: DailyTunnelScore;
  peer_earnings?: PeerEarningsEvidence;
  pullback_rejection?: PullbackRejectionSignal;
  pullback_confirmation?: PullbackConfirmationSignal;
  // R1 calibrated probability (honest, corrects the inverted raw score).
  calibrated_probability?: number | null;
  calibration?: {
    horizon_days?: number;
    launch?: { p_calibrated?: number | null; source?: string; support_ok?: boolean } | null;
    daily_tunnel?: { p_calibrated?: number | null; source?: string; support_ok?: boolean } | null;
  } | null;
}

// R1 calibration summary attached to a launch scan (the response top-level
// `calibration` now carries this shadow-mode summary, not the legacy ML result).
export interface LaunchScanCalibrationSummary {
  horizon_days?: number;
  mode?: string;
  n_signals?: number;
  n_calibrated?: number;
  events_logged?: number;
}

export type ConfidenceBadge = "validated" | "low_edge" | "experimental";

export interface EarningsInfo {
  available?: boolean;
  next_date?: string | null;
  days_until?: number | null;
  eps_estimated?: number | null;
  revenue_estimated?: number | null;
  last_report_date?: string | null;
  last_surprise_pct?: number | null;
  last_eps_actual?: number | null;
  last_eps_estimated?: number | null;
  eps_estimate_vs_last_actual?: number | null;
  estimate_tone?: "positive" | "warning" | "neutral" | string;
  estimate_basis?: string;
  near_earnings?: boolean;
  time?: string | null;
  reason?: string;
}

export interface PriorityPick {
  symbol: string;
  current_price: number;
  priority_score: number;
  ranking_score?: number | null;
  soft_enhancement_nudge?: number | null;
  relative_strength_20d: number;
  stock_return_3m?: number | null;
  stock_return_6m?: number | null;
  pullback_hv_score?: number | null;
  calibrated_probability: number;
  calibration_source?: string;
  regime_multiplier: number;
  portfolio_regime?: string | null;
  portfolio_regime_cn?: string | null;
  raw_launch_score: number;
  daily_tunnel_score: number;
  trend_30d?: number;
  options_sentiment?: { tone: string; label: string } | null;
  iv?: { atm_iv?: number | null; iv_rise?: number | null; iv_state?: string; samples?: number } | null;
  hv?: { hv_rise?: number | null; hv_state?: string } | null;
  liquidity?: { adv?: number | null; tier?: string; tone?: string } | null;
  deep_oversold?: { level: "deep" | "oversold" | "none"; z?: number; rsi2?: number; below_band?: boolean } | null;
  track?: string | null;
  track_cn?: string | null;
  source_universe_ids?: string[];
  source_universe_labels?: string[];
  playbook_enhancements?: {
    market_liquid_rs_top40?: boolean;
    market_liquid_rs_percentile_cutoff?: number | null;
    stock_stronger_than_industry?: boolean;
    industry_etf?: string | null;
    stock_vs_industry_3m?: number | null;
    stock_vs_industry_6m?: number | null;
    market_ok?: boolean;
    liquid_ok?: boolean;
    market_rs_score?: number | null;
    market_rs_raw?: number | null;
    playbook_score?: number | null;
    sector_diffusion?: {
      sector_etf?: string | null;
      score?: number | null;
      current_count?: number | null;
      prev_avg_count?: number | null;
      breadth_change?: number | null;
      fresh_component_ratio?: number | null;
      stock_vs_sector_ratio?: number | null;
      evidence_level?: string | null;
    } | null;
    labels?: Array<{ id?: string; label?: string; tone?: string; reason?: string }>;
  } | null;
  news?: { label?: string | null; net_score?: number; neg?: number; pos?: number; trump?: boolean } | null;
  earnings?: EarningsInfo | null;
  confidence_badge: ConfidenceBadge;
  reason: string;
  detail_url: string;
  v_swing?: VSwingScore;
}

export interface VSwingScore {
  status?: string;
  lifecycle?: string;
  today_buy?: boolean;
  buy?: boolean;
  sell?: boolean;
  buy_p?: number;
  sell_p?: number;
  buy_threshold?: number;
  sell_threshold?: number;
  price_as_of?: string;
  signal_date?: string;
  close?: number;
  earn_warn?: boolean;
  earnings_unknown?: boolean;
  r_multiple?: number | null;
  plan?: { entry: number; stop: number; target: number; stop_distance_pct: number; stop_too_far: boolean; one_pct_risk_position_pct: number };
  frozen_plan?: { entry: number; stop: number; target: number; managed_stop?: number };
}

export interface VSwingChartSnapshot {
  status?: string;
  available?: boolean;
  session?: string;
  generated_at?: string;
  pattern_supported?: boolean;
  score?: VSwingScore;
  markers?: TradeMarker[];
}

export interface VSwingStatus {
  enabled?: boolean;
  rank_today_first?: boolean;
  job: { status: string; error_type?: string; finished_at?: string };
  snapshot: { session?: string; generated_at?: string; today_buys?: number; open_buys?: number; pattern_supported?: boolean;
    profit_calibration?: { status?: string; eligible?: boolean; train_n?: number; test_n?: number } };
  validation: { experimental?: boolean; data_audit?: { supplied_labels?: number; used_buy?: number; used_sell?: number; symbols?: number };
    resolved_buys?: number; net_win_rate?: number;
    group_sides?: Record<string, { rf: { pr_auc?: number; baseline_pr_auc?: number; precision?: number; recall?: number }; logistic: { pr_auc?: number } }> };
}

export interface NewsItem {
  title: string;
  publisher?: string | null;
  published_utc?: string | null;
  url?: string | null;
  sentiment: "positive" | "negative" | "neutral";
  reasoning?: string | null;
  trump?: boolean;
}

export interface NewsImpact {
  available: boolean;
  point_pct?: number;
  band_pct?: number;
  direction?: string;
  basis?: string;
  note?: string;
}

export interface NewsDigest {
  available: boolean;
  symbol?: string;
  n?: number;
  pos?: number;
  neg?: number;
  neu?: number;
  net_score?: number;
  label?: string;
  risk?: boolean;
  reason?: string | null;
  status_code?: number | null;
  source?: string;
  items?: NewsItem[];
  trump?: { mentioned: boolean; count: number; net_score: number; items: NewsItem[] };
  impact?: NewsImpact;
}

export interface StockNewsArticle {
  id: string; title_original: string; title_cn: string; source_summary: string;
  summary_cn: string; sentiment: string; reason: string; publisher: string; source: string; relevance?: string;
  url?: string | null; published_utc: string; time_basis: string; reported_time: string;
  content_basis: "source_abstract" | "title_only";
}
export interface StockNewsResearchData {
  symbol: string; available: boolean; count: number; window_days: number; fingerprint: string;
  news_from?: string | null; news_to?: string | null; note: string; items: StockNewsArticle[];
  analysis: { status: string; summary_cn?: string; sentiment?: string; generated_at?: string; model?: string;
    summary_basis?: "title_only" | "mixed" | "source_abstract"; summary_news_ids?: string[];
    support?: { text: string; news_ids: string[] }[]; risks?: { text: string; news_ids: string[] }[] };
}

export interface LeaderCompareMember {
  symbol: string;
  is_query?: boolean;
  adv?: number | null;
  liquidity?: { adv?: number | null; tier?: string; tone?: string };
  current_price?: number | null;
  calibrated_win_rate?: number | null;
  pullback_state?: string | null;
  in_pullback?: boolean;
}

export interface LeaderCompareData {
  available?: boolean;
  reason?: string;
  track_cn?: string | null;
  leader?: string;
  query_is_leader?: boolean;
  members?: LeaderCompareMember[];
  verdict?: string;
  note?: string;
}

export interface TrackLeaderResponse {
  symbol: string;
  track_cn?: string | null;
  leader_compare?: LeaderCompareData;
}

export interface PriorityBasketItem {
  symbol: string;
  track?: string | null;
  track_cn?: string | null;
  current_price: number;
  calibrated_probability?: number;
  hv20?: number | null;
  weight: number;
  target_amount: number;
  shares: number;
  est_cost: number;
  stop_suggest: number;
  liquidity?: { adv?: number | null; tier?: string; tone?: string } | null;
  detail_url?: string;
}

export interface PriorityBasketResponse {
  available: boolean;
  no_edge_today: boolean;
  weighting?: string;
  n_requested: number;
  n_filled: number;
  budget: number;
  deployed: number;
  cash_left: number;
  equal_weight_pct: number;
  basket: PriorityBasketItem[];
  liquidity_floor?: { applied?: boolean; threshold_adv?: number };
  generated_at?: string;
  note?: string;
}

export interface LongOptionScreenRow {
  symbol: string;
  status: string;
  reason?: string;
  side?: "C" | "P" | null;
  spot?: number;
  stock_data_as_of?: string;
  benchmark_data_as_of?: string | null;
  net_consensus?: number;
  signal_score?: number;
  bull_consensus?: number | null;
  bear_consensus?: number | null;
  hv20?: number | null;
  relative_strength_20d?: number | null;
  universe_source?: string;
  option_volume_12m?: number;
  technical_context?: LongOptionTechnicalContext;
}

export interface LongOptionTechnicalContext {
  range_sessions?: number;
  range_high?: number;
  range_low?: number;
  swing_direction?: "up" | "down";
  fib_retracement?: Record<string, number | null>;
  breakout_price?: number;
  support_price?: number;
  wave_status?: string;
  wave_structure?: LongOptionWaveStructure;
}

export interface LongOptionWavePoint {
  label?: string;
  kind: "high" | "low";
  price: number;
  date: string;
  confirmed_on: string;
}

export interface LongOptionWaveStructure {
  status: "unverified" | "no_pattern" | "impulse_forming" | "impulse_candidate" | "abc_candidate" | "stale_pattern";
  stage?: "wave5_pending" | "wave5_complete" | "abc_complete" | null;
  direction?: "up" | "down" | null;
  points: LongOptionWavePoint[];
  recent_pivots: LongOptionWavePoint[];
  rule_checks: string[];
  pivot_confirmation_bars: number;
  last_pivot_age_bars?: number;
  reason?: string;
  is_predictive_probability: false;
}

export interface LongOptionReview {
  reference_evidence?: import("@/components/GildataEvidence").GildataEvidence;
  available: boolean;
  status: string;
  symbol: string;
  reason?: string;
  provider?: string;
  model?: string;
  rule_view?: "CALL" | "PUT";
  model_view: "CALL" | "PUT" | "WAIT";
  final_view: "CALL" | "PUT" | "WAIT";
  conflict?: boolean;
  summary?: string;
  supporting_points?: string[];
  objections?: string[];
  watch_condition?: string;
  invalidation_condition?: string;
  wave_note?: string;
  technical_context?: LongOptionTechnicalContext;
  stock_data_as_of?: string;
  created_at?: string;
}

export interface LongOptionScreenSnapshot {
  generated_at: string;
  data_as_of: string;
  source: string;
  rows: LongOptionScreenRow[];
  universe_count: number;
  verified_sector_groups: number;
  sector_group_count: number;
  option_data_request_count: number;
  option_volume_research?: {
    available: boolean;
    leaders: Array<{ symbol: string; contracts_12m: number }>;
    window_start?: string;
    window_end?: string;
    coverage?: string;
    reason?: string;
  };
  note: string;
}

export interface LongOptionScreenResponse {
  available: boolean;
  snapshot: LongOptionScreenSnapshot | null;
  stale?: boolean;
  latest_completed_session?: string;
  reason?: string;
}

export interface LongOptionScreenJob {
  job_id?: string;
  status: "idle" | "running" | "completed" | "failed" | "unavailable";
  processed?: number;
  total?: number;
  stage?: string;
  error?: string;
  reason?: string;
  shadow_recorded?: number;
  shadow_resolved?: number;
  shadow_warning?: string;
}

export interface LongOptionShadowScorecard {
  mode: "live_only";
  horizon_days: number;
  recorded: number;
  late_replay_excluded: number;
  pending: number;
  invalid_data: number;
  resolved: number;
  independent_signal_dates: number;
  recorded_signal_dates: number;
  latest_signal_date: string | null;
  directional: { n: number; hit_net: number | null; mean_signed_net: number | null; mean_signed_spy_excess: number | null };
  coverage: number | null;
  matched_always_call: { n: number; policy_mean_per_candidate: number | null; always_call_mean_per_candidate: number | null; delta_policy_minus_always_call: number | null };
  call: { n: number; hit_net: number | null; mean_signed_net: number | null; mean_signed_spy_excess: number | null };
  put: { n: number; hit_net: number | null; mean_signed_net: number | null; mean_signed_spy_excess: number | null };
  wait: { n: number; mean_abs_underlying_move: number | null };
  ci95_mean_signed_net_by_date: [number, number] | null;
  evidence_status: string;
  note: string;
}

export interface PriorityBoardResponse {
  generated_at: string;
  data_as_of?: string | null;
  data_stale?: boolean | null;
  snapshot_id?: string | null;
  benchmark_return_20d: number;
  confidence: { badge: ConfidenceBadge; note: string; spread?: number };
  signal_basis?: string;
  horizon_days?: number;
  n_candidates: number;
  n_freshly_scored?: number;
  liquidity_floor?: {
    applied?: boolean;
    threshold_adv?: number;
    set_size?: number;
    base?: string;
    base_count?: number;
    ndx_included?: boolean;
    top_n?: number;
    as_of?: string;
  };
  no_edge_today?: boolean;
  top_calibrated_win_rate?: number;
  win_rate_explainer?: string;
  calibration_provenance?: {
    available?: boolean;
    source?: string | null;
    source_label?: string | null;
    generated_at?: string | null;
    event_count?: number | null;
    survivorship_controlled?: boolean;
    provenance_complete?: boolean;
    price_source?: string | null;
    window?: [string, string] | null;
    in_sample?: boolean;
    note?: string | null;
  };
  picks: PriorityPick[];
  cache_hit: boolean;
  method_note: string;
}

export type PriorityMonitorStatus = "NEW" | "WATCHING" | "IMPROVING" | "WEAKENING" | "INVALIDATED";

export interface PriorityMonitorRow {
  symbol: string;
  as_of_date?: string | null;
  candidate_rank?: number | null;
  is_board_pick?: boolean;
  status: PriorityMonitorStatus;
  first_seen?: string | null;
  days_seen?: number;
  best_rank?: number | null;
  rank_delta?: number | null;
  current_price?: number | null;
  calibrated_probability?: number | null;
  priority_score?: number | null;
  relative_strength_20d?: number | null;
  waiting_breakout_price?: number | null;
  invalidation_price?: number | null;
  reason?: string | null;
  track?: string | null;
  track_cn?: string | null;
  source_universe_ids?: string[];
  source_universe_labels?: string[];
  liquidity?: { adv?: number | null; tier?: string; tone?: string } | null;
  hv?: { hv_state?: string | null; hv_rise?: number | null } | null;
  detail_url?: string | null;
}

export interface PriorityMonitorResponse {
  available: boolean;
  as_of_date?: string | null;
  limit: number;
  offset: number;
  lookback_days: number;
  rows: PriorityMonitorRow[];
  summary: {
    shown: number;
    tracked_symbols: number;
    board_picks_shown: number;
    improving: number;
    watching: number;
    new: number;
    invalidated: number;
  };
}

export interface FeishuMobileUrl {
  available: boolean;
  url: string | null;
  mobile_url: string | null;
  updated_at: string | null;
  error?: string;
}

export interface SoxlPaperTrade {
  symbol: "SOXL";
  side: "LONG" | "SHORT";
  qty: number;
  entry_time?: string | null;
  exit_time?: string | null;
  entry_price: number;
  exit_price: number;
  pnl: number;
  return_pct: number;
  exit_reason: string;
  entry_score: number;
  holding_bars: number;
}

export interface SoxlPerformance {
  initial_capital: number;
  final_equity: number;
  net_profit: number;
  total_return: number;
  max_drawdown: number;
  sharpe: number;
  trade_count: number;
  long_trades: number;
  short_trades: number;
  win_rate: number;
  profit_factor: number;
  avg_trade_pnl: number;
  sessions: number;
  start?: string;
  end?: string;
  mean_session_return?: number;
  mean_session_return_ci95?: [number | null, number | null];
  session_edge_significant?: boolean;
  positive_session_rate?: number;
  side_stats?: Record<"LONG" | "SHORT", { trade_count: number; win_rate: number; net_profit: number; profit_factor: number }>;
  trades: SoxlPaperTrade[];
  equity_curve?: { timestamp: string; equity: number }[];
}

export interface SoxlBacktestResult {
  run_id: string;
  generated_at: string;
  mode: string;
  symbol: "SOXL";
  dataset: string;
  schema: string;
  source?: { source?: string; rows?: number; effective_end?: string; path?: string };
  selected_config: Record<string, number>;
  splits: Record<string, [string, string]>;
  train: SoxlPerformance;
  validation: SoxlPerformance;
  test: SoxlPerformance;
  all: SoxlPerformance;
  test_always_long_benchmark: { initial_capital: number; final_equity: number; total_return: number; sessions: number; positive_session_rate: number };
  test_excess_vs_always_long: number;
  candidate_count: number;
  selection_note: string;
  limitations: string[];
}

export interface SoxlBacktestJob {
  job_id?: string;
  status: "idle" | "running" | "completed" | "error" | string;
  progress: number;
  message: string;
  run_id?: string;
  started_at?: string;
  finished_at?: string;
  result?: SoxlBacktestResult;
}

export interface SoxlLiveStatus {
  status: "stopped" | "connecting" | "running" | "stopping" | "error" | string;
  symbol: "SOXL";
  mode: "paper";
  broker_execution: false;
  message: string;
  bars_received: number;
  initial_capital?: number;
  cash?: number;
  equity?: number;
  realized_pnl?: number;
  trade_count?: number;
  signal?: "LONG" | "SHORT" | "FLAT";
  signal_score?: number;
  pending_action?: string | null;
  position?: { side: number; qty: number; entry_price: number; entry_time: string; stop_price: number; target_price: number } | null;
  last_bar?: { timestamp: string; bid: number; ask: number; mid: number; spread_bps?: number | null };
  recent_bars?: { timestamp: string; bid: number; ask: number; mid: number; spread_bps?: number | null }[];
  error?: string | null;
  data_source_configured?: boolean;
}

export interface SoxlQuantStatus {
  symbol: "SOXL";
  allowed_symbols: ["SOXL"];
  paper_only: true;
  broker_execution: false;
  dataset: string;
  schema?: string;
  data_source_configured?: boolean;
  session: string;
  backtest_job: SoxlBacktestJob;
  live: SoxlLiveStatus;
  latest_backtest?: SoxlBacktestResult | null;
}

export interface DailyAutoStatus {
  daily_news?: { date?: string; status?: string; written?: number; error?: string; finished_at?: string; elapsed_seconds?: number } | null;
  daily_evidence?: { status?: string; event_status?: string; llm_status?: string; finished_at?: string } | null;
  blocked_by_active_worker?: boolean;
  status?: string;
  message?: string;
  progress?: number;
  current_universe?: string;
  started_at?: string | null;
  finished_at?: string | null;
  universe_count?: number;
  enabled?: boolean;
  schedule_time?: string;
  timezone?: string;
  today?: string;
  scheduled_at?: string;
  last_record?: {
    error?: string | null;
    phase?: string;
    data_warnings?: string[];
    completed_universe_count?: number;
    failed_universe_count?: number;
    skipped_universe_count?: number;
    cached_price_coverage?: { current?: number; total?: number; ratio?: number };
    price_repair?: { status?: string; error?: string; phase?: string; available_end?: string | null; reserved_usd?: number } | null;
    gildata_price_sync?: { status?: string; symbols_written?: number; records_added?: number; rejected_count?: number; rejection_reasons?: Record<string, number>; cooldown_count?: number; pending_count?: number; calls?: number; elapsed_seconds?: number; error?: string; sync_version?: number } | null;
    resumed_universe_count?: number;
    results?: Record<string, DailyPoolResult> | DailyPoolResult[];
    date?: string;
    status?: string;
    finished_at?: string;
    finalize_timed_out?: boolean;
    market_data_fresh?: boolean;
    snapshot_row_count?: number;
    excluded_stale_count?: number;
    phase_timings?: {
      total?: number;
      universe_loop_total?: number;
      finalize_total?: number;
      per_universe?: Record<string, number>;
      [phase: string]: number | Record<string, number> | undefined;
    };
  } | null;
  market?: {
    most_recent_session?: string;
    today_is_trading_day?: boolean;
    last_synced_session?: string | null;
    price_sync_pending?: boolean;
  } | null;
}

export interface DailyPoolResult {
  universe: string;
  status?: string;
  message?: string;
  elapsed_seconds?: number;
  reused?: boolean;
}

export interface CalibRebuildStatus {
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  message?: string;
}

export interface PredictionScorecard {
  available: boolean;
  counts: { total: number; resolved: number; pending: number; board_decisions: number; by_mode?: Record<string, number> };
  generated_at?: string;
  mode?: string;
  method_note?: string;
  n_resolved?: number;
  trading_days?: number;
  predicted_win_rate?: number | null;
  realized_win_rate?: number;
  mean_net_excess?: number;
  net_excess_ci?: [number | null, number | null];
  significant?: boolean;
  evidence_direction?: "positive" | "negative" | "inconclusive";
  evaluation_version?: string;
  evaluation_note?: string;
  overlap_adjusted_ci?: [number | null, number | null];
  mean_net_return?: number | null;
  horizon_cohorts?: {horizon_days: number; n: number; mean_net_excess: number}[];
  brier?: number | null;
  brier_skill_score?: number | null;
  reliability_buckets?: { label: string; n: number; predicted: number; realized: number }[];
  rank_buckets?: { label: string; n: number; predicted_win_rate: number; realized_win_rate: number; mean_net_excess: number }[];
  rank_monotone?: boolean | null;
  enhancement_buckets?: {
    label: string;
    n: number;
    predicted_win_rate: number;
    realized_win_rate: number;
    mean_net_excess: number;
    net_excess_ci?: [number | null, number | null];
    mean_beta_alpha?: number | null;
    beta_alpha_ci?: [number | null, number | null];
    significant?: boolean;
  }[];
  enhancement_note?: string;
  calibration_audit?: {
    available: boolean;
    warnings?: string[];
    note?: string;
    items?: {
      signal_type?: string;
      horizon_days?: number;
      source?: string | null;
      event_count?: number;
      cluster_count?: number;
      validated?: boolean;
      spread?: number | null;
      price_source?: string | null;
      option_source?: string | null;
      survivorship_controlled?: boolean | null;
      point_in_time_universe?: boolean | null;
    }[];
  };
  beta_alpha?: {
    n: number;
    mean_net_alpha: number;
    win_rate: number;
    ci: [number | null, number | null];
    significant: boolean;
    evidence_direction?: "positive" | "negative" | "inconclusive";
    note?: string;
  };
  scope?: string;
  open_positions?: { count: number; avg_return_so_far?: number | null; note?: string };
}

export type PortfolioAction = "ADD" | "HOLD" | "TRIM" | "CLOSE";

export interface RotationTarget {
  symbol: string;
  current_price: number;
  calibrated_win_rate: number;
  expected_return_8d?: number | null;
  expected_excess_8d?: number | null;
  liquidity_tier?: string | null;
  deep_oversold_level?: string | null;
  deep_oversold_cn?: string | null;
  sector?: string | null;
  news_label?: string | null;
  news_neg?: number;
  news_pos?: number;
  excess_is_estimate?: boolean;
  detail_url: string;
  est_shares?: number;
  est_gain_8d?: number | null;
  est_excess_8d?: number | null;
}

export interface RotationBlock {
  freed_capital?: number;
  pool_capital?: number;
  targets: RotationTarget[];
  note?: string;
}

export interface PortfolioDecision {
  action: PortfolioAction;
  action_cn: string;
  tone: "buy" | "hold" | "warn" | "sell";
  reason: string;
  add_qty?: number;
  rotation?: RotationBlock;
  levels: {
    stop_price?: number;
    stop_pct?: number;
    take_profit?: number;
    take_profit_pct?: number;
    add_zone_low?: number;
    add_zone_high?: number;
    add_qty?: number;
    trim_shares?: number;
  };
}

export interface PortfolioHoldingView {
  symbol: string;
  shares: number;
  avg_cost: number;
  current_price?: number | null;
  note?: string;
  position_value: number;
  cost_value: number;
  unrealized_pnl: number;
  unrealized_pct?: number | null;
  weight_pct?: number | null;
  atr14?: number | null;
  calibrated_win_rate?: number | null;
  confidence_badge?: ConfidenceBadge | null;
  pullback_state?: string | null;
  hv_state?: string | null;
  relative_strength_20d?: number | null;
  distribution_risk?: DistributionRisk | null;
  signal_available: boolean;
  earnings?: { next_date?: string | null; days_until?: number | null; last_surprise_pct?: number | null };
  decision: PortfolioDecision;
  detail_url: string;
}

export interface PortfolioView {
  generated_at: string;
  account: {
    available_cash: number;
    currency: string;
    market_value: number;
    total_equity: number;
    cost_basis: number;
    unrealized_pnl: number;
    unrealized_pct?: number | null;
    cash_pct?: number | null;
    invested_pct?: number | null;
    updated_at?: string | null;
  };
  regime: { gross_exposure_multiplier: number; regime_cn?: string };
  action_counts: Record<PortfolioAction, number>;
  rotation?: (RotationBlock & { pool_capital: number; note: string }) | null;
  holdings: PortfolioHoldingView[];
  n_holdings: number;
  method_note: string;
}

export interface SignalCalibrationStatus {
  mode?: string;
  events?: { total: number; resolved: number; pending: number };
  active_curves?: Array<{
    signal_type: string;
    horizon_days: number;
    source: string;
    generated_at: string;
    event_count?: number;
    global?: { edge?: { validated?: boolean } };
  }>;
}

export interface LaunchCalibrationResult {
  precision: number;
  recall: number;
  f1: number;
  threshold: number;
  n_train_samples: number;
  n_positive: number;
  n_features: number;
  contamination: number;
  gmm_components: number;
  feature_importances: Record<string, number>;
  validation_results: Record<string, unknown>;
}

export interface LaunchScanResponse {
  run_id: string;
  scan_time: string;
  elapsed_seconds: number;
  n_scanned: number;
  n_signals: number;
  threshold: number;
  signals: LaunchSignalItem[];
  calibration?: LaunchScanCalibrationSummary | null;
  feature_stats?: Record<string, number> | null;
  input_source?: string;
  scan_mode?: "technical" | "peer_earnings" | "combined";
  peer_group?: string;
}

export interface LaunchModelInfo {
  path: string;
  name: string;
  precision: number;
  recall: number;
  f1: number;
  n_samples: number;
  threshold: number;
}

export interface MessageItem {
  message_id: string;
  session_id: string;
  role: string;
  content: string;
  created_at: string;
  linked_attempt_id?: string;
  metadata?: Record<string, unknown>;
}
