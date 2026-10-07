import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  Clock3,
  ExternalLink,
  Loader2,
  Play,
  Radar,
  RefreshCw,
  ShieldAlert,
  TimerReset,
  TrendingUp,
  Zap,
} from "lucide-react";
import { authHeaders } from "@/lib/apiAuth";

interface ResearchUniverseOption {
  universe?: string;
  id?: string;
  label?: string;
  group?: string;
  cost_level?: string;
  run_cost?: string;
  default_selected?: boolean;
  recent_win_rate?: number | null;
  historical_win_rate?: number | null;
  proxy_win_rate?: number | null;
  historical_is_proxy?: boolean | null;
  historical_source?: string | null;
  unified_probability?: number | null;
  gex_coverage?: number | null;
  candidate_count?: number | null;
  top_symbol?: string | null;
  recent_win?: {
    status?: string;
    candidate_count?: number | null;
    recent_win_score?: number | null;
    historical_win_rate?: number | null;
    proxy_win_rate?: number | null;
    historical_is_proxy?: boolean | null;
    historical_source?: string | null;
    unified_probability?: number | null;
    gex_coverage?: number | null;
    top_symbol?: string | null;
    report_run_id?: string | null;
    report_timestamp?: string | null;
    source_kind?: string | null;
    source_is_fallback?: boolean | null;
    source_audit_note?: string | null;
  };
}

interface ResearchRow {
  symbol?: string;
  current_price?: number | null;
  market_cap?: number | null;
  pe?: number | null;
  stock_signal_score?: number | null;
  launch_signal_status?: string | null;
  launch_signal_score?: number | null;
  event_radar_status?: string | null;
  macro_regime_label?: string | null;
  macro_regime_mode_cn?: string | null;
  macro_vix_value?: number | null;
  macro_panic_score?: number | null;
  macro_systemic_crisis?: boolean | null;
  gex_level?: string | null;
  gex_regime?: string | null;
  overall_sort_score?: number | null;
  unified_score?: number | null;
  unified_probability?: number | null;
  unified_probability_low?: number | null;
  unified_probability_high?: number | null;
  historical_win_rate?: number | null;
  empirical_win_rate?: number | null;
  proxy_win_rate?: number | null;
  historical_is_proxy?: boolean | null;
  historical_source?: string | null;
  probability_for_ranking?: number | null;
  expected_value?: number | null;
  overall_status?: string | null;
  invalidation_price?: number | null;
  target_price?: number | null;
  reward_risk_ratio?: number | null;
  open_risk_status?: string | null;
  open_risk_reason?: string | null;
  exit_action?: string | null;
  exit_reason?: string | null;
  short_reason?: string | null;
  risk_flags?: string[];
  source_pools?: string[];
  snapshot_time?: string | null;
  daily_tunnel_score?: number | null;
  daily_tunnel_label?: string | null;
  pullback_rejection_status?: string | null;
  pullback_rejection_score?: number | null;
  pullback_confirmation_status?: string | null;
  pullback_confirmation_score?: number | null;
  waiting_breakout_price?: number | null;
  nearest_support?: number | null;
  llm_review?: {
    available?: boolean;
    status?: string;
    provider?: string;
    model?: string;
    review_focus?: string;
    verdict?: string;
    confidence?: number | null;
    risk_level?: string;
    overnight_action?: string;
    summary?: string;
    specialist_notes?: string[];
    supporting_points?: string[];
    objections?: string[];
    risk_flags?: string[];
    watch_items?: string[];
    invalid_if?: string[];
  };
}

interface AggregatedResearchRow extends ResearchRow {
  source_universe_ids: string[];
  source_universe_labels: string[];
}

interface SignalHubResponse {
  rows?: ResearchRow[];
  snapshot_time?: string;
  snapshot?: { generated_at?: string; data_as_of?: string; status?: string };
  universe?: string;
  macro_regime?: {
    label_cn?: string;
    mode_cn?: string;
    value?: number | null;
    systemic_crisis?: boolean;
    panic_score?: number | null;
  };
}

interface OvernightAlphaSymbolResult {
  symbol?: string;
  available?: boolean;
  reason?: string;
  benchmark?: string;
  benchmark_source_universe?: string;
  beta?: number | null;
  stats?: {
    sample_count?: number | null;
    absolute_win_rate?: number | null;
    alpha_win_rate?: number | null;
    strong_alpha_win_rate?: number | null;
    mean_alpha?: number | null;
    median_alpha?: number | null;
    mean_beta_adjusted_alpha?: number | null;
    median_beta_adjusted_alpha?: number | null;
    profit_factor?: number | null;
    max_loss?: number | null;
    max_consecutive_alpha_losses?: number | null;
  };
}

interface OvernightAlphaSummary {
  available?: boolean;
  period?: string;
  method?: string;
  note?: string;
  portfolio?: {
    sample_count?: number | null;
    symbol_count?: number | null;
    alpha_win_rate?: number | null;
    strong_alpha_win_rate?: number | null;
    mean_beta_adjusted_alpha?: number | null;
  };
  results?: OvernightAlphaSymbolResult[];
}

interface HomeDashboardSnapshotResponse {
  available?: boolean;
  snapshot?: {
    snapshot_id?: string;
    generated_at?: string;
    payload?: {
      rows?: AggregatedResearchRow[];
      overnight_alpha?: OvernightAlphaSummary | null;
      universe_alpha_top?: UniverseAlphaTopItem[];
      loaded_universe_count?: number;
      failed_universe_count?: number;
      macro_hub?: SignalHubResponse | null;
      market_status?: MarketStatus | null;
      universe_source_audit?: UniverseSourceAudit | null;
      peer_relay_audit?: PeerRelayAudit | null;
      last_loaded_at?: string;
    };
  } | null;
}

interface UniverseSourceAudit {
  universe_count?: number;
  fallback_count?: number;
  archive_or_live_count?: number;
}

interface PeerRelayAudit {
  available?: boolean;
  snapshot_count?: number;
  signal_keys?: number;
  resolved_count?: number;
  unresolved_count?: number;
  win_rate?: number | null;
  mean_realized_return?: number | null;
  method_note?: string;
}

interface UniverseAlphaTopItem {
  universe_id: string;
  universe_label: string;
  rank: number;
  score: number;
  symbol: string;
  alpha?: OvernightAlphaSymbolResult;
}

interface MarketStatus {
  status?: string;
  data_sources?: Record<string, unknown>;
  options_priority?: string[];
  price_priority?: string[];
}

interface RunStatus {
  job_id?: string;
  status?: "queued" | "running" | "completed" | "failed" | string;
  progress?: number;
  phase?: string;
  message?: string;
  summary?: string;
}

interface SessionPhase {
  label: string;
  detail: string;
  tone: "idle" | "watch" | "active" | "closed";
  etClock: string;
}

const CANDIDATE_LIMIT = 50;

function numberValue(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return value;
}

function clamp(value: number, min = 0, max = 100): number {
  return Math.min(max, Math.max(min, value));
}

function normalizeScore(value: number | null | undefined): number {
  if (value == null || !Number.isFinite(value)) return 0;
  if (value <= 1) return value * 100;
  return value;
}

function formatPrice(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "--";
  if (value >= 1000) return `$${value.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatPercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "--";
  const pct = value <= 1 ? value * 100 : value;
  return `${pct.toFixed(1)}%`;
}

function formatSignedPercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "--";
  const pct = value <= 1 && value >= -1 ? value * 100 : value;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
}

function formatScore(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "--";
  return normalizeScore(value).toFixed(1);
}

function formatMarketCap(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value <= 0) return "--";
  if (value >= 1_000_000_000_000) return `$${(value / 1_000_000_000_000).toFixed(2)}T`;
  if (value >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(1)}B`;
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(0)}M`;
  return `$${value.toLocaleString("en-US")}`;
}

function etParts(now = new Date()): { weekday: string; hour: number; minute: number; clock: string } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(now);
  const get = (type: string) => parts.find((part) => part.type === type)?.value || "";
  return {
    weekday: get("weekday"),
    hour: Number(get("hour")),
    minute: Number(get("minute")),
    clock: `${get("hour")}:${get("minute")} ET`,
  };
}

function getUsSessionPhase(now = new Date()): SessionPhase {
  const parts = etParts(now);
  const minutes = parts.hour * 60 + parts.minute;
  const weekday = parts.weekday;
  if (weekday === "Sat" || weekday === "Sun") {
    return { label: "周末复盘", detail: "适合复核三层证据与下周观察清单", tone: "closed", etClock: parts.clock };
  }
  if (minutes < 9 * 60 + 30) {
    return { label: "盘前观察", detail: "先看隔夜新闻、GEX 和宏观风险，不急于开仓", tone: "watch", etClock: parts.clock };
  }
  if (minutes < 14 * 60 + 30) {
    return { label: "盘中筛选", detail: "观察启动质量，避免在噪声区追价", tone: "idle", etClock: parts.clock };
  }
  if (minutes < 16 * 60) {
    return { label: "尾盘入场窗口", detail: "符合隔夜研究条件的标的优先复核", tone: "active", etClock: parts.clock };
  }
  if (minutes < 20 * 60) {
    return { label: "盘后复盘", detail: "复核收盘价是否触发失效或隔夜保留条件", tone: "watch", etClock: parts.clock };
  }
  return { label: "隔夜计划", detail: "等待下一交易日，先整理优先级和退出线", tone: "closed", etClock: parts.clock };
}

function statusText(value?: string | null): string {
  if (!value) return "--";
  const map: Record<string, string> = {
    NONE: "无信号",
    WATCH: "观察",
    STRONG_WATCH: "强观察",
    WAITING_CONFIRMATION: "等待确认",
    WEAK_CONFIRMED: "弱确认",
    CONFIRMED: "已确认",
    STRONG_CONFIRMED: "强确认",
    INVALIDATED: "已失效",
    BLOCK_OPEN: "禁止开仓",
    PASS: "通过",
  };
  return map[value] || value;
}

function isBlocked(row: ResearchRow): boolean {
  const openStatus = String(row.open_risk_status || "").toUpperCase();
  const exitAction = String(row.exit_action || "").toUpperCase();
  const eventStatus = String(row.event_radar_status || "");
  return (
    row.macro_systemic_crisis === true ||
    openStatus.includes("BLOCK") ||
    exitAction.includes("EXIT") ||
    eventStatus.includes("回避") ||
    eventStatus.includes("告警")
  );
}

function scoreRow(row: ResearchRow): number {
  let score = 0;
  score += normalizeScore(row.overall_sort_score) * 0.34;
  score += normalizeScore(row.unified_probability) * 0.22;
  score += normalizeScore(row.empirical_win_rate) * 0.1;
  score += normalizeScore(row.proxy_win_rate) * 0.04;
  score += normalizeScore(row.stock_signal_score) * 0.1;
  score += normalizeScore(row.launch_signal_score) * 0.08;
  score += normalizeScore(row.daily_tunnel_score) * 0.07;
  score += normalizeScore(row.pullback_rejection_score) * 0.04;
  score += normalizeScore(row.pullback_confirmation_score) * 0.05;

  const confirmation = String(row.pullback_confirmation_status || "");
  const rejection = String(row.pullback_rejection_status || "");
  if (confirmation === "STRONG_CONFIRMED") score += 9;
  else if (confirmation === "CONFIRMED") score += 6;
  else if (confirmation === "WEAK_CONFIRMED") score += 2;
  if (rejection === "STRONG_WATCH") score += 3;
  else if (rejection === "WATCH") score += 1.5;

  const gexLevel = String(row.gex_level || "").toUpperCase();
  const gexRegime = String(row.gex_regime || "").toLowerCase();
  if (gexLevel === "HIGH" && gexRegime.includes("negative")) score -= 8;
  else if (gexLevel === "HIGH") score -= 4;
  if (isBlocked(row)) score -= 28;
  if ((row.risk_flags || []).length >= 3) score -= 3;
  return clamp(score);
}

function historyLabel(row?: ResearchRow | null): string {
  if (!row) return "--";
  if (row.empirical_win_rate != null) return `实证 ${formatPercent(row.empirical_win_rate)}`;
  if (row.proxy_win_rate != null || row.historical_is_proxy) return `代理 ${formatPercent(row.proxy_win_rate ?? row.historical_win_rate)}`;
  return "样本不足";
}

function universeHistoryLabel(universe?: ResearchUniverseOption | null): string {
  const recent = universe?.recent_win;
  if (!recent) return "历史 --";
  if (recent.historical_win_rate != null) return `实证 ${formatPercent(recent.historical_win_rate)}`;
  if (recent.proxy_win_rate != null || recent.historical_is_proxy) return `代理 ${formatPercent(recent.proxy_win_rate)}`;
  return "历史样本不足";
}

function universeId(universe: ResearchUniverseOption): string {
  return universe.universe || universe.id || "";
}

function universeRecentWin(universe: ResearchUniverseOption): number | null | undefined {
  return universe.recent_win?.recent_win_score ?? universe.recent_win_rate;
}

function universeCandidateCount(universe: ResearchUniverseOption): number {
  return Number(universe.recent_win?.candidate_count ?? universe.candidate_count ?? 0) || 0;
}

function universeHasCompletedSnapshot(universe: ResearchUniverseOption): boolean {
  const recent = universe.recent_win;
  return recent?.status === "ready" || Boolean(recent?.report_run_id) || universeCandidateCount(universe) > 0;
}

function shortPlan(row: ResearchRow): string {
  if (isBlocked(row)) return "风控阻断，只适合人工复核";
  const confirmation = String(row.pullback_confirmation_status || "");
  if (confirmation.includes("CONFIRMED")) return "尾盘复核，隔夜优先级较高";
  if (row.waiting_breakout_price) return "等待突破价附近确认";
  if (row.invalidation_price) return "观察有效，先盯失效线";
  return row.short_reason || "证据不足，放入观察";
}

function llmReviewTone(row?: ResearchRow | null): "neutral" | "good" | "warn" | "bad" | "info" {
  const review = row?.llm_review;
  if (!review?.available) return "neutral";
  const verdict = String(review.verdict || "");
  const risk = String(review.risk_level || "").toLowerCase();
  if (verdict.includes("回避") || risk === "high") return "bad";
  if (verdict.includes("优先") && risk !== "high") return "good";
  if (verdict.includes("观察") || verdict.includes("等待") || risk === "medium") return "warn";
  return "info";
}

function llmReviewLabel(row?: ResearchRow | null): string {
  const review = row?.llm_review;
  if (!review) return "未复核";
  if (!review.available) return review.status === "disabled" ? "已关闭" : "复核不可用";
  return `${review.verdict || "已复核"} · ${review.overnight_action || "人工复核"}`;
}

function llmReviewFocusLabel(row?: ResearchRow | null): string {
  const focus = row?.llm_review?.review_focus;
  if (focus === "peer_relay") return "同行财报接力复核";
  if (focus === "ai_supply_chain") return "AI关键供应链复核";
  if (focus === "three_layer_research") return "三层信号复核";
  return "DeepSeek 决策辅助复核";
}

async function fetchJson<T>(path: string, options?: RequestInit, timeoutMs = 30000): Promise<T> {
  const { headers, ...rest } = options ?? {};
  const mergedHeaders: Record<string, string> = { "Content-Type": "application/json", ...authHeaders() };
  if (headers) {
    new Headers(headers).forEach((value, key) => {
      mergedHeaders[key] = value;
    });
  }
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(path, { ...rest, headers: mergedHeaders, signal: controller.signal });
  } finally {
    window.clearTimeout(timer);
  }
  const text = await res.text();
  let data: unknown = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }
  if (!res.ok) {
    const detail = typeof data === "object" && data && "detail" in data ? String((data as { detail?: unknown }).detail) : `HTTP ${res.status}`;
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data as T;
}

function Pill({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "good" | "warn" | "bad" | "info" }) {
  const tones = {
    neutral: "border-border bg-muted/35 text-muted-foreground",
    good: "border-emerald-500/35 bg-emerald-500/10 text-emerald-300",
    warn: "border-amber-500/35 bg-amber-500/10 text-amber-200",
    bad: "border-red-500/35 bg-red-500/10 text-red-200",
    info: "border-blue-500/35 bg-blue-500/10 text-blue-200",
  };
  return <span className={`inline-flex h-6 items-center rounded-full border px-2 text-xs font-medium ${tones[tone]}`}>{children}</span>;
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-2 text-2xl font-semibold tracking-tight">{value}</div>
      {hint ? <div className="mt-1 truncate text-xs text-muted-foreground">{hint}</div> : null}
    </div>
  );
}

export function Home() {
  const [universes, setUniverses] = useState<ResearchUniverseOption[]>([]);
  const [aggregatedRows, setAggregatedRows] = useState<AggregatedResearchRow[]>([]);
  const [macroHub, setMacroHub] = useState<SignalHubResponse | null>(null);
  const [marketStatus, setMarketStatus] = useState<MarketStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [runStatus, setRunStatus] = useState<RunStatus | null>(null);
  const [lastLoadedAt, setLastLoadedAt] = useState<string>("");
  const [loadedUniverseCount, setLoadedUniverseCount] = useState(0);
  const [failedUniverseCount, setFailedUniverseCount] = useState(0);
  const [overnightAlpha, setOvernightAlpha] = useState<OvernightAlphaSummary | null>(null);
  const [overnightAlphaLoading, setOvernightAlphaLoading] = useState(false);
  const [universeAlphaTop, setUniverseAlphaTop] = useState<UniverseAlphaTopItem[]>([]);
  const [universeSourceAudit, setUniverseSourceAudit] = useState<UniverseSourceAudit | null>(null);
  const [peerRelayAudit, setPeerRelayAudit] = useState<PeerRelayAudit | null>(null);

  const phase = useMemo(() => getUsSessionPhase(), []);

  const sortedUniverses = useMemo(() => {
    return [...universes].sort((a, b) => normalizeScore(universeRecentWin(b)) - normalizeScore(universeRecentWin(a)));
  }, [universes]);

  const scoredRows = useMemo(() => {
    return aggregatedRows
      .map((row) => ({ row, score: scoreRow(row) }))
      .sort((a, b) => b.score - a.score);
  }, [aggregatedRows]);

  const tradableRows = useMemo(() => scoredRows.filter((item) => !isBlocked(item.row)), [scoredRows]);
  const topPick = tradableRows[0] || scoredRows[0] || null;
  const blockedCount = scoredRows.filter((item) => isBlocked(item.row)).length;
  const watchCount = scoredRows.filter((item) => String(item.row.pullback_rejection_status || "").includes("WATCH")).length;
  const confirmCount = scoredRows.filter((item) => String(item.row.pullback_confirmation_status || "").includes("CONFIRMED")).length;

  const snapshotLabel =
    macroHub?.snapshot?.generated_at ||
    macroHub?.snapshot?.data_as_of ||
    macroHub?.snapshot_time ||
    aggregatedRows.find((row) => row.snapshot_time)?.snapshot_time ||
    "--";

  const load = useCallback(async (options: { forceRefresh?: boolean } = {}) => {
    const forceRefresh = Boolean(options.forceRefresh);
    setLoading(true);
    setError("");
    setLoadedUniverseCount(0);
    setFailedUniverseCount(0);
    setOvernightAlpha(null);
    setUniverseAlphaTop([]);
    setUniverseSourceAudit(null);
    setPeerRelayAudit(null);
    try {
      const cachedSnapshot = await fetchJson<HomeDashboardSnapshotResponse>("/home-dashboard/snapshot", undefined, 5000).catch(() => null);
      const cachedPayload = cachedSnapshot?.snapshot?.payload;
      if (cachedSnapshot?.available && cachedPayload) {
        setAggregatedRows(cachedPayload.rows || []);
        setOvernightAlpha(cachedPayload.overnight_alpha || null);
        setUniverseAlphaTop(cachedPayload.universe_alpha_top || []);
        setLoadedUniverseCount(Number(cachedPayload.loaded_universe_count || 0));
        setFailedUniverseCount(Number(cachedPayload.failed_universe_count || 0));
        setMacroHub(cachedPayload.macro_hub || null);
        setMarketStatus(cachedPayload.market_status || null);
        setUniverseSourceAudit(cachedPayload.universe_source_audit || null);
        setPeerRelayAudit(cachedPayload.peer_relay_audit || null);
        setLastLoadedAt(cachedPayload.last_loaded_at || cachedSnapshot.snapshot?.generated_at || "");
        setLoading(false);
        if (!forceRefresh) {
          return;
        }
      }
      const [universePayload, statusData] = await Promise.all([
        fetchJson<{ universes: ResearchUniverseOption[] }>("/research-universes"),
        fetchJson<MarketStatus>("/market-data/status").catch(() => null),
      ]);
      const catalog = universePayload.universes || [];
      setUniverses(catalog);
      setMarketStatus(statusData);
      const sourceAudit = {
        universe_count: catalog.length,
        fallback_count: catalog.filter((item) => item.recent_win?.source_is_fallback).length,
        archive_or_live_count: catalog.filter((item) => ["live", "archive"].includes(String(item.recent_win?.source_kind || ""))).length,
      };
      setUniverseSourceAudit(sourceAudit);
      const peerAudit = await fetchJson<PeerRelayAudit>("/launch-signal/peer-history/summary?limit=300", undefined, 8000).catch(() => null);
      setPeerRelayAudit(peerAudit);

      const completedUniverses = catalog
        .filter((item) => universeId(item) && universeHasCompletedSnapshot(item))
        .sort((a, b) => normalizeScore(universeRecentWin(b)) - normalizeScore(universeRecentWin(a)));

      const batches: ResearchUniverseOption[][] = [];
      for (let index = 0; index < completedUniverses.length; index += 4) {
        batches.push(completedUniverses.slice(index, index + 4));
      }

      const rowMap = new Map<string, AggregatedResearchRow>();
      let loaded = 0;
      let failed = 0;
      let firstHub: SignalHubResponse | null = null;
      const universeTopInputs: Array<{
        meta: Omit<UniverseAlphaTopItem, "alpha">;
        payload: {
          symbol: string;
          source_universe_ids: string[];
          source_pools: string[];
          pullback_rejection_status?: string;
          pullback_confirmation_status?: string;
        };
      }> = [];

      for (const batch of batches) {
        const results = await Promise.allSettled(
          batch.map(async (universe) => {
            const params = new URLSearchParams({ universe: universeId(universe), limit: "30", compact: "true" });
            const hubData = await fetchJson<SignalHubResponse>(`/research-signal-hub?${params.toString()}`, undefined, 25000);
            return { universe, hubData };
          }),
        );

        for (const result of results) {
          if (result.status !== "fulfilled") {
            failed += 1;
            continue;
          }
          loaded += 1;
          const { universe, hubData } = result.value;
          const currentUniverseId = universeId(universe);
          const currentUniverseLabel = universe.label || currentUniverseId;
          if (!firstHub) firstHub = hubData;
          (hubData.rows || [])
            .map((rawRow) => ({
              row: {
                ...rawRow,
                source_universe_ids: [currentUniverseId],
                source_universe_labels: [currentUniverseLabel],
              } as AggregatedResearchRow,
              score: scoreRow(rawRow),
            }))
            .sort((a, b) => b.score - a.score)
            .slice(0, 3)
            .forEach(({ row, score }, index) => {
              const symbol = String(row.symbol || "").toUpperCase();
              if (!symbol) return;
              universeTopInputs.push({
                meta: {
                  universe_id: currentUniverseId,
                  universe_label: currentUniverseLabel,
                  rank: index + 1,
                  score,
                  symbol,
                },
                payload: {
                  symbol,
                  source_universe_ids: [currentUniverseId],
                  source_pools: row.source_pools || [],
                  pullback_rejection_status: row.pullback_rejection_status || undefined,
                  pullback_confirmation_status: row.pullback_confirmation_status || undefined,
                },
              });
            });
          for (const rawRow of hubData.rows || []) {
            const symbol = String(rawRow.symbol || "").toUpperCase();
            if (!symbol) continue;
            const incoming: AggregatedResearchRow = {
              ...rawRow,
              source_universe_ids: [currentUniverseId],
              source_universe_labels: [currentUniverseLabel],
            };
            const existing = rowMap.get(symbol);
            if (!existing) {
              rowMap.set(symbol, incoming);
              continue;
            }
            const mergedIds = Array.from(new Set([...existing.source_universe_ids, currentUniverseId]));
            const mergedLabels = Array.from(new Set([...existing.source_universe_labels, currentUniverseLabel]));
            const keepIncoming = scoreRow(incoming) > scoreRow(existing);
            rowMap.set(symbol, {
              ...(keepIncoming ? incoming : existing),
              source_universe_ids: mergedIds,
              source_universe_labels: mergedLabels,
            });
          }
        }
        setLoadedUniverseCount(loaded);
        setFailedUniverseCount(failed);
      }

      const mergedRows = Array.from(rowMap.values());
      setMacroHub(firstHub);
      setAggregatedRows(mergedRows);
      setLastLoadedAt(new Date().toLocaleString("zh-CN", { hour12: false }));
      let refreshedAlpha: OvernightAlphaSummary | null = null;
      let refreshedUniverseTop: UniverseAlphaTopItem[] = [];
      const alphaRows = mergedRows
        .map((row) => ({ row, score: scoreRow(row) }))
        .sort((a, b) => b.score - a.score)
        .slice(0, 12)
        .map(({ row }) => ({
          symbol: row.symbol || "",
          source_universe_ids: row.source_universe_ids || [],
          source_pools: row.source_pools || [],
          pullback_rejection_status: row.pullback_rejection_status || undefined,
          pullback_confirmation_status: row.pullback_confirmation_status || undefined,
        }))
        .filter((row) => row.symbol);
      if (alphaRows.length) {
        setOvernightAlphaLoading(true);
        try {
          const alpha = await fetchJson<OvernightAlphaSummary>(
            "/overnight-alpha/summary",
            { method: "POST", body: JSON.stringify({ rows: alphaRows, period: "2y", min_edge: 0.001, limit: 12 }) },
            60000,
          );
          refreshedAlpha = alpha;
          setOvernightAlpha(alpha);
          const topInputs = universeTopInputs.slice(0, 30);
          if (topInputs.length) {
            const perUniverseAlpha = await fetchJson<OvernightAlphaSummary>(
              "/overnight-alpha/summary",
              { method: "POST", body: JSON.stringify({ rows: topInputs.map((item) => item.payload), period: "2y", min_edge: 0.001, limit: topInputs.length }) },
              90000,
            );
            refreshedUniverseTop = topInputs.map((item, index) => ({
              ...item.meta,
              alpha: perUniverseAlpha.results?.[index],
            }));
            setUniverseAlphaTop(refreshedUniverseTop);
          }
        } catch {
          setOvernightAlpha(null);
          setUniverseAlphaTop([]);
        } finally {
          setOvernightAlphaLoading(false);
        }
      }
      if (mergedRows.length > 0) {
        await fetchJson(
          "/home-dashboard/snapshot",
          {
            method: "POST",
            body: JSON.stringify({
              source: "home",
              payload: {
                rows: mergedRows,
                overnight_alpha: refreshedAlpha,
                universe_alpha_top: refreshedUniverseTop,
                loaded_universe_count: loaded,
                failed_universe_count: failed,
                macro_hub: firstHub,
                market_status: statusData,
                universe_source_audit: sourceAudit,
                peer_relay_audit: peerAudit,
                last_loaded_at: new Date().toLocaleString("zh-CN", { hour12: false }),
              },
            }),
          },
          10000,
        ).catch(() => undefined);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载统一看板失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, []);

  const rebuildDashboard = async () => {
    if (running) return;
    setRunning(true);
    setError("");
    setRunStatus({ status: "running", progress: 8, phase: "正在整合所有已完成三维快照", message: "首页不再选股票池，会把所有已有三维分析结果合并、去重并重新排序。" });
    try {
      await load({ forceRefresh: true });
      setRunStatus({ status: "completed", progress: 100, phase: "统一看板已刷新", message: "所有可读取的三维快照已经完成整合。" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "无法重新整合看板");
    } finally {
      setRunning(false);
    }
  };

  const progress = clamp(numberValue(runStatus?.progress) ?? (running ? 8 : 0));
  const topRow = topPick?.row;
  const topAlpha = overnightAlpha?.results?.find((item) => String(item.symbol || "").toUpperCase() === String(topRow?.symbol || "").toUpperCase());
  const alphaPortfolio = overnightAlpha?.portfolio || {};
  const universeAlphaGroups = useMemo(() => {
    const groups = new Map<string, UniverseAlphaTopItem[]>();
    for (const item of universeAlphaTop) {
      const key = item.universe_id || item.universe_label;
      groups.set(key, [...(groups.get(key) || []), item]);
    }
    return Array.from(groups.values()).map((items) => items.sort((a, b) => a.rank - b.rank));
  }, [universeAlphaTop]);

  return (
    <div className="min-h-screen bg-background px-4 py-5 text-foreground sm:px-6 lg:px-8">
      <div className="mx-auto flex w-full max-w-[1680px] flex-col gap-5">
        <section className="grid gap-4 lg:grid-cols-[1.4fr_0.95fr_0.9fr]">
          <div className="rounded-lg border border-border bg-card p-5">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <div className="flex flex-wrap items-center gap-2 text-xs font-semibold uppercase tracking-[0.16em] text-primary">
                  <Activity className="h-4 w-4" />
                  StockSignalForge research cockpit
                </div>
                <h1 className="mt-3 text-2xl font-semibold tracking-tight sm:text-3xl">尾盘买入 · 次日开盘卖出 · 隔夜研究驾驶舱</h1>
                <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
                  首页默认读取最近一次三维信号快照，重点展示尾盘到收盘阶段需要复核的标的、隔夜风险、GEX 状态和失效退出线。所有内容仅作为研究优先级，不是确定性买入建议。
                </p>
              </div>
              <div className="flex min-w-[220px] flex-col gap-2 rounded-lg border border-border bg-muted/30 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs text-muted-foreground">当前美股阶段</span>
                  <span className="text-xs font-medium">{phase.etClock}</span>
                </div>
                <div className="flex items-center gap-2">
                  <Clock3 className={`h-5 w-5 ${phase.tone === "active" ? "text-emerald-300" : phase.tone === "watch" ? "text-amber-300" : "text-blue-300"}`} />
                  <span className="text-lg font-semibold">{phase.label}</span>
                </div>
                <p className="text-xs leading-5 text-muted-foreground">{phase.detail}</p>
              </div>
            </div>
          </div>

          <div className="rounded-lg border border-border bg-card p-5">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <Radar className="h-4 w-4 text-primary" />
              统一整合范围
            </div>
            <div className="mt-4 grid grid-cols-2 gap-3">
              <div className="rounded-md border border-border bg-muted/25 p-3">
                <div className="text-xs text-muted-foreground">已读取池</div>
                <div className="mt-1 text-2xl font-semibold">{loadedUniverseCount}</div>
              </div>
              <div className="rounded-md border border-border bg-muted/25 p-3">
                <div className="text-xs text-muted-foreground">失败/超时</div>
                <div className="mt-1 text-2xl font-semibold">{failedUniverseCount}</div>
              </div>
            </div>
            <div className="mt-3 space-y-1 text-xs text-muted-foreground">
              <div>来源：所有已有完成快照的三维分析池</div>
              <div>全局去重标的：{aggregatedRows.length || "--"} · 有效快照池：{sortedUniverses.filter(universeHasCompletedSnapshot).length || "--"}</div>
              <div>当前最高分池：{sortedUniverses[0]?.label || "--"} · {universeHistoryLabel(sortedUniverses[0])}</div>
              <div>
                股票池来源：fallback {universeSourceAudit?.fallback_count ?? "--"} / {universeSourceAudit?.universe_count ?? "--"}
                ，live/archive {universeSourceAudit?.archive_or_live_count ?? "--"}
              </div>
              <div>
                同行接力验证：已兑现 {peerRelayAudit?.resolved_count ?? "--"} / 信号键 {peerRelayAudit?.signal_keys ?? "--"}
                ，胜率 {formatPercent(peerRelayAudit?.win_rate)}
              </div>
              <div>快照：{snapshotLabel}</div>
            </div>
          </div>

          <div className="rounded-lg border border-border bg-card p-5">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <TimerReset className="h-4 w-4 text-primary" />
              操作
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => void load({ forceRefresh: true })}
                disabled={loading || running}
                className="inline-flex h-10 items-center justify-center gap-2 rounded-md border border-border bg-muted/35 px-3 text-sm font-medium hover:bg-muted disabled:cursor-not-allowed disabled:opacity-55"
              >
                <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
                刷新看板
              </button>
              <button
                type="button"
                onClick={() => void rebuildDashboard()}
                disabled={running}
                className="inline-flex h-10 items-center justify-center gap-2 rounded-md bg-primary px-3 text-sm font-semibold text-primary-foreground hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-55"
              >
                {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                重新整合
              </button>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <Link className="inline-flex items-center gap-1 text-xs text-blue-300 hover:text-blue-200" to="/signal-dashboard">
                三维总览 <ExternalLink className="h-3 w-3" />
              </Link>
              <Link className="inline-flex items-center gap-1 text-xs text-blue-300 hover:text-blue-200" to="/stock-signals">
                实股信号 <ExternalLink className="h-3 w-3" />
              </Link>
              <Link className="inline-flex items-center gap-1 text-xs text-blue-300 hover:text-blue-200" to="/launch-signal">
                启动信号 <ExternalLink className="h-3 w-3" />
              </Link>
            </div>
          </div>
        </section>

        {error ? (
          <div className="rounded-lg border border-red-500/35 bg-red-500/10 p-4 text-sm text-red-100">
            {error}
          </div>
        ) : null}

        <section className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-5 shadow-lg shadow-emerald-950/20">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-2 text-sm font-semibold text-emerald-200">
                <BarChart3 className="h-5 w-5" />
                隔夜 Alpha 胜率验证
              </div>
              <h2 className="mt-2 text-xl font-semibold tracking-tight text-emerald-50">
                尾盘买入、次日开盘卖出，按“跑赢所属指数 Beta”计算胜率
              </h2>
              <p className="mt-2 max-w-4xl text-sm leading-6 text-emerald-100/80">
                主口径不是个股绝对涨跌，而是个股隔夜收益减去所属基准的 Beta 调整隔夜收益；Alpha &gt; 0 才算模型选股有效。
              </p>
            </div>
            <Pill tone={overnightAlpha?.available ? "good" : overnightAlphaLoading ? "warn" : "neutral"}>
              {overnightAlphaLoading ? "正在回测" : overnightAlpha?.available ? "已验证" : "等待样本"}
            </Pill>
          </div>
          <div className="mt-4 grid gap-3 md:grid-cols-5">
            <div className="rounded-md border border-emerald-400/25 bg-background/35 p-3">
              <div className="text-xs text-emerald-100/70">Top 候选 Alpha 胜率</div>
              <div className="mt-1 text-3xl font-semibold text-emerald-200">
                {overnightAlphaLoading ? "--" : formatPercent(topAlpha?.stats?.alpha_win_rate)}
              </div>
              <div className="mt-1 text-xs text-emerald-100/65">
                {topRow?.symbol || "--"} vs {topAlpha?.benchmark || "--"} · β {topAlpha?.beta?.toFixed?.(2) || "--"}
              </div>
            </div>
            <div className="rounded-md border border-emerald-400/25 bg-background/35 p-3">
              <div className="text-xs text-emerald-100/70">强 Alpha 胜率</div>
              <div className="mt-1 text-2xl font-semibold text-emerald-100">
                {overnightAlphaLoading ? "--" : formatPercent(topAlpha?.stats?.strong_alpha_win_rate)}
              </div>
              <div className="mt-1 text-xs text-emerald-100/65">Alpha &gt; 成本阈值 0.10%</div>
            </div>
            <div className="rounded-md border border-emerald-400/25 bg-background/35 p-3">
              <div className="text-xs text-emerald-100/70">平均 Beta 调整 Alpha</div>
              <div className="mt-1 text-2xl font-semibold text-emerald-100">
                {overnightAlphaLoading ? "--" : formatSignedPercent(topAlpha?.stats?.mean_beta_adjusted_alpha)}
              </div>
              <div className="mt-1 text-xs text-emerald-100/65">同类历史信号均值</div>
            </div>
            <div className="rounded-md border border-emerald-400/25 bg-background/35 p-3">
              <div className="text-xs text-emerald-100/70">Top12 加权 Alpha 胜率</div>
              <div className="mt-1 text-2xl font-semibold text-emerald-100">
                {overnightAlphaLoading ? "--" : formatPercent(alphaPortfolio.alpha_win_rate)}
              </div>
              <div className="mt-1 text-xs text-emerald-100/65">样本 {alphaPortfolio.sample_count ?? "--"} · 标的 {alphaPortfolio.symbol_count ?? "--"}</div>
            </div>
            <div className="rounded-md border border-emerald-400/25 bg-background/35 p-3">
              <div className="text-xs text-emerald-100/70">最大连续 Alpha 亏损</div>
              <div className="mt-1 text-2xl font-semibold text-emerald-100">
                {overnightAlphaLoading ? "--" : topAlpha?.stats?.max_consecutive_alpha_losses ?? "--"}
              </div>
              <div className="mt-1 text-xs text-emerald-100/65">样本 {topAlpha?.stats?.sample_count ?? "--"} · 周期 {overnightAlpha?.period || "2y"}</div>
            </div>
          </div>
          <div className="mt-3 text-xs leading-5 text-emerald-100/70">
            {overnightAlpha?.note || "历史验证会在首页候选聚合后自动运行；样本不足时不显示胜率，不用大模型估算。"}
          </div>
          <div className="mt-5 border-t border-emerald-400/20 pt-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-sm font-semibold text-emerald-100">各指数池 Top3 历史 Alpha</div>
                <div className="text-xs text-emerald-100/65">每个池按本池三维分数取前三，再用该池对应指数 ETF 做 Beta 调整验证。</div>
              </div>
              <Pill tone={universeAlphaGroups.length ? "good" : overnightAlphaLoading ? "warn" : "neutral"}>
                {universeAlphaGroups.length ? `${universeAlphaGroups.length} 个池` : overnightAlphaLoading ? "计算中" : "暂无池内Top3"}
              </Pill>
            </div>
            <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {universeAlphaGroups.length ? (
                universeAlphaGroups.slice(0, 12).map((items) => (
                  <div key={items[0]?.universe_id || items[0]?.universe_label} className="rounded-md border border-emerald-400/20 bg-background/30 p-3">
                    <div className="truncate text-sm font-semibold text-emerald-100">{items[0]?.universe_label || "--"}</div>
                    <div className="mt-2 space-y-2">
                      {items.map((item) => (
                        <div key={`${item.universe_id}-${item.rank}-${item.symbol}`} className="grid grid-cols-[auto_1fr_auto] items-center gap-2 text-xs">
                          <span className="flex h-5 w-5 items-center justify-center rounded bg-emerald-500/15 text-emerald-200">{item.rank}</span>
                          <div className="min-w-0">
                            <div className="truncate font-semibold text-emerald-50">
                              {item.symbol}
                              <span className="ml-1 text-emerald-100/55">vs {item.alpha?.benchmark || "--"}</span>
                            </div>
                            <div className="truncate text-emerald-100/55">
                              样本 {item.alpha?.stats?.sample_count ?? "--"} · 均值 {formatSignedPercent(item.alpha?.stats?.mean_beta_adjusted_alpha)}
                            </div>
                          </div>
                          <div className="text-right">
                            <div className="font-semibold text-emerald-100">{formatPercent(item.alpha?.stats?.alpha_win_rate)}</div>
                            <div className="text-emerald-100/50">Alpha</div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))
              ) : (
                <div className="rounded-md border border-emerald-400/20 bg-background/30 p-3 text-sm text-emerald-100/65">
                  {overnightAlphaLoading ? "正在计算各指数池 Top3 历史 Alpha..." : "暂无可展示的指数池 Top3。"}
                </div>
              )}
            </div>
          </div>
        </section>

        {running ? (
          <section className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4">
            <div className="flex items-center justify-between gap-3 text-sm">
              <div className="font-semibold text-amber-100">{runStatus?.phase || "三层分析运行中"}</div>
              <div className="text-amber-100">{progress.toFixed(0)}%</div>
            </div>
            <div className="mt-3 h-2 overflow-hidden rounded-full bg-background">
              <div className="h-full rounded-full bg-amber-400 transition-all" style={{ width: `${progress}%` }} />
            </div>
            <div className="mt-2 text-xs text-amber-100/80">{runStatus?.message || runStatus?.summary || "正在更新行情、公告/新闻和三维信号快照。"}</div>
          </section>
        ) : null}

        <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <Metric label="候选总数" value={loading ? "--" : String(scoredRows.length)} hint={`展示 Top ${Math.min(CANDIDATE_LIMIT, scoredRows.length)} 快照候选`} />
          <Metric label="回调承接/确认" value={`${watchCount} / ${confirmCount}`} hint="观察承接与启动确认的衔接质量" />
          <Metric label="风险阻断" value={String(blockedCount)} hint="BLOCK_OPEN、事件告警或宏观危机标记" />
          <Metric
            label="行情数据源"
            value={(marketStatus?.price_priority || []).slice(0, 2).join(" / ") || "缓存快照"}
            hint={`最后刷新 ${lastLoadedAt || "--"}`}
          />
        </section>

        <section className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
          <div className="rounded-lg border border-border bg-card p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <Zap className="h-5 w-5 text-amber-300" />
                <div>
                  <div className="text-sm font-semibold">今晚优先复核标的</div>
                  <div className="text-xs text-muted-foreground">按综合研究分、启动确认、GEX 与风控阻断筛选</div>
                </div>
              </div>
              {topPick ? <Pill tone={isBlocked(topPick.row) ? "bad" : "good"}>{isBlocked(topPick.row) ? "仅复核" : "可进入尾盘复核"}</Pill> : <Pill>暂无候选</Pill>}
            </div>

            {loading ? (
              <div className="mt-8 flex h-48 items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 正在读取最近快照
              </div>
            ) : topRow ? (
              <div className="mt-5 grid gap-4 lg:grid-cols-[0.85fr_1.15fr]">
                <div className="rounded-lg border border-border bg-muted/25 p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="text-4xl font-semibold tracking-tight">{topRow.symbol || "--"}</div>
                      <div className="mt-2 text-sm text-muted-foreground">
                        现价 {formatPrice(topRow.current_price)} · 市值 {formatMarketCap(topRow.market_cap)} · PE {topRow.pe ? `${topRow.pe.toFixed(1)}x` : "--"}
                      </div>
                      <div className="mt-2 line-clamp-2 text-xs text-blue-200">
                        来源池：{topRow.source_universe_labels?.slice(0, 4).join(" / ") || "--"}
                        {(topRow.source_universe_labels?.length || 0) > 4 ? ` 等 ${topRow.source_universe_labels.length} 个池` : ""}
                      </div>
                    </div>
                    <div className="text-right">
                      <div className="text-xs text-muted-foreground">综合分</div>
                      <div className="text-3xl font-semibold text-emerald-300">{topPick?.score.toFixed(1)}</div>
                    </div>
                  </div>
                  <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
                    <div>
                      <div className="text-xs text-muted-foreground">统一概率</div>
                      <div className="font-semibold">{formatPercent(topRow.unified_probability)}</div>
                    </div>
                    <div>
                      <div className="text-xs text-muted-foreground">历史口径</div>
                      <div className="font-semibold">{historyLabel(topRow)}</div>
                    </div>
                    <div>
                      <div className="text-xs text-muted-foreground">等待突破</div>
                      <div className="font-semibold">{formatPrice(topRow.waiting_breakout_price)}</div>
                    </div>
                    <div>
                      <div className="text-xs text-muted-foreground">失效退出</div>
                      <div className="font-semibold">{formatPrice(topRow.invalidation_price)}</div>
                    </div>
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="rounded-lg border border-border bg-muted/25 p-4">
                    <div className="flex items-center gap-2 text-sm font-semibold">
                      <TrendingUp className="h-4 w-4 text-emerald-300" />
                      尾盘条件
                    </div>
                    <div className="mt-3 space-y-2 text-sm">
                      <div className="flex justify-between gap-2"><span className="text-muted-foreground">回调承接</span><span>{statusText(topRow.pullback_rejection_status)}</span></div>
                      <div className="flex justify-between gap-2"><span className="text-muted-foreground">启动确认</span><span>{statusText(topRow.pullback_confirmation_status)}</span></div>
                      <div className="flex justify-between gap-2"><span className="text-muted-foreground">日隧道协商</span><span>{topRow.daily_tunnel_label || "--"}</span></div>
                      <div className="flex justify-between gap-2"><span className="text-muted-foreground">风报比</span><span>{formatScore(topRow.reward_risk_ratio)}</span></div>
                    </div>
                  </div>
                  <div className="rounded-lg border border-border bg-muted/25 p-4">
                    <div className="flex items-center gap-2 text-sm font-semibold">
                      <ShieldAlert className="h-4 w-4 text-amber-300" />
                      隔夜风险
                    </div>
                    <div className="mt-3 space-y-2 text-sm">
                      <div className="flex justify-between gap-2"><span className="text-muted-foreground">GEX</span><span>{topRow.gex_level || "--"} / {topRow.gex_regime || "--"}</span></div>
                      <div className="flex justify-between gap-2"><span className="text-muted-foreground">事件雷达</span><span>{topRow.event_radar_status || "--"}</span></div>
                      <div className="flex justify-between gap-2"><span className="text-muted-foreground">开仓风控</span><span>{statusText(topRow.open_risk_status)}</span></div>
                      <div className="flex justify-between gap-2"><span className="text-muted-foreground">宏观</span><span>{topRow.macro_regime_label || topRow.macro_regime_mode_cn || "--"}</span></div>
                    </div>
                  </div>
                  <div className="sm:col-span-2 rounded-lg border border-border bg-background p-4">
                    <div className="text-xs text-muted-foreground">研究动作</div>
                    <div className="mt-2 text-base font-semibold">{shortPlan(topRow)}</div>
                    <div className="mt-2 text-sm leading-6 text-muted-foreground">{topRow.short_reason || topRow.open_risk_reason || topRow.exit_reason || "若尾盘仍保持信号强度，人工复核成交量、公告和隔夜风险；跌破失效价则取消研究假设。"}</div>
                  </div>
                  <div className="sm:col-span-2 rounded-lg border border-blue-500/30 bg-blue-500/10 p-4">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="text-xs text-blue-100/75">{llmReviewFocusLabel(topRow)}</div>
                      <Pill tone={llmReviewTone(topRow)}>{llmReviewLabel(topRow)}</Pill>
                    </div>
                    <div className="mt-2 text-sm font-semibold text-blue-50">
                      {topRow.llm_review?.summary || "尚无 DeepSeek 复核缓存。请在三维总览运行三层分析，完成后会对 Top 候选生成复核意见。"}
                    </div>
                    {topRow.llm_review?.available && (topRow.llm_review.specialist_notes || []).length ? (
                      <div className="mt-2 rounded border border-blue-300/20 bg-blue-950/25 px-3 py-2 text-xs leading-5 text-blue-100/85">
                        {(topRow.llm_review.specialist_notes || []).slice(0, 2).join("；")}
                      </div>
                    ) : null}
                    {topRow.llm_review?.available ? (
                      <div className="mt-3 grid gap-2 text-xs text-blue-100/80 md:grid-cols-2">
                        <div>
                          <div className="font-semibold text-blue-100">支持证据</div>
                          <div className="mt-1 line-clamp-3">{(topRow.llm_review.supporting_points || []).join("；") || "--"}</div>
                        </div>
                        <div>
                          <div className="font-semibold text-blue-100">反对/风险</div>
                          <div className="mt-1 line-clamp-3">{(topRow.llm_review.objections || topRow.llm_review.risk_flags || []).join("；") || "--"}</div>
                        </div>
                      </div>
                    ) : null}
                  </div>
                </div>
              </div>
            ) : (
              <div className="mt-8 flex h-48 items-center justify-center text-sm text-muted-foreground">当前股票池暂无完成快照，请先运行三层分析。</div>
            )}
          </div>

          <div className="rounded-lg border border-border bg-card p-5">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <AlertTriangle className="h-4 w-4 text-amber-300" />
              收盘前检查清单
            </div>
            <div className="mt-4 space-y-3">
              {[
                ["宏观 VIX/恐慌", macroHub?.macro_regime?.label_cn || macroHub?.macro_regime?.mode_cn || "读取快照", macroHub?.macro_regime?.systemic_crisis ? "bad" : "good"],
                ["GEX 状态", topRow ? `${topRow.gex_level || "--"} / ${topRow.gex_regime || "--"}` : "--", topRow?.gex_level === "HIGH" && String(topRow?.gex_regime || "").includes("negative") ? "warn" : "info"],
                ["启动确认", topRow ? statusText(topRow.pullback_confirmation_status) : "--", String(topRow?.pullback_confirmation_status || "").includes("CONFIRMED") ? "good" : "warn"],
                ["事件风险", topRow?.event_radar_status || "--", String(topRow?.event_radar_status || "").includes("告警") ? "bad" : "neutral"],
                ["失效退出", topRow?.invalidation_price ? formatPrice(topRow.invalidation_price) : "--", "neutral"],
              ].map(([label, value, tone]) => (
                <div key={label} className="flex items-center justify-between gap-3 rounded-lg border border-border bg-muted/25 px-3 py-2">
                  <div className="flex min-w-0 items-center gap-2">
                    {tone === "good" ? <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-300" /> : <AlertTriangle className="h-4 w-4 shrink-0 text-amber-300" />}
                    <span className="truncate text-sm">{label}</span>
                  </div>
                  <span className="truncate text-right text-sm text-muted-foreground">{String(value)}</span>
                </div>
              ))}
            </div>
            <div className="mt-4 rounded-lg border border-border bg-background p-3 text-xs leading-5 text-muted-foreground">
              这块只用于把“能不能隔夜”前置到首页。若宏观恐慌、GEX 负 Gamma、重大事件或开仓风控阻断出现，首页会把标的降级为人工复核。
            </div>
          </div>
        </section>

        <section className="rounded-lg border border-border bg-card">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-4">
            <div className="flex items-center gap-2">
              <BarChart3 className="h-5 w-5 text-primary" />
              <div>
                <div className="text-sm font-semibold">尾盘隔夜候选排名</div>
                <div className="text-xs text-muted-foreground">按首页隔夜研究分排序，风险阻断标的自动降权</div>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <Pill tone="info">Top {Math.min(12, scoredRows.length)}</Pill>
              <Pill tone={blockedCount ? "warn" : "good"}>阻断 {blockedCount}</Pill>
            </div>
          </div>

          <div className="max-h-[560px] overflow-y-auto">
            <table className="w-full table-fixed text-left text-sm">
              <thead className="sticky top-0 z-10 bg-muted text-xs text-muted-foreground">
                <tr>
                  <th className="w-[13%] px-4 py-3">标的</th>
                  <th className="w-[10%] px-3 py-3">现价</th>
                  <th className="w-[10%] px-3 py-3">隔夜分</th>
                  <th className="w-[12%] px-3 py-3">统一概率</th>
                  <th className="w-[13%] px-3 py-3">回调/确认</th>
                  <th className="w-[13%] px-3 py-3">GEX</th>
                  <th className="w-[12%] px-3 py-3">等待突破</th>
                  <th className="w-[12%] px-3 py-3">失效价</th>
                  <th className="w-[15%] px-3 py-3">研究动作</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr>
                    <td className="px-4 py-8 text-center text-muted-foreground" colSpan={9}>
                      <Loader2 className="mr-2 inline h-4 w-4 animate-spin" /> 正在读取候选
                    </td>
                  </tr>
                ) : scoredRows.length ? (
                  scoredRows.slice(0, 12).map(({ row, score }, index) => (
                    <tr key={`${row.symbol || index}-${index}`} className="border-t border-border hover:bg-muted/25">
                      <td className="px-4 py-3">
                        <div className="font-semibold">{row.symbol || "--"}</div>
                        <div className="truncate text-xs text-muted-foreground">{formatMarketCap(row.market_cap)} · PE {row.pe ? row.pe.toFixed(1) : "--"}</div>
                        <div className="truncate text-xs text-blue-200/85">{row.source_universe_labels?.slice(0, 2).join(" / ") || "--"}</div>
                      </td>
                      <td className="px-3 py-3">{formatPrice(row.current_price)}</td>
                      <td className="px-3 py-3 font-semibold text-emerald-300">{score.toFixed(1)}</td>
                      <td className="px-3 py-3">
                        <div>{formatPercent(row.unified_probability)}</div>
                        <div className="text-xs text-muted-foreground">{historyLabel(row)}</div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="truncate">{statusText(row.pullback_rejection_status)}</div>
                        <div className="truncate text-xs text-muted-foreground">{statusText(row.pullback_confirmation_status)}</div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="truncate">{row.gex_level || "--"}</div>
                        <div className="truncate text-xs text-muted-foreground">{row.gex_regime || "--"}</div>
                      </td>
                      <td className="px-3 py-3">{formatPrice(row.waiting_breakout_price)}</td>
                      <td className="px-3 py-3">{formatPrice(row.invalidation_price)}</td>
                      <td className="px-3 py-3">
                        <div className="line-clamp-2 text-xs leading-5 text-muted-foreground">{shortPlan(row)}</div>
                        <div className="mt-1 line-clamp-2 text-xs leading-5 text-blue-200/85">
                          {llmReviewFocusLabel(row)}：{row.llm_review?.summary || llmReviewLabel(row)}
                        </div>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td className="px-4 py-8 text-center text-muted-foreground" colSpan={9}>
                      当前股票池暂无候选快照。
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  );
}
