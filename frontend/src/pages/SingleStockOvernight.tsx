import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  Bot,
  Loader2,
  RefreshCw,
  Search,
  ShieldAlert,
  Star,
  Target,
} from "lucide-react";
import { toast } from "sonner";
import { authHeaders } from "@/lib/apiAuth";
import { cn } from "@/lib/utils";
import { CandlestickChart } from "@/components/charts/CandlestickChart";
import { GildataEvidencePanel, type GildataEvidence } from "@/components/GildataEvidence";
import { api, type DistributionRisk, type EarningsInfo, type PriceBar, type TradeMarker, type NewsDigest } from "@/lib/api";

type AnyRecord = Record<string, any>;

interface SearchItem {
  symbol: string;
  name: string;
  exchange?: string;
  exchange_display?: string;
  quote_type?: string;
  source?: string;
  has_history?: boolean;
}

interface MetricRow {
  key: string;
  label: string;
  plain_meaning: string;
  current_value: number | null;
  percentile: number | null;
  alpha_correlation: number | null;
  next_open_correlation: number | null;
  sample_count: number;
  direction_cn: string;
}

interface SingleStockResponse {
  symbol: string;
  page_revision?: string;
  row: AnyRecord;
  day_change?: {
    price: number;
    prev_close: number;
    change: number;
    change_pct: number;
    as_of?: string | null;
  } | null;
  overnight_alpha?: AnyRecord;
  correlation?: {
    available?: boolean;
    reason?: string;
    benchmark?: string;
    benchmark_source_universe?: string;
    beta?: number | null;
    sample_count?: number;
    data_sources?: Record<string, string>;
    metrics?: MetricRow[];
    price_line?: { date: string; close: number }[];
  };
  volatility?: {
    hv20?: number | null;
    hv30?: number | null;
    iv?: AnyRecord;
  };
  distribution_risk?: DistributionRisk | null;
  three_month_comparison?: ThreeMonthComparison;
  action_plan?: AnyRecord;
  deepseek_summary?: AnyRecord;
  metric_benchmarks?: {
    key: string;
    label: string;
    value: number;
    fmt: string;
    ref: string;
    verdict: string;
    tone: string;
    leader_value?: number | null;
    leader_symbol?: string | null;
    hint?: string;
  }[];
  fib_levels?: {
    available: boolean;
    trend?: string;
    trend_cn?: string;
    swing_high?: number;
    swing_low?: number;
    window_days?: number;
    current_price?: number;
    levels?: { ratio: number; label: string; price: number }[];
    zone?: { upper: { label: string; price: number }; lower: { label: string; price: number } } | null;
    shallow_zone?: { low: number; high: number } | null;
    in_shallow_zone?: boolean;
    evidence?: string;
    note?: string;
  };
  news?: NewsDigest;
  signal_read?: SignalRead;
  company_profile?: CompanyProfile;
  earnings?: EarningsInfo | null;
  leader_compare?: LeaderCompare;
  note?: string;
}

interface LeaderMember {
  symbol: string;
  is_query?: boolean;
  adv?: number | null;
  liquidity?: { adv?: number | null; tier?: string; tone?: string };
  current_price?: number | null;
  calibrated_win_rate?: number | null;
  pullback_state?: string | null;
  in_pullback?: boolean;
}

interface LeaderCompare {
  available?: boolean;
  reason?: string;
  track_cn?: string | null;
  leader?: string;
  query_is_leader?: boolean;
  members?: LeaderMember[];
  verdict?: string;
  note?: string;
}

interface CompanyProfile {
  available?: boolean;
  symbol?: string;
  market_session?: string | null;
  market_cap_meta?: {
    source?: string;
    fetched_at?: string | null;
    data_as_of_date?: string | null;
    stale?: boolean;
  } | null;
  gildata_research?: {
    as_of?: string;
    source?: string;
    is_latest_session?: boolean;
    ratings?: {
      buy?: number;
      overweight?: number;
      neutral?: number;
      underweight?: number;
      sell?: number;
      total?: number;
      window_days?: number;
    } | null;
    target_avg_usd?: number | null;
    target_count?: number | null;
    target_window_days?: number | null;
    pe?: number | null;
    pb?: number | null;
    ps?: number | null;
    daily_quote?: { close?: number; as_of?: string; is_realtime?: boolean; adjustment?: string } | null;
    annual_eps_estimates?: { report_period: string; mean: number; count?: number; window_days?: number }[];
    evidence?: GildataEvidence;
  } | null;
  facts?: {
    name?: string | null;
    sector?: string | null;
    industry?: string | null;
    country?: string | null;
    employees?: number | null;
    website?: string | null;
    market_cap?: number | null;
    business_summary_en?: string | null;
    revenue_yoy?: number | null;
  };
  earnings?: {
    next_date?: string | null;
    days_until?: number | null;
    eps_estimated?: number | null;
    revenue_estimated?: number | null;
    last_surprise_pct?: number | null;
    estimate_tone?: string;
    estimate_basis?: string;
  } | null;
  analyst?: {
    as_of?: string | null;
    strong_buy?: number | null;
    buy?: number | null;
    hold?: number | null;
    sell?: number | null;
    strong_sell?: number | null;
    target_avg_quarter?: number | null;
    target_count_quarter?: number | null;
    rating_stats?: {
      signal?: string;
      directional_buy_share?: number | null;
      wilson_buy_lower_95?: number | null;
      wilson_sell_lower_95?: number | null;
      method?: string;
    } | null;
  } | null;
  ai_available?: boolean;
  ai_note?: string;
  business_cn?: string;
  vendor_business_cn?: string;
  segment_cn?: string;
  products?: string[];
  upstream_suppliers?: string[];
  downstream_customers?: string[];
  competitors?: string[];
  moat?: string;
  track_position?: string;
  model?: string;
}

interface SignalRead {
  available?: boolean;
  reason?: string;
  calibrated_win_rate?: number | null;
  calibration_source?: string;
  confidence_badge?: "validated" | "low_edge" | "experimental";
  signal_basis?: string;
  horizon_days?: number;
  pullback_state?: string;
  hv?: { hv_rise?: number | null; hv_state?: string } | null;
  relative_strength_20d?: number | null;
  raw_launch_score?: number | null;
  daily_tunnel_score?: number | null;
}

interface ComparisonPoint {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
}

interface ThreeMonthComparison {
  available?: boolean;
  reason?: string;
  symbol?: string;
  benchmark?: string;
  stock?: ComparisonPoint[];
  benchmark_series?: ComparisonPoint[];
  stock_return?: number;
  benchmark_return?: number;
  relative_return?: number;
}

function fmtNumber(value: unknown, digits = 1) {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toFixed(digits);
}

function fmtMoney(value: unknown) {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) return "--";
  if (number >= 1000) return `$${number.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  return `$${number.toFixed(2)}`;
}

function fmtPct(value: unknown, digits = 1) {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${(number * 100).toFixed(digits)}%`;
}

function fmtSignedPct(value: unknown, digits = 1) {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) return "--";
  const pct = number * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(digits)}%`;
}

function pctTone(value: unknown): "good" | "bad" | "neutral" {
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "neutral";
  return number > 0 ? "good" : "bad";
}

function compactMoney(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  if (Math.abs(number) >= 1e9) return `$${(number / 1e9).toFixed(1)}B`;
  if (Math.abs(number) >= 1e6) return `$${(number / 1e6).toFixed(0)}M`;
  return fmtMoney(number);
}

function EarningsExpectationsCard({ earnings }: { earnings?: EarningsInfo | null }) {
  if (!earnings?.available) return null;
  const days = typeof earnings.days_until === "number" ? earnings.days_until : null;
  const near = days !== null && days >= 0 && days <= 10;
  const tone = earnings.estimate_tone === "positive" ? "good" : earnings.estimate_tone === "warning" || near ? "warn" : "neutral";
  return (
    <section className={cn(
      "rounded border bg-card p-4",
      near && "border-amber-500/50 bg-amber-500/5",
      earnings.estimate_tone === "positive" && !near && "border-emerald-500/40 bg-emerald-500/5",
    )}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="font-semibold">下一次财报预期</h2>
          <p className="text-xs text-muted-foreground">来自当前可用财报日历/预期数据；预期方向只是风险提示，不是财报结果预测。</p>
        </div>
        <StatusPill tone={tone}>
          {near ? "临近财报" : earnings.estimate_tone === "positive" ? "预期偏正面" : earnings.estimate_tone === "warning" ? "预期需警惕" : "中性"}
        </StatusPill>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="预计披露日期" value={earnings.next_date || "--"} detail={days !== null ? `距离现在约 ${days} 天` : "披露日待确认"} />
        <Metric label="EPS 预期" value={earnings.eps_estimated == null ? "--" : String(earnings.eps_estimated)} detail={earnings.eps_estimate_vs_last_actual == null ? "缺少上次实际EPS对比" : `较上次实际 ${fmtSignedPct(earnings.eps_estimate_vs_last_actual)}`} />
        <Metric label="营收预期" value={compactMoney(earnings.revenue_estimated)} detail="若数据源未提供则为空" />
        <Metric label="上次EPS Surprise" value={fmtSignedPct(earnings.last_surprise_pct)} detail={earnings.last_report_date ? `上次披露 ${earnings.last_report_date}` : "上次披露日未知"} />
      </div>
      <div className={cn(
        "mt-3 rounded border px-3 py-2 text-sm",
        earnings.estimate_tone === "positive"
          ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
          : earnings.estimate_tone === "warning" || near
            ? "border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-300"
            : "border-border bg-muted/30 text-muted-foreground",
      )}>
        {earnings.estimate_basis || "未形成明确正/负预期信号。"}
        {near && " 距离财报较近，盘末买入/隔夜持有需要额外考虑跳空风险。"}
      </div>
    </section>
  );
}

function DistributionRiskCard({ risk }: { risk?: DistributionRisk | null }) {
  if (!risk?.available || !risk.triggered) return null;
  const high = risk.level === "high";
  const reasons = risk.reasons ?? [];
  return (
    <section
      className={cn(
        "rounded border p-4",
        high
          ? "border-red-500/50 bg-red-500/10 text-red-700 dark:text-red-300"
          : "border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-300",
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 font-semibold">
            <AlertTriangle className="h-4 w-4" />
            {risk.label || "高位派发风险"}
          </h2>
          <p className="mt-1 text-sm opacity-90">
            成交量明显放大但价格推进不足，需警惕冲高回落、放量横盘或高位筹码交换。
          </p>
        </div>
        <StatusPill tone={high ? "bad" : "warn"}>{high ? "强警示" : "观察警示"}</StatusPill>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="量能放大" value={risk.volume_ratio ? `${fmtNumber(risk.volume_ratio, 2)}x` : "--"} detail="相对20日中位成交量" />
        <Metric label="收盘位置" value={fmtPct(risk.close_location)} detail="越靠近低位，冲高回落越明显" />
        <Metric label="上影线占比" value={fmtPct(risk.upper_shadow_pct)} detail={risk.failed_breakout ? "盘中新高未站稳" : "占当日振幅比例"} />
        <Metric label="单日涨跌" value={fmtSignedPct(risk.day_return)} detail="放量但涨幅有限会提高警示级别" />
      </div>
      {reasons.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {reasons.slice(0, 5).map((reason) => (
            <span key={reason} className="rounded border border-current/20 bg-background/40 px-2 py-0.5 text-xs">
              {reason}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}

function OptionVolatilityCard({ iv, hv20 }: { iv?: AnyRecord | null; hv20?: number | null }) {
  if (!iv) return null;
  if (!iv.available) {
    return (
      <section className="rounded border bg-card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-semibold">期权波动率分析</h2>
          <StatusPill tone="neutral">暂不可用</StatusPill>
        </div>
        <p className="mt-2 text-sm text-muted-foreground">{iv.status_cn || iv.reason || "当前没有可用期权快照。"}</p>
      </section>
    );
  }
  const source = String(iv.source || "");
  const isMassive = source.includes("massive");
  const isDatabento = source.includes("databento");
  const primaryAttempt = iv.primary_source_attempt as AnyRecord | undefined;
  const massiveFallback =
    primaryAttempt?.source === "massive" && primaryAttempt?.available === false
      ? String(primaryAttempt.reason || "unavailable")
      : "";
  const greeks = (iv.greeks || {}) as AnyRecord;
  const callVolume = typeof iv.call_volume === "number" && Number.isFinite(iv.call_volume) ? iv.call_volume : null;
  const putVolume = typeof iv.put_volume === "number" && Number.isFinite(iv.put_volume) ? iv.put_volume : null;
  const callOi = typeof iv.call_open_interest === "number" && Number.isFinite(iv.call_open_interest) ? iv.call_open_interest : null;
  const putOi = typeof iv.put_open_interest === "number" && Number.isFinite(iv.put_open_interest) ? iv.put_open_interest : null;
  const callPutRatio = callVolume !== null && putVolume !== null && putVolume > 0 ? iv.call_put_ratio : null;
  const callPutOiRatio = callOi !== null && putOi !== null && putOi > 0 ? iv.call_put_oi_ratio : null;
  const activityMissing = callVolume === null && putVolume === null && callOi === null && putOi === null;
  return (
    <section className="rounded border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="font-semibold">期权波动率分析</h2>
          <p className="text-xs text-muted-foreground">
            {isMassive
              ? "Massive 当前期权快照：IV / Greeks / OI / Call-Put。"
              : isDatabento
                ? "Databento OPRA 延迟报价：用 CBBO 中间价反推 IV；OI/成交量不伪造。"
                : "当前可用期权 IV；Greeks/Call-Put 取决于数据源。"}
          </p>
        </div>
        <StatusPill tone={isMassive || isDatabento ? "good" : "neutral"}>{isMassive ? "Massive" : isDatabento ? "Databento" : iv.source || "IV"}</StatusPill>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="隐含波动率 IV" value={fmtPct(iv.atm_iv)} detail={`到期 ${iv.expiry || "--"} · DTE ${iv.dte ?? "--"}`} />
        <Metric label="历史波动率 HV20" value={fmtPct(hv20 ?? iv.hv20)} detail={iv.state_cn || "用近20日日收益率年化"} />
        <Metric label="IV / HV20" value={fmtNumber(iv.iv_hv20, 2)} detail=">1 表示期权隐含波动高于近期真实波动" />
        <Metric label="Call / Put Ratio" value={fmtNumber(callPutRatio, 2)} detail={`成交量 Call ${callVolume ?? "--"} / Put ${putVolume ?? "--"}`} />
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Delta" value={fmtNumber(greeks.delta, 3)} detail="ATM call/put 均值，仅供方向敏感度参考" />
        <Metric label="Gamma" value={fmtNumber(greeks.gamma, 4)} detail="价格变动时 Delta 的变化速度" />
        <Metric label="Theta" value={fmtNumber(greeks.theta, 3)} detail="时间价值衰减，通常为负" />
        <Metric label="Vega" value={fmtNumber(greeks.vega, 3)} detail="IV 变化对期权价格的敏感度" />
      </div>
      {massiveFallback ? (
        <div className="mt-3 rounded border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
          Massive 期权快照不可用：{massiveFallback}；当前已退回备用 IV 数据源。
          {massiveFallback === "SSLError" ? " 这通常是本机/容器 TLS 证书链或网络代理导致，不代表该标的没有期权。" : ""}
        </div>
      ) : null}
      <div className="mt-3 rounded border bg-muted/30 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
        OI Call/Put：{fmtNumber(callPutOiRatio, 2)} · Call OI {callOi ?? "--"} / Put OI {putOi ?? "--"}。
        {activityMissing ? " 当前数据源未返回成交量/OI 活动字段；这不是 OI=0。" : " "}
        {isDatabento && iv.as_of_date ? `Databento OPRA 日期：${iv.as_of_date}；` : ""}
        IV Rank / IV Percentile 需要历史 IV 序列；当前卡片先展示可用实时/延迟快照，不伪造历史分位。
      </div>
    </section>
  );
}

const SUMMARY_KEY_RE =
  /(\$\d[\d,]*\.?\d*|[+-]?\d+(?:\.\d+)?%|买入|卖出|止损|止盈|看多|看空|谨慎|突破|跌破|支撑|阻力|回踩|强势|风险|偏多|偏空|观望|阻断)/g;
const SUMMARY_KEY_TEST =
  /^(?:\$\d[\d,]*\.?\d*|[+-]?\d+(?:\.\d+)?%|买入|卖出|止损|止盈|看多|看空|谨慎|突破|跌破|支撑|阻力|回踩|强势|风险|偏多|偏空|观望|阻断)$/;

function HighlightedText({ text }: { text: string }) {
  if (!text) return null;
  const parts = text.split(SUMMARY_KEY_RE);
  return (
    <>
      {parts.map((part, index) =>
        SUMMARY_KEY_TEST.test(part) ? (
          <strong key={index} className="rounded bg-primary/10 px-1 font-semibold text-primary">
            {part}
          </strong>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </>
  );
}

function metricValue(metric: MetricRow) {
  if (metric.current_value == null) return "--";
  if (metric.key === "volume_ratio") return `${fmtNumber(metric.current_value, 2)}x`;
  return fmtPct(metric.current_value);
}

function scoreTone(value: unknown): "good" | "warn" | "bad" | "neutral" {
  const number = Number(value);
  if (!Number.isFinite(number)) return "neutral";
  if (number >= 0.58 || number >= 58) return "good";
  if (number >= 0.5 || number >= 50) return "warn";
  return "bad";
}

function StatusPill({ children, tone = "neutral" }: { children: ReactNode; tone?: "good" | "bad" | "warn" | "neutral" }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center whitespace-nowrap rounded border px-2 py-0.5 text-xs font-medium",
        tone === "good" && "border-success/30 bg-success/10 text-success",
        tone === "bad" && "border-danger/30 bg-danger/10 text-danger",
        tone === "warn" && "border-warning/30 bg-warning/10 text-warning",
        tone === "neutral" && "border-border bg-muted text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

function ComparisonChart({ comp }: { comp?: ThreeMonthComparison }) {
  const stock = (comp?.stock ?? []).filter((item) => Number.isFinite(item.close));
  const bench = (comp?.benchmark_series ?? []).filter((item) => Number.isFinite(item.close));
  if (!comp?.available || stock.length < 2 || bench.length < 2) {
    return (
      <div className="mt-2 flex h-40 items-center justify-center rounded border border-dashed text-sm text-muted-foreground">
        {comp?.reason || "暂无近三个月对比样本"}
      </div>
    );
  }
  const values = [...stock.map((p) => p.close), ...bench.map((p) => p.close), 0];
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(0.0001, max - min);
  const toY = (close: number) => 96 - ((close - min) / range) * 88;
  const toPath = (points: ComparisonPoint[]) =>
    points
      .map((item, index) => {
        const x = (index / Math.max(1, points.length - 1)) * 100;
        return `${index === 0 ? "M" : "L"} ${x.toFixed(2)} ${toY(item.close).toFixed(2)}`;
      })
      .join(" ");
  const zeroY = toY(0);
  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="mt-2 h-40 w-full">
      <line
        x1="0"
        x2="100"
        y1={zeroY.toFixed(2)}
        y2={zeroY.toFixed(2)}
        stroke="currentColor"
        strokeWidth="1"
        strokeDasharray="2 3"
        vectorEffect="non-scaling-stroke"
        className="text-muted-foreground/50"
      />
      <path d={toPath(bench)} fill="none" stroke="#f59e0b" strokeWidth="2" vectorEffect="non-scaling-stroke" />
      <path d={toPath(stock)} fill="none" stroke="#3b82f6" strokeWidth="2.6" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export function SingleStockOvernight() {
  const [symbol, setSymbol] = useState("");
  const [query, setQuery] = useState("");
  const [data, setData] = useState<SingleStockResponse | null>(null);
  const [suggestions, setSuggestions] = useState<SearchItem[]>([]);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");
  const [watchlisted, setWatchlisted] = useState(false);
  const [watchlistBusy, setWatchlistBusy] = useState(false);
  const searchTimer = useRef<number | null>(null);
  const suppressSearch = useRef(false);
  const searchBoxRef = useRef<HTMLDivElement | null>(null);
  const summaryTimer = useRef<number | null>(null);
  const loadSequence = useRef(0);
  const pageRevision = useRef("");
  const loadController = useRef<AbortController | null>(null);

  // Poll the lazy DeepSeek summary (computed in the background) until ready.
  const pollSummary = (safe: string, attempt = 0, sequence = loadSequence.current) => {
    if (summaryTimer.current) window.clearTimeout(summaryTimer.current);
    if (attempt > 15) return; // ~75s ceiling
    summaryTimer.current = window.setTimeout(async () => {
      if (sequence !== loadSequence.current) return;
      try {
        const resp = await fetch(`/single-stock-overnight/${encodeURIComponent(safe)}/summary`, { headers: authHeaders() });
        const s = await resp.json();
        if (sequence !== loadSequence.current) return;
        if (s && s.status && s.status !== "pending" && s.status !== "idle") {
          setData((prev) => (prev ? { ...prev, deepseek_summary: s } : prev));
          return;
        }
        pollSummary(safe, attempt + 1, sequence);
      } catch {
        if (sequence === loadSequence.current) pollSummary(safe, attempt + 1, sequence);
      }
    }, 5000);
  };

  // Poll the slow sections (profile / leader / option IV) warmed in background.
  const enrichTimer = useRef<number | null>(null);
  const pollEnrich = (safe: string, attempt = 0, sequence = loadSequence.current) => {
    if (enrichTimer.current) window.clearTimeout(enrichTimer.current);
    if (attempt > 24) return; // Slow optional sections do not hold up the page.
    enrichTimer.current = window.setTimeout(async () => {
      if (sequence !== loadSequence.current) return;
      try {
        const resp = await fetch(`/single-stock-overnight/${encodeURIComponent(safe)}/enrich?revision=${encodeURIComponent(pageRevision.current)}`, { headers: authHeaders() });
        if (!resp.ok) throw new Error("Enrichment unavailable");
        const e = await resp.json();
        if (sequence !== loadSequence.current) return;
        const core = e.core_payload as SingleStockResponse | undefined;
        if (core?.symbol === safe && core.page_revision) pageRevision.current = core.page_revision;
        setData((prev) =>
          prev?.symbol === safe
            ? {
                ...prev,
                ...(core?.symbol === safe ? core : {}),
                company_profile: e.company_profile ?? core?.company_profile ?? prev.company_profile,
                earnings: e.earnings ?? core?.earnings ?? prev.earnings,
                leader_compare: e.leader_compare ?? core?.leader_compare ?? prev.leader_compare,
                volatility: { ...(core?.volatility ?? prev.volatility ?? {}), iv: e.volatility?.iv ?? core?.volatility?.iv ?? prev.volatility?.iv },
              }
            : prev,
        );
        if (core?.deepseek_summary?.status === "pending") pollSummary(safe, 0, sequence);
        if (e.done) return;
        pollEnrich(safe, attempt + 1, sequence);
      } catch {
        if (sequence === loadSequence.current) pollEnrich(safe, attempt + 1, sequence);
      }
    }, 5000);
  };

  useEffect(() => () => {
    loadSequence.current += 1;
    loadController.current?.abort();
    didDeepLink.current = false;
    if (summaryTimer.current) window.clearTimeout(summaryTimer.current);
    if (enrichTimer.current) window.clearTimeout(enrichTimer.current);
  }, []);

  // Close the suggestion dropdown on any click outside the search box.
  useEffect(() => {
    const onDocMouseDown = (event: MouseEvent) => {
      if (searchBoxRef.current && !searchBoxRef.current.contains(event.target as Node)) {
        setSuggestOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, []);

  const refreshWatchlistState = async (safe: string) => {
    const sequence = loadSequence.current;
    try {
      const res = await api.getWatchlistItem(safe);
      if (sequence === loadSequence.current) setWatchlisted(Boolean(res.exists));
    } catch {
      if (sequence === loadSequence.current) setWatchlisted(false);
    }
  };

  const toggleWatchlist = async () => {
    const safe = (data?.symbol || symbol || query).trim().toUpperCase();
    if (!safe || watchlistBusy) return;
    setWatchlistBusy(true);
    try {
      if (watchlisted) {
        await api.deleteWatchlistItem(safe);
        setWatchlisted(false);
        toast.success(`${safe} 已移出自选池`);
      } else {
        const profile = (data?.company_profile ?? {}) as AnyRecord;
        await api.addWatchlistItem({ symbol: safe, name: String(profile.name || safe), enabled: true });
        setWatchlisted(true);
        toast.success(`${safe} 已加入自选池`);
      }
    } catch (exc) {
      toast.error(exc instanceof Error ? exc.message : "自选池更新失败");
    } finally {
      setWatchlistBusy(false);
    }
  };

  const load = async (nextSymbol = symbol) => {
    const safe = nextSymbol.trim().toUpperCase();
    if (!safe) return;
    const sequence = ++loadSequence.current;
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    // Selecting a symbol updates `query`; suppress the resulting search so the
    // suggestion dropdown stays closed instead of immediately re-opening.
    suppressSearch.current = true;
    setLoading(true);
    setError("");
    setSuggestOpen(false);
    setSuggestions([]);
    if (summaryTimer.current) window.clearTimeout(summaryTimer.current);
    if (enrichTimer.current) window.clearTimeout(enrichTimer.current);
    try {
      const response = await fetch(`/single-stock-overnight/${encodeURIComponent(safe)}?universe=auto&period=2y`, {
        headers: authHeaders(),
        signal: controller.signal,
      });
      const payload = await response.json();
      if (sequence !== loadSequence.current) return;
      if (!response.ok) throw new Error(payload.detail || "加载失败");
      pageRevision.current = payload.page_revision || "";
      setData(payload);
      setSymbol(safe);
      setQuery(safe);
      void refreshWatchlistState(safe);
      // The AI summary is computed lazily; poll for it if still pending.
      if (payload?.deepseek_summary?.status === "pending") pollSummary(safe);
      // Slow sections (profile / leader / IV) warm in background; poll to fill in.
      pollEnrich(safe, 0, sequence);
    } catch (exc) {
      if (sequence === loadSequence.current && !controller.signal.aborted)
        setError(exc instanceof Error ? exc.message : "加载失败");
    } finally {
      if (sequence === loadSequence.current) setLoading(false);
    }
  };

  // Deep-link support: /single-stock-overnight?symbol=AAPL auto-loads on mount
  // (this is the canonical detail page the priority board links to).
  const [searchParams] = useSearchParams();
  const didDeepLink = useRef(false);
  useEffect(() => {
    if (didDeepLink.current) return;
    const linked = searchParams.get("symbol");
    if (linked && linked.trim()) {
      didDeepLink.current = true;
      void load(linked.trim().toUpperCase());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const searchSymbols = async (text: string) => {
    const safe = text.trim();
    if (!safe) {
      setSuggestions([]);
      return;
    }
    // Already on this exact symbol (e.g. arrived via deep-link) — don't pop the
    // autocomplete for the symbol that's already loaded.
    if (safe.toUpperCase() === symbol.toUpperCase()) {
      setSuggestions([]);
      setSuggestOpen(false);
      return;
    }
    setSearching(true);
    try {
      const response = await fetch(`/single-stock-overnight/search?q=${encodeURIComponent(safe)}&limit=8`, {
        headers: authHeaders(),
      });
      const payload = await response.json();
      setSuggestions(Array.isArray(payload.items) ? payload.items : []);
      setSuggestOpen(true);
    } catch {
      setSuggestions([]);
    } finally {
      setSearching(false);
    }
  };

  useEffect(() => {
    if (suppressSearch.current) {
      suppressSearch.current = false;
      return;
    }
    if (searchTimer.current) window.clearTimeout(searchTimer.current);
    searchTimer.current = window.setTimeout(() => searchSymbols(query), 250);
    return () => {
      if (searchTimer.current) window.clearTimeout(searchTimer.current);
    };
  }, [query]);

  const metrics = useMemo(() => data?.correlation?.metrics ?? [], [data]);
  const stats = data?.overnight_alpha?.stats ?? {};
  const row = data?.row ?? {};
  const plan = data?.action_plan ?? {};
  // Price lines for the chart: action-plan levels + current price.
  const chartPriceLines = useMemo(() => {
    const lines: { price: number; label: string; color?: string; dashed?: boolean }[] = [];
    const cur = Number(data?.day_change?.price ?? row.current_price);
    if (Number.isFinite(cur)) lines.push({ price: cur, label: "现价", color: "#3b82f6" });
    if (plan?.available) {
      const g = "#10b981", r = "#ef4444";
      const add = (v: unknown, label: string, color: string, dashed?: boolean) => {
        const n = Number(v);
        if (Number.isFinite(n) && n > 0) lines.push({ price: n, label, color, dashed });
      };
      add(plan.buy_pullback_price, "回踩买", g, true);
      add(plan.buy_strength_price, "强势买", g, true);
      add(plan.take_profit, "止盈", g);
      add(plan.stop_loss, "止损", r);
      add(plan.stop_buying_below, "停止买入", r, true);
    }
    // Fib retracement levels (38.2/50/61.8%) — gray, support/resistance only.
    // Backtested as NOT a buy signal, so deliberately neutral-colored.
    const fib = data?.fib_levels;
    if (fib?.available && fib.levels) {
      for (const lv of fib.levels) {
        if (lv.ratio === 0.382 || lv.ratio === 0.5 || lv.ratio === 0.618) {
          lines.push({ price: lv.price, label: `fib ${lv.label}`, color: "#9ca3af", dashed: true });
        }
      }
    }
    return lines;
  }, [data, plan, row]);
  const comparison = data?.three_month_comparison;
  const deepseek = data?.deepseek_summary ?? {};
  const alphaWin = stats.alpha_win_rate;
  const strongWin = stats.strong_alpha_win_rate;
  const meanAlpha = stats.mean_beta_adjusted_alpha;
  const sr = data?.signal_read;
  const action = row.open_risk_status === "BLOCK_OPEN" ? "风控阻断，仅人工复核" : row.short_reason || "等待盘末复核";

  return (
    <div className="min-h-full bg-background p-4 md:p-6">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">Stock research</div>
            <h1 className="mt-1 text-2xl font-bold tracking-tight">个股研判</h1>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
              先看公司基本面（是干什么的、所处行业、上下游、竞争对手、赛道地位），再看买入信号、关键价位与复核意见。
            </p>
          </div>
          <form
            className="relative flex w-full gap-2 lg:w-[440px]"
            onSubmit={(event) => {
              event.preventDefault();
              load(query);
            }}
          >
            <div className="relative flex-1" ref={searchBoxRef}>
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value.toUpperCase())}
                onFocus={() => suggestions.length && setSuggestOpen(true)}
                className="h-10 w-full rounded border bg-card pl-9 pr-8 text-sm outline-none focus:border-primary"
                placeholder="搜索代码，例如 HOOD"
              />
              {searching && <Loader2 className="absolute right-2.5 top-3 h-4 w-4 animate-spin text-muted-foreground" />}
              {suggestOpen && !loading && suggestions.length > 0 && query !== symbol && (
                <div className="absolute left-0 right-0 top-11 z-20 max-h-72 overflow-auto rounded border bg-card shadow-xl">
                  {suggestions.map((item) => {
                    const showName = item.name && item.name.toUpperCase() !== item.symbol.toUpperCase();
                    const exchange = item.exchange_display || item.exchange;
                    return (
                      <button
                        key={`${item.symbol}-${item.exchange}`}
                        type="button"
                        onMouseDown={(event) => event.preventDefault()}
                        onClick={() => load(item.symbol)}
                        className="flex w-full items-center justify-between gap-3 border-b px-3 py-2 text-left text-sm last:border-b-0 hover:bg-muted"
                      >
                        <span className="min-w-0 truncate">
                          <span className="font-semibold">{item.symbol}</span>
                          {showName && <span className="ml-2 text-muted-foreground">{item.name}</span>}
                        </span>
                        {exchange && (
                          <span className="shrink-0 whitespace-nowrap rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                            {exchange}
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
            <button type="submit" disabled={loading} className="inline-flex h-10 items-center gap-2 rounded bg-primary px-4 text-sm font-semibold text-primary-foreground disabled:opacity-60">
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              研判
            </button>
          </form>
        </div>

        {error && <div className="rounded border border-danger/30 bg-danger/10 p-3 text-sm text-danger">{error}</div>}

        {!data && !loading && (
          <div className="flex flex-col items-center justify-center gap-3 rounded border border-dashed border-muted-foreground/30 py-20 text-center">
            <Search className="h-10 w-10 text-muted-foreground/40" />
            <p className="text-base font-medium text-muted-foreground">输入美股代码，点击"研判"开始分析</p>
            <p className="max-w-md text-sm text-muted-foreground/70">
              支持美股代码（如 AAPL、TSLA、HOOD），搜索框输入后从下拉列表选择或直接按回车。
            </p>
          </div>
        )}

        {data && (
        <>
          <section className="rounded border bg-card p-4">
            <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-2">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h2 className="text-3xl font-bold tracking-tight">{data?.symbol ?? symbol}</h2>
                <button
                  type="button"
                  onClick={toggleWatchlist}
                  disabled={watchlistBusy}
                  className={cn(
                    "inline-flex h-9 w-9 items-center justify-center rounded border transition",
                    watchlisted
                      ? "border-amber-300 bg-amber-50 text-amber-500 dark:bg-amber-950/30"
                      : "border-border bg-background text-muted-foreground hover:text-amber-500",
                    watchlistBusy && "cursor-wait opacity-70",
                  )}
                  title={watchlisted ? "从自选池移除" : "加入自选池"}
                >
                  {watchlistBusy ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Star className={cn("h-5 w-5", watchlisted && "fill-amber-400")} />
                  )}
                </button>
                <span className="text-3xl font-bold tabular-nums">{fmtMoney(data?.day_change?.price ?? row.current_price)}</span>
                {data?.day_change && (
                  <span className={cn(
                    "text-xl font-semibold tabular-nums",
                    data.day_change.change_pct > 0 ? "text-success" : data.day_change.change_pct < 0 ? "text-danger" : "text-muted-foreground",
                  )}>
                    {fmtSignedPct(data.day_change.change_pct)} ({data.day_change.change >= 0 ? "+" : ""}{data.day_change.change.toFixed(2)})
                  </span>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <StatusPill tone={row.open_risk_status === "BLOCK_OPEN" ? "bad" : "warn"}>{row.open_risk_status || "研究复核"}</StatusPill>
                {data?.day_change?.as_of && <span className="text-muted-foreground">截至 {data.day_change.as_of} 收盘（缓存）</span>}
                {row.context_status === "loading" && <span className="text-muted-foreground">三层证据更新中</span>}
              </div>
            </div>
          </section>

          <PriceChart symbol={data?.symbol ?? symbol} currentIv={data?.volatility?.iv?.atm_iv} priceLines={chartPriceLines} dataDate={data?.day_change?.as_of ?? undefined} />

          <DistributionRiskCard risk={data?.distribution_risk} />

          <OptionVolatilityCard iv={data?.volatility?.iv} hv20={data?.volatility?.hv20} />

          <NewsCard news={data?.news} />

          <EarningsExpectationsCard earnings={(data?.earnings ?? data?.company_profile?.earnings) as EarningsInfo | null | undefined} />

          <CompanyProfileCard profile={data?.company_profile} symbol={data?.symbol ?? symbol} />

          <LeaderCompareCard compare={data?.leader_compare} />

          <SignalReadCard sr={sr} symbol={data?.symbol ?? symbol} price={row.current_price} />

          <section className="grid gap-3 lg:grid-cols-[1.1fr_0.9fr]">
            <div className="rounded border bg-card p-4">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="font-semibold">隔夜 Alpha 参考</h2>
              <StatusPill tone={scoreTone(alphaWin)}>隔夜Alpha {fmtPct(alphaWin)}<span className="opacity-60"> ·参考</span></StatusPill>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">{action}</p>
            <div className="mt-3 text-xs font-medium text-muted-foreground">尾盘买入·次日开盘卖出，另一套口径，非本页主信号</div>
            <div className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              <Metric label="隔夜胜率" value={fmtPct(alphaWin)} detail="次日开盘跑赢自身Beta的比例" />
              <Metric label="强隔夜胜率" value={fmtPct(strongWin)} detail="隔夜超额>0.1%" />
              <Metric label="平均隔夜超额" value={fmtPct(meanAlpha, 2)} detail="收盘到次日开盘" />
              <Metric label="样本数 / Beta" value={`${stats.sample_count ?? "--"} / ${fmtNumber(data?.overnight_alpha?.beta, 2)}`} detail={`基准 ${data?.overnight_alpha?.benchmark || data?.correlation?.benchmark || "--"}`} />
            </div>
            <div className="mt-4 border-t pt-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">近 3 个月走势对比 · 标的 vs 指数</h3>
                <div className="flex items-center gap-3 text-xs">
                  <span className="inline-flex items-center gap-1">
                    <span className="inline-block h-2.5 w-3 rounded-sm" style={{ backgroundColor: "#3b82f6" }} />
                    {comparison?.symbol || data?.symbol || symbol}
                    <span className={cn("font-semibold", pctTone(comparison?.stock_return) === "good" ? "text-success" : pctTone(comparison?.stock_return) === "bad" ? "text-danger" : "text-muted-foreground")}>
                      {fmtSignedPct(comparison?.stock_return)}
                    </span>
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <span className="inline-block h-2.5 w-3 rounded-sm" style={{ backgroundColor: "#f59e0b" }} />
                    {comparison?.benchmark || "指数"}
                    <span className={cn("font-semibold", pctTone(comparison?.benchmark_return) === "good" ? "text-success" : pctTone(comparison?.benchmark_return) === "bad" ? "text-danger" : "text-muted-foreground")}>
                      {fmtSignedPct(comparison?.benchmark_return)}
                    </span>
                  </span>
                </div>
              </div>
              <ComparisonChart comp={comparison} />
              <p className="mt-1 text-xs text-muted-foreground">
                两条线均以区间首日归一化为 0%；蓝色为标的、橙色为指数。相对指数{" "}
                <span className={cn("font-semibold", pctTone(comparison?.relative_return) === "good" ? "text-success" : pctTone(comparison?.relative_return) === "bad" ? "text-danger" : "text-muted-foreground")}>
                  {fmtSignedPct(comparison?.relative_return)}
                </span>
                。
              </p>
            </div>
          </div>

          <div className="rounded border border-primary/40 bg-primary/5 p-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 className="font-semibold">开盘后行动区间</h2>
                <p className="text-xs text-muted-foreground">用于盘中复核，不是自动下单指令</p>
              </div>
              <Target className="h-5 w-5 text-primary" />
            </div>
            {plan.available ? (
              <>
                <div className="mt-3 grid gap-2 sm:grid-cols-2">
                  <ActionBox tone="good" label="强势买入复核" value={`涨到 ${fmtMoney(plan.buy_strength_price)}`} pct={plan.buy_strength_pct} detail="站上该价位且量价配合，再考虑盘末动作" />
                  <ActionBox tone="good" label="回踩买入复核" value={`跌到 ${fmtMoney(plan.buy_pullback_price)}`} pct={plan.buy_pullback_pct} detail="靠近支撑后企稳，不破再复核" />
                  <ActionBox tone="warn" label="停止追高" value={`涨超 ${fmtMoney(plan.stop_chasing_above)}`} pct={plan.stop_chasing_pct} detail="超过该价位，追高性价比下降" />
                  <ActionBox tone="bad" label="停止买入" value={`跌破 ${fmtMoney(plan.stop_buying_below)}`} pct={plan.stop_buying_below_pct} detail="结构走弱，取消当日买入动作" />
                  <ActionBox tone="bad" label="执行后止损" value={fmtMoney(plan.stop_loss)} pct={plan.stop_loss_pct} detail={`单股风险约 ${fmtMoney(plan.risk_per_share)}`} />
                  <ActionBox tone="good" label="执行后止盈" value={fmtMoney(plan.take_profit)} pct={plan.take_profit_pct} detail={`风报比约 ${fmtNumber(plan.reward_risk, 2)}`} />
                </div>
                <p className="mt-2 text-xs text-muted-foreground">涨跌幅均相对{plan.reference_label || "前一交易日收盘价"}（{fmtMoney(plan.as_of_price)}）。</p>
                <p className="mt-2 text-sm text-muted-foreground">{plan.plain_cn}</p>
              </>
            ) : (
              <div className="mt-3 rounded border border-warning/30 bg-warning/10 p-3 text-sm text-warning">{plan.reason || "暂无可用行动区间"}</div>
            )}
          </div>
        </section>

        <section className="rounded border bg-card p-4">
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <Bot className="h-5 w-5 shrink-0 text-primary" />
              <h2 className="font-semibold">AI 复核意见</h2>
              <StatusPill tone={deepseek.available ? "good" : "warn"}>{deepseek.verdict || "等待复核"}</StatusPill>
            </div>
            <div className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
              <span className="font-mono">{deepseek.model || "--"}</span>
              <span className="rounded bg-muted px-1.5 py-0.5">{deepseek.cache_hit ? "缓存" : "最新"}</span>
            </div>
          </div>
          <div className="mt-3 rounded-md border-l-4 border-primary bg-primary/5 p-3">
            <p className="text-base font-medium leading-7 text-foreground">
              <HighlightedText text={deepseek.summary || "DeepSeek 总结生成中或暂不可用，请先按行动区间人工复核。"} />
            </p>
          </div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <ListBlock title="支持因素" items={deepseek.bull_points} empty="暂无明确支持项" tone="good" />
            <ListBlock title="反对 / 风险" items={deepseek.bear_points || deepseek.risk_flags} empty="暂无额外风险项" tone="bad" />
          </div>
          <div className="mt-3 rounded border bg-muted/20 p-3">
            <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-warning">
              <Target className="h-4 w-4" />
              关注价位
            </div>
            <WatchLevels plan={plan} fallback={deepseek.watch_levels} />
          </div>
        </section>

        <MetricBenchmarks rows={data?.metric_benchmarks} />

        <FibLevels fib={data?.fib_levels} />

        <section className="rounded border bg-card p-4">
          <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
            <div>
              <h2 className="font-semibold">与隔夜 Alpha 的相关性证据</h2>
              <p className="text-sm text-muted-foreground">
                按与“Beta 调整后收盘到次日开盘 Alpha”的相关性绝对值排序。相关性不是因果，只用于解释当前因子更像历史上哪类隔夜环境。
              </p>
            </div>
            <div className="text-xs text-muted-foreground">
              基准 {data?.correlation?.benchmark || "--"} · 样本 {data?.correlation?.sample_count ?? "--"} · 数据源 {data?.correlation?.data_sources?.stock || "--"}
            </div>
          </div>
          {!data?.correlation?.available ? (
            <div className="mt-4 flex items-center gap-2 rounded border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
              <AlertTriangle className="h-4 w-4" />
              价格历史不足或数据源暂不可用：{data?.correlation?.reason || "--"}
            </div>
          ) : (
            <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
              {metrics.map((metric) => (
                <div key={metric.key} className="rounded border bg-muted/20 p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="font-semibold">{metric.label}</div>
                      <div className="mt-1 text-xs leading-5 text-muted-foreground">{metric.plain_meaning}</div>
                    </div>
                    <StatusPill tone={(metric.alpha_correlation ?? 0) >= 0 ? "good" : "warn"}>{metric.direction_cn}</StatusPill>
                  </div>
                  <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
                    <Mini label="当前值" value={metricValue(metric)} />
                    <Mini label="历史分位" value={fmtPct(metric.percentile)} />
                    <Mini label="Alpha相关" value={fmtNumber(metric.alpha_correlation, 3)} />
                    <Mini label="次开相关" value={fmtNumber(metric.next_open_correlation, 3)} />
                  </div>
                  <div className="mt-2 text-xs text-muted-foreground">有效样本 {metric.sample_count}</div>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="rounded border bg-card p-4">
          <div className="flex items-start gap-2 text-sm text-muted-foreground">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
            <p>
              {data?.note || "本页仅用于研究复核，不构成确定性买入建议。"} IV 来自 yfinance 期权链字段；若标的无上市期权会明确显示“无上市期权”，若期权链存在但字段缺失或接近 0，会显示“暂不采用”。
            </p>
          </div>
        </section>
        </>
        )}

      </div>
    </div>
  );
}

const SR_BADGE: Record<string, { label: string; cls: string }> = {
  validated: { label: "已验证", cls: "bg-success/15 text-success border-success/30" },
  low_edge: { label: "低把握·未验证", cls: "bg-warning/15 text-warning border-warning/30" },
  experimental: { label: "实验·未校准", cls: "bg-muted text-muted-foreground border-muted-foreground/30" },
};
const SR_BASIS: Record<string, string> = {
  pullback_hv: "回调 + 波动扩张(10日)",
  launch_contrarian: "回调买入(5日)",
  launch: "启动分(研究)",
};

const BENCH_TONE: Record<string, string> = {
  green: "text-emerald-600 dark:text-emerald-400",
  red: "text-rose-600 dark:text-rose-400",
  amber: "text-amber-600 dark:text-amber-400",
  sky: "text-sky-600 dark:text-sky-400",
  muted: "text-foreground",
};
const BENCH_VERDICT_CN: Record<string, string> = {
  good: "达标/偏好", bad: "偏低·留意", high: "偏高", low: "偏低", neutral: "中性",
};
function fmtBench(v: number | null | undefined, fmt: string): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  if (fmt === "pct1") return `${(v * 100).toFixed(1)}%`;
  if (fmt === "pct1s") return `${v > 0 ? "+" : ""}${(v * 100).toFixed(1)}%`;
  if (fmt === "x") return `${v.toFixed(2)}×`;
  return String(v);
}
function MetricBenchmarks({ rows }: { rows?: SingleStockResponse["metric_benchmarks"] }) {
  if (!rows || rows.length === 0) return null;
  return (
    <section className="rounded border bg-card p-4">
      <h2 className="font-semibold">指标对照（高 / 低 一眼看懂）</h2>
      <p className="mt-0.5 text-xs text-muted-foreground">
        每个指标给中性线 / 参考带与同赛道龙头值；颜色标注 偏高(琥珀) / 达标(绿) / 偏低·留意(红) / 中性。参考带是经验阈值，非保证。
      </p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-muted-foreground">
              <th className="py-1.5 pr-3">指标</th>
              <th className="py-1.5 pr-3 text-right">本股</th>
              <th className="py-1.5 pr-3 text-right">赛道龙头</th>
              <th className="py-1.5">参考范围</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className="border-b align-top last:border-0">
                <td className="py-1.5 pr-3">
                  <div className="font-medium">{r.label}</div>
                  {r.hint && <div className="text-[11px] leading-snug text-muted-foreground">{r.hint}</div>}
                </td>
                <td className={`whitespace-nowrap py-1.5 pr-3 text-right font-mono font-semibold ${BENCH_TONE[r.tone] || BENCH_TONE.muted}`}>
                  {fmtBench(r.value, r.fmt)}
                  <div className="text-[10px] font-normal text-muted-foreground">{BENCH_VERDICT_CN[r.verdict] || r.verdict}</div>
                </td>
                <td className="whitespace-nowrap py-1.5 pr-3 text-right font-mono text-muted-foreground">
                  {r.leader_value != null ? fmtBench(r.leader_value, r.fmt) : "--"}
                  {r.leader_symbol && r.leader_value != null && <div className="text-[10px] font-normal">{r.leader_symbol}</div>}
                </td>
                <td className="py-1.5 text-xs text-muted-foreground">{r.ref}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function sentimentColor(s?: string): string {
  if (s === "positive") return "text-emerald-600 dark:text-emerald-400";
  if (s === "negative") return "text-rose-600 dark:text-rose-400";
  return "text-muted-foreground";
}
function sentimentCn(s?: string): string {
  return s === "positive" ? "正面" : s === "negative" ? "负面" : "中性";
}

function NewsCard({ news }: { news?: NewsDigest }) {
  if (!news || !news.available) {
    return (
      <section className="rounded border bg-card p-4">
        <h2 className="font-semibold">舆情 · 近期新闻情绪</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          {news?.reason === "not_cached" ? "新闻正在后台更新。"
            : news?.reason && news.reason !== "no_recent_news" ? "新闻源暂不可用，暂不能判断近期新闻情绪。"
              : "近 10 日暂无可解析的新闻情绪。"}
        </p>
      </section>
    );
  }
  const trump = news.trump;
  const impact = news.impact;
  return (
    <section className="rounded border bg-card p-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="font-semibold">舆情 · 近期新闻情绪</h2>
        <span className={cn("rounded px-1.5 py-0.5 text-[11px]", (news.neg ?? 0) > (news.pos ?? 0) ? "bg-amber-500/15 text-amber-700 dark:text-amber-300" : "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300")}>
          {news.label}（近10日 {news.n} 条：<span className="text-emerald-600 dark:text-emerald-400">正{news.pos}</span>/<span className="text-rose-600 dark:text-rose-400">负{news.neg}</span>/中{news.neu}）
        </span>
      </div>

      {/* D.Trump 提及 —— 醒目置顶 */}
      {trump?.mentioned && (
        <div className="mt-2 rounded-md border-2 border-orange-500/60 bg-orange-500/10 p-2.5">
          <div className="flex flex-wrap items-center gap-2 text-sm font-medium text-orange-700 dark:text-orange-300">
            <span className="rounded bg-orange-500 px-1.5 py-0.5 text-[11px] text-white">TRUMP 提及</span>
            近 10 日有 {trump.count} 条提及 · 情绪{trump.net_score > 0.1 ? "偏正" : trump.net_score < -0.1 ? "偏负" : "中性"}（净 {trump.net_score >= 0 ? "+" : ""}{(trump.net_score).toFixed(2)}）
          </div>
          <ul className="mt-1.5 space-y-1">
            {trump.items?.map((it, i) => (
              <li key={i} className="text-xs">
                <a href={it.url || "#"} target="_blank" rel="noreferrer" className="text-primary hover:underline">{it.title}</a>
                <span className={cn("ml-1", sentimentColor(it.sentiment))}>· {sentimentCn(it.sentiment)}</span>
                {it.published_utc && <span className="ml-1 text-muted-foreground">· {it.published_utc.slice(0, 10)}</span>}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-[10px] text-orange-700/80 dark:text-orange-300/80">⚠️ 政治人物提及常引发短期、双向的剧烈波动；本系统仅展示，未验证为可交易信号。</p>
        </div>
      )}

      {/* 预计股价影响（启发式） */}
      {impact?.available && (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          <span className="font-medium">预计短期影响：</span>
          <span className={cn("font-mono font-semibold", (impact.point_pct ?? 0) >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400")}>
            {(impact.point_pct ?? 0) >= 0 ? "+" : ""}{((impact.point_pct ?? 0) * 100).toFixed(2)}%（{impact.direction}）
          </span>
          <span className="text-muted-foreground">波动带 ±{((impact.band_pct ?? 0) * 100).toFixed(1)}%</span>
          <span className="text-muted-foreground/80">{impact.basis}</span>
        </div>
      )}
      {impact && !impact.available && <p className="mt-2 text-xs text-muted-foreground/80">{impact.note}</p>}
      {impact?.available && <p className="mt-0.5 text-[10px] text-muted-foreground/70">{impact.note}</p>}

      {/* 近期新闻列表 */}
      <div className="mt-3 space-y-1.5">
        {news.items?.slice(0, 8).map((it, i) => (
          <div key={i} className="flex flex-wrap items-baseline gap-x-2 text-xs">
            <span className={cn("font-medium", sentimentColor(it.sentiment))}>{sentimentCn(it.sentiment)}</span>
            <a href={it.url || "#"} target="_blank" rel="noreferrer" className="text-foreground hover:text-primary hover:underline">{it.title}</a>
            {it.publisher && <span className="text-muted-foreground">· {it.publisher}</span>}
            {it.published_utc && <span className="text-muted-foreground/70">· {it.published_utc.slice(0, 10)}</span>}
          </div>
        ))}
      </div>
      <p className="mt-2 text-[10px] text-muted-foreground/70">情绪来自 Polygon 新闻 AI 标注（启发式，非事实判断）。</p>
    </section>
  );
}

// 每个回撤档位代表什么（多空含义），按比例数值匹配。
const FIB_MEANING: Record<string, string> = {
  "23.6": "浅回撤 · 强势趋势中的小回踩",
  "38.2": "第一道支撑 · 健康回调常见起点",
  "50.0": "中位线 · 多空均衡(习惯用，非严格斐波那契)",
  "61.8": "黄金分割 · 关键支撑/最后防线，跌破多转弱",
  "78.6": "深回撤 · 接近回吐全部涨幅，反转风险升高",
  "0.0": "摆动端点 · 本轮起点",
  "100.0": "摆动端点 · 本轮终点",
};
function fibMeaning(label: string): string {
  const m = label.match(/[\d.]+/);
  if (!m) return "";
  return FIB_MEANING[parseFloat(m[0]).toFixed(1)] || "";
}

function FibLevels({ fib }: { fib?: SingleStockResponse["fib_levels"] }) {
  if (!fib || !fib.available) return null;
  const cp = fib.current_price ?? 0;
  return (
    <section className="rounded border bg-card p-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="font-semibold">斐波那契回撤（画线参考）</h2>
        <span className="text-xs text-muted-foreground">{fib.trend_cn} · 近 {fib.window_days} 日 高 ${fib.swing_high} / 低 ${fib.swing_low}</span>
        {fib.in_shallow_zone && <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[11px] text-amber-700 dark:text-amber-300">当前价在 38.2–61.8% 浅回撤区</span>}
      </div>
      <p className="mt-0.5 text-xs text-amber-700 dark:text-amber-300">
        ⚠️ <b>回测结论</b>：本系统的超卖回调 edge 在 38.2–61.8% 浅回撤区<b>反而显著为负</b>（真正 edge 在更深回撤）。所以这些档位只作支撑/阻力画线，<b>fib 浅回撤区不是买点</b>。
      </p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full max-w-2xl text-sm">
          <tbody>
            {fib.levels?.map((lv) => {
              const inShallow = !!fib.shallow_zone && lv.price >= fib.shallow_zone.low && lv.price <= fib.shallow_zone.high;
              return (
                <tr key={lv.ratio} className={`border-b last:border-0 ${inShallow ? "bg-amber-500/5" : ""}`}>
                  <td className="py-1.5 pr-3 font-mono text-muted-foreground align-top">{lv.label}</td>
                  <td className="py-1.5 pr-3 text-right font-mono font-medium align-top">${lv.price.toFixed(2)}</td>
                  <td className="py-1.5 text-xs text-muted-foreground">
                    {fibMeaning(lv.label)}
                    {inShallow && <span className="text-amber-600 dark:text-amber-400"> · 浅回撤区(实证偏弱)</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
        含义速记：回撤越浅(23.6/38.2%)说明趋势越强、只是小歇脚；<b>61.8% 是黄金分割、关键防线</b>，守住多看趋势延续、跌破多转弱；78.6% 已接近把涨幅吐光、反转风险升高；50% 不是严格斐波那契，只是习惯用的多空均衡中位线。
      </p>
      <div className="mt-2 text-xs text-muted-foreground">
        当前价 <span className="font-mono font-medium text-foreground">${cp.toFixed(2)}</span>
        {fib.zone && <> · 位于 {fib.zone.lower.label}–{fib.zone.upper.label} 回撤区间</>}
      </div>
    </section>
  );
}

function PriceChart({ symbol, currentIv, priceLines, dataDate }: { symbol: string; currentIv?: number; priceLines?: { price: number; label: string; color?: string; dashed?: boolean }[]; dataDate?: string }) {
  const [tf, setTf] = useState<"daily" | "1h" | "15m" | "5m">("daily");
  const TF_LABEL: Record<string, string> = { daily: "日线", "1h": "1小时", "15m": "15分", "5m": "5分" };
  const [bars, setBars] = useState<PriceBar[]>([]);
  const [markers, setMarkers] = useState<TradeMarker[]>([]);
  const [loading, setLoading] = useState(false);
  const [note, setNote] = useState("");
  useEffect(() => {
    if (!symbol) return;
    let cancelled = false;
    const controller = new AbortController();
    setLoading(true);
    fetch(`/single-stock-overnight/${encodeURIComponent(symbol)}/candles?timeframe=${tf}`, { headers: authHeaders(), signal: controller.signal })
      .then((r) => r.json())
      .then((resp) => {
        if (cancelled) return;
        const conv: PriceBar[] = (resp?.candles || []).map((c: { t: number; o: number; h: number; l: number; c: number; v?: number }) => ({
          time: tf === "daily" ? new Date(c.t).toISOString().slice(0, 10) : new Date(c.t).toISOString().slice(5, 16).replace("T", " "),
          open: c.o, high: c.h, low: c.l, close: c.c, volume: c.v ?? 0,
        }));
        setBars(conv);
        setMarkers((resp?.markers || []) as TradeMarker[]);
        setNote(resp?.note || "");
      })
      .catch(() => { if (!cancelled) { setBars([]); setMarkers([]); setNote("行情加载失败"); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [symbol, tf, dataDate]);
  return (
    <section className="rounded border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h2 className="font-semibold">行情图表</h2>
          <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[11px] text-amber-700 dark:text-amber-300">{tf === "daily" ? "日线 · 非实时" : "分时 · 非实时"}</span>
        </div>
        <div className="inline-flex rounded-md border p-1 text-xs">
          {(["daily", "1h", "15m", "5m"] as const).map((t) => (
            <button key={t} type="button" onClick={() => setTf(t)}
              className={cn("rounded px-2.5 py-1", tf === t ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted")}>
              {TF_LABEL[t]}
            </button>
          ))}
        </div>
      </div>
      {loading && bars.length === 0 ? (
        <div className="flex h-64 items-center justify-center text-sm text-muted-foreground"><Loader2 className="h-5 w-5 animate-spin" /></div>
      ) : bars.length < 2 ? (
        <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">暂无{TF_LABEL[tf]}行情数据</div>
      ) : (
        <div className="mt-2"><CandlestickChart data={bars} markers={markers} height={720} initialOverlays={["ema5", "ema10", "ema15", "ema20"]} currentIv={currentIv} priceLines={priceLines} initialRange={tf === "daily" ? "3M" : "ALL"} /></div>
      )}
      {note && <p className="mt-1 text-[11px] text-muted-foreground">{note}</p>}
    </section>
  );
}

function fmtMarketCap(value?: number | null): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "--";
  if (value >= 1e12) return `$${(value / 1e12).toFixed(2)}万亿`;
  if (value >= 1e8) return `$${(value / 1e8).toFixed(0)}亿`;
  return `$${(value / 1e6).toFixed(0)}百万`;
}

function isRecentResearchDate(asOf?: string | null, marketSession?: string | null): boolean {
  if (!asOf || !marketSession) return false;
  const observed = Date.parse(`${asOf.slice(0, 10)}T00:00:00Z`);
  const session = Date.parse(`${marketSession.slice(0, 10)}T00:00:00Z`);
  return Number.isFinite(observed) && Number.isFinite(session)
    && observed <= session && session - observed <= 7 * 24 * 60 * 60 * 1000;
}

function TagList({ items }: { items?: string[] }) {
  if (!items || items.length === 0) return <span className="text-sm text-muted-foreground">暂无</span>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((it, i) => (
        <span key={`${it}-${i}`} className="rounded bg-muted px-2 py-0.5 text-xs text-foreground">
          {it}
        </span>
      ))}
    </div>
  );
}

function FactChip({ label, value, sub, meta, danger, good, title }: { label: string; value: string; sub?: string; meta?: string; danger?: boolean; good?: boolean; title?: string }) {
  return (
    <div className={cn(
      "rounded border px-2.5 py-1.5",
      danger ? "border-danger/40 bg-danger/5" : good ? "border-emerald-500/40 bg-emerald-500/5" : "bg-muted/30",
    )} title={title}>
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className={cn("text-sm font-semibold tabular-nums", danger && "text-danger", good && "text-emerald-600 dark:text-emerald-400")}>
        {value}
        {sub && <span className="ml-1 text-[11px] font-normal text-muted-foreground">{sub}</span>}
      </div>
      {meta && <div className="mt-0.5 text-[11px] text-muted-foreground">{meta}</div>}
    </div>
  );
}

function ProfileRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[5.5rem_1fr] gap-2 py-1.5">
      <div className="pt-0.5 text-xs font-medium text-muted-foreground">{label}</div>
      <div className="min-w-0 text-sm">{children}</div>
    </div>
  );
}

function CompanyProfileCard({ profile, symbol }: { profile?: CompanyProfile; symbol: string }) {
  const f = profile?.facts;
  const gil = profile?.gildata_research;
  const analyst = profile?.analyst;
  const gilRecent = isRecentResearchDate(gil?.as_of, profile?.market_session);
  const fmpRecent = isRecentResearchDate(analyst?.as_of, profile?.market_session);
  const useGilRating = Boolean(gil?.ratings && (!analyst?.as_of || (gil?.as_of ?? "") >= analyst.as_of));
  const useGilTarget = typeof gil?.target_avg_usd === "number" && (gilRecent || typeof analyst?.target_avg_quarter !== "number");
  const gilRating = gil?.ratings;
  const capMeta = profile?.market_cap_meta;
  const capSource = capMeta?.source?.startsWith("gildata") ? "聚源" : capMeta?.source?.startsWith("massive") ? "Massive" : "缓存";
  const hasFacts = f && (f.name || f.sector || f.industry || f.business_summary_en);
  if (!profile?.available && !hasFacts) {
    return (
      <section className="rounded border border-dashed border-muted-foreground/30 bg-card p-4 text-sm text-muted-foreground">
        {symbol} 公司基本面暂不可用。
      </section>
    );
  }
  return (
    <section className="rounded border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b pb-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-bold">{f?.name || symbol}</h2>
            <span className="font-mono text-sm text-muted-foreground">{symbol}</span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs">
            {f?.sector && <span className="rounded bg-primary/10 px-2 py-0.5 font-medium text-primary">{f.sector}</span>}
            {f?.industry && <span className="rounded bg-muted px-2 py-0.5 text-muted-foreground">{f.industry}</span>}
            {profile?.segment_cn && <span className="rounded bg-muted px-2 py-0.5 text-muted-foreground">{profile.segment_cn}</span>}
          </div>
        </div>
        <div className="shrink-0 text-right text-xs text-muted-foreground">
          <div>市值 <span className="font-semibold text-foreground">{fmtMarketCap(f?.market_cap)}</span></div>
          <div>{capSource} · {capMeta?.data_as_of_date ? `数据 ${capMeta.data_as_of_date}` : capMeta?.fetched_at ? `抓取 ${capMeta.fetched_at.slice(0, 10)}` : "日期未核实"}{capMeta?.stale ? " · 可能过期" : ""}</div>
          {f?.employees ? <div>员工 {f.employees.toLocaleString()}</div> : null}
          {f?.country ? <div>{f.country}</div> : null}
        </div>
      </div>

      {(profile?.earnings || profile?.analyst || gil) && (
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {profile?.earnings?.next_date && (
            <FactChip
              label="下次财报"
              value={profile.earnings.next_date}
              sub={typeof profile.earnings.days_until === "number" ? `${profile.earnings.days_until} 天后` : undefined}
              danger={typeof profile.earnings.days_until === "number" && profile.earnings.days_until <= 7}
            />
          )}
          {typeof profile?.earnings?.last_surprise_pct === "number" && (
            <FactChip
              label="上次财报意外"
              value={fmtSignedPct(profile.earnings.last_surprise_pct)}
              good={profile.earnings.last_surprise_pct > 0}
            />
          )}
          {useGilRating && gilRating ? (
            <FactChip
              label="分析师评级"
              value={`买${gilRating.buy ?? 0} / 增${gilRating.overweight ?? 0} / 中${gilRating.neutral ?? 0}`}
              sub={`减${gilRating.underweight ?? 0} / 卖${gilRating.sell ?? 0}`}
              meta={`聚源 · ${gil?.as_of ?? "日期未知"}${gilRecent ? "" : " · 过期"}`}
              title="聚源买入、增持、中性、减持、卖出为五档原始分类；与 FMP 的强买、买、持、卖、强卖口径不同，不直接比较。"
            />
          ) : analyst && (
            <FactChip
              label="分析师评级"
              value={`买${(analyst.strong_buy ?? 0) + (analyst.buy ?? 0)} / 持${analyst.hold ?? 0} / 卖${(analyst.sell ?? 0) + (analyst.strong_sell ?? 0)}`}
              good={fmpRecent && analyst.rating_stats?.signal === "strong_bullish"}
              danger={fmpRecent && analyst.rating_stats?.signal === "strong_bearish"}
              sub={
                analyst.rating_stats?.directional_buy_share != null
                  ? `方向买方${fmtPct(analyst.rating_stats.directional_buy_share)}`
                  : undefined
              }
              meta={`FMP · ${analyst.as_of || "日期未提供"}${fmpRecent ? "" : " · 过期或未核实"}`}
              title={[
                analyst.rating_stats?.method || "用买/卖方向评级做 Wilson 置信下界判断，持有视为中性。",
                analyst.rating_stats?.wilson_buy_lower_95 != null ? `买方Wilson下界：${fmtPct(analyst.rating_stats.wilson_buy_lower_95)}` : "",
                analyst.rating_stats?.wilson_sell_lower_95 != null ? `卖方Wilson下界：${fmtPct(analyst.rating_stats.wilson_sell_lower_95)}` : "",
              ].filter(Boolean).join("；")}
            />
          )}
          {useGilTarget && typeof gil?.target_avg_usd === "number" ? (
            <FactChip
              label="平均目标价"
              value={fmtMoney(gil.target_avg_usd)}
              meta={`聚源 · 近${gil.target_window_days ?? 100}日统计 · ${gil.as_of ?? "日期未知"}${gilRecent ? "" : " · 过期"}`}
              title="近一段时间分析师目标价的统计均值，不是未来收益预测；与 FMP 季度目标价均值不可直接比较。"
            />
          ) : typeof analyst?.target_avg_quarter === "number" && (
            <FactChip
              label="平均目标价"
              value={fmtMoney(analyst.target_avg_quarter)}
              meta="FMP · 上季度统计 · 发布日期未提供"
              title="FMP 仅提供季度目标价均值，缺少可核实的发布日期；不据此推算当前上涨空间。"
            />
          )}
        </div>
      )}

      <div className="mt-2 divide-y divide-border/60">
        {gil?.annual_eps_estimates && gil.annual_eps_estimates.length > 0 && !gil.evidence?.forecast?.estimates?.length && (
          <ProfileRow label="年度EPS预期">
            <div className="space-y-1 text-sm">
              {gil.annual_eps_estimates.slice(0, 3).map((estimate) => (
                <div key={estimate.report_period}>
                  年度截至 {estimate.report_period} · {fmtMoney(estimate.mean)} / 股 · {estimate.count ?? "--"} 份预测
                </div>
              ))}
              <div className="text-xs text-muted-foreground">聚源 · 数据 {gil.as_of} · 年度一致预期，不是下一季度财报预期{gilRecent ? "" : " · 数据可能过期"}</div>
            </div>
          </ProfileRow>
        )}
        {gil?.daily_quote && (
          <ProfileRow label="日收盘参考">
            <span className="text-sm">{fmtMoney(gil.daily_quote.close)} · 聚源 {gil.daily_quote.as_of} · 非盘中实时价；复权口径未提供，不用于历史回测补洞</span>
          </ProfileRow>
        )}
        {(profile?.vendor_business_cn || profile?.business_cn) && <ProfileRow label="主营业务">
          {profile.vendor_business_cn || profile.business_cn}
          {profile.vendor_business_cn && <p className="mt-1 text-xs text-muted-foreground">聚源公司资料 · 更新时间未提供</p>}
        </ProfileRow>}
        {profile?.products && profile.products.length > 0 && (
          <ProfileRow label="主要产品"><TagList items={profile.products} /></ProfileRow>
        )}
        {profile?.ai_available && (
          <>
            <ProfileRow label="上游供应"><TagList items={profile.upstream_suppliers} /></ProfileRow>
            <ProfileRow label="下游客户"><TagList items={profile.downstream_customers} /></ProfileRow>
            <ProfileRow label="竞争对手"><TagList items={profile.competitors} /></ProfileRow>
            {profile.moat && <ProfileRow label="核心竞争力">{profile.moat}</ProfileRow>}
            {profile.track_position && <ProfileRow label="赛道地位">{profile.track_position}</ProfileRow>}
          </>
        )}
        {f?.business_summary_en && (
          <ProfileRow label="公司简介">
            <p className="leading-6 text-muted-foreground">{f.business_summary_en}</p>
          </ProfileRow>
        )}
      </div>

      <GildataEvidencePanel evidence={gil?.evidence} marketDate={profile?.market_session} />
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted-foreground">
        <span>{profile?.ai_note}</span>
        {f?.website && (
          <a href={f.website} target="_blank" rel="noreferrer" className="text-primary hover:underline">
            官网 ↗
          </a>
        )}
      </div>
    </section>
  );
}

function fmtAdv(v?: number | null): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "--";
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B/日`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(0)}M/日`;
  return `$${(v / 1e3).toFixed(0)}K/日`;
}

function LeaderCompareCard({ compare }: { compare?: LeaderCompare }) {
  if (!compare?.available || !compare.members || compare.members.length < 2) {
    return null;
  }
  return (
    <section className="rounded border bg-card p-4">
      <div className="flex flex-wrap items-center gap-2 border-b pb-2">
        <h2 className="text-base font-semibold">赛道龙头对比</h2>
        {compare.track_cn && <span className="rounded bg-primary/10 px-2 py-0.5 text-xs text-primary">{compare.track_cn}</span>}
        <span className="ml-auto text-xs text-muted-foreground">按流动性(日均成交额)排序 · 龙头 = {compare.leader}</span>
      </div>
      {compare.verdict && (
        <div className="mt-3 rounded-md border-l-4 border-primary bg-primary/5 p-3 text-sm font-medium leading-6">
          {compare.verdict}
        </div>
      )}
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-muted-foreground">
              <th className="py-1.5 pr-2">代码</th>
              <th className="py-1.5 pr-2 text-right">流动性</th>
              <th className="py-1.5 pr-2">流动性档</th>
              <th className="py-1.5 pr-2 text-right">校准胜率</th>
              <th className="py-1.5 pr-2">回调状态</th>
            </tr>
          </thead>
          <tbody>
            {compare.members.map((m) => (
              <tr key={m.symbol} className={cn("border-b last:border-0", m.symbol === compare.leader && "bg-emerald-500/5")}>
                <td className="py-2 pr-2 font-mono font-semibold">
                  <a className="text-primary hover:underline" href={`/single-stock-overnight?symbol=${m.symbol}`} title="查看个股研判">{m.symbol}</a>
                  {m.symbol === compare.leader && <span className="ml-1 rounded bg-emerald-500/15 px-1 py-0.5 text-[10px] text-emerald-700 dark:text-emerald-300">龙头</span>}
                  {m.is_query && m.symbol !== compare.leader && <span className="ml-1 rounded bg-amber-500/15 px-1 py-0.5 text-[10px] text-amber-700 dark:text-amber-300">本标的</span>}
                </td>
                <td className="py-2 pr-2 text-right tabular-nums">{fmtAdv(m.adv)}</td>
                <td className="py-2 pr-2 text-xs text-muted-foreground">{m.liquidity?.tier ?? "--"}</td>
                <td className="py-2 pr-2 text-right tabular-nums">{m.calibrated_win_rate != null ? `${(m.calibrated_win_rate * 100).toFixed(0)}%` : "--"}</td>
                <td className={cn("py-2 pr-2 text-xs", m.in_pullback ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground")}>{m.pullback_state ?? "--"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">{compare.note}</p>
    </section>
  );
}

function SignalReadCard({ sr, symbol, price }: { sr?: SignalRead; symbol: string; price?: number }) {
  if (!sr || !sr.available) {
    return (
      <section className="rounded border border-dashed border-muted-foreground/30 bg-card p-4 text-sm text-muted-foreground">
        信号判定暂不可用：{sr?.reason || "价格历史不足"}
      </section>
    );
  }
  const badge = SR_BADGE[sr.confidence_badge || "experimental"] ?? SR_BADGE.experimental;
  const basis = SR_BASIS[sr.signal_basis || ""] ?? sr.signal_basis ?? "--";
  const win = sr.calibrated_win_rate;
  const rs = sr.relative_strength_20d;
  const hvUp = (sr.hv?.hv_state || "").includes("扩张");
  const pulledBack = (sr.pullback_state || "").includes("回调");
  const verdict = pulledBack && hvUp
    ? "符合『回调 + 波动扩张』组合：已验证的最强买点形态。"
    : pulledBack
      ? "处于回调区，但波动尚未扩张——可等波动放大的启动确认。"
      : "未处于回调区（偏强/追高），不符合回调买入口径。";
  return (
    <section className="rounded border border-primary/40 bg-primary/5 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Target className="h-5 w-5 text-primary" />
          <h2 className="text-base font-semibold">信号判定 · 该不该买</h2>
          <span className={cn("inline-flex items-center rounded-md border px-2 py-0.5 text-[11px] font-medium", badge.cls)}>
            {badge.label}
          </span>
        </div>
        <span className="text-xs text-muted-foreground">信号口径：{basis}</span>
      </div>
      <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded border bg-card p-3">
          <div className="text-xs text-muted-foreground">校准胜率（净成本后跑赢自身基线）</div>
          <div className="mt-1 text-2xl font-bold text-primary">{fmtPct(win)}</div>
        </div>
        <div className="rounded border bg-card p-3">
          <div className="text-xs text-muted-foreground">回调状态</div>
          <div className={cn("mt-1 text-lg font-semibold", pulledBack ? "text-success" : "text-muted-foreground")}>
            {sr.pullback_state || "--"}
          </div>
        </div>
        <div className="rounded border bg-card p-3">
          <div className="text-xs text-muted-foreground">波动趋势</div>
          <div className={cn("mt-1 text-lg font-semibold", hvUp ? "text-success" : "text-muted-foreground")}>
            {sr.hv?.hv_state || "--"}
            {sr.hv?.hv_rise != null && <span className="ml-1 text-sm font-mono">{fmtSignedPct(sr.hv.hv_rise)}</span>}
          </div>
        </div>
        <div className="rounded border bg-card p-3">
          <div className="text-xs text-muted-foreground">相对大盘强度(20日)</div>
          <div className={cn("mt-1 text-lg font-semibold font-mono", (rs ?? 0) >= 0 ? "text-success" : "text-danger")}>
            {fmtSignedPct(rs)}
          </div>
        </div>
      </div>
      <p className="mt-3 text-sm text-muted-foreground">
        <span className="font-medium text-foreground">{symbol}</span>
        {typeof price === "number" ? ` @ ${fmtMoney(price)}：` : "："}{verdict}
        {sr.calibration_source !== "calibrated" && "（注：当前用经验回退，非已验证校准）"}
      </p>
    </section>
  );
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="rounded border bg-muted/30 p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-xl font-semibold">{value}</div>
      <div className="text-xs text-muted-foreground">{detail}</div>
    </div>
  );
}

function ActionBox({
  label,
  value,
  detail,
  tone,
  pct,
}: {
  label: string;
  value: string;
  detail: string;
  tone: "good" | "bad" | "warn";
  pct?: number | null;
}) {
  const hasPct = typeof pct === "number" && Number.isFinite(pct);
  const up = hasPct && (pct as number) >= 0;
  return (
    <div
      className={cn(
        "rounded border p-3",
        tone === "good" && "border-success/25 bg-success/10",
        tone === "bad" && "border-danger/25 bg-danger/10",
        tone === "warn" && "border-warning/25 bg-warning/10",
      )}
    >
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="text-lg font-bold">{value}</span>
        {hasPct && (
          <span
            className={cn(
              "rounded px-1 text-[11px] font-semibold",
              up ? "bg-success/15 text-success" : "bg-danger/15 text-danger",
            )}
          >
            {fmtSignedPct(pct)}
          </span>
        )}
      </div>
      <div className="mt-1 text-xs text-muted-foreground">{detail}</div>
    </div>
  );
}

function Mini({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded bg-background/60 p-2">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="font-semibold">{value}</div>
    </div>
  );
}

function WatchLevels({ plan, fallback }: { plan: AnyRecord; fallback?: string[] }) {
  if (!plan?.available) {
    const list = Array.isArray(fallback) && fallback.length ? fallback : [plan?.reason || "暂无可用关注价位"];
    return (
      <ul className="space-y-1 text-sm text-muted-foreground">
        {list.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    );
  }
  const items: { label: string; price: unknown; pct: unknown; tone: "good" | "bad" }[] = [
    { label: "买入触发 · 强势", price: plan.buy_strength_price, pct: plan.buy_strength_pct, tone: "good" },
    { label: "买入触发 · 回踩", price: plan.buy_pullback_price, pct: plan.buy_pullback_pct, tone: "good" },
    { label: "停止买入", price: plan.stop_buying_below, pct: plan.stop_buying_below_pct, tone: "bad" },
    { label: "止损", price: plan.stop_loss, pct: plan.stop_loss_pct, tone: "bad" },
    { label: "止盈", price: plan.take_profit, pct: plan.take_profit_pct, tone: "good" },
  ];
  return (
    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
      {items.map((item) => {
        const hasPct = typeof item.pct === "number" && Number.isFinite(item.pct);
        const up = hasPct && (item.pct as number) >= 0;
        return (
          <div
            key={item.label}
            className={cn(
              "rounded border p-2",
              item.tone === "good" ? "border-success/25 bg-success/5" : "border-danger/25 bg-danger/5",
            )}
          >
            <div className="text-xs text-muted-foreground">{item.label}</div>
            <div className="mt-1 text-base font-bold">{fmtMoney(item.price)}</div>
            {hasPct && (
              <span
                className={cn(
                  "mt-1 inline-block rounded px-1 text-[11px] font-semibold",
                  up ? "bg-success/15 text-success" : "bg-danger/15 text-danger",
                )}
              >
                {fmtSignedPct(item.pct)}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

function ListBlock({ title, items, empty, tone }: { title: string; items?: string[]; empty: string; tone: "good" | "bad" | "warn" }) {
  const list = Array.isArray(items) && items.length ? items : [empty];
  return (
    <div className="rounded border bg-muted/20 p-3">
      <div
        className={cn(
          "mb-2 text-sm font-semibold",
          tone === "good" && "text-success",
          tone === "bad" && "text-danger",
          tone === "warn" && "text-warning",
        )}
      >
        {title}
      </div>
      <ul className="space-y-1 text-sm text-muted-foreground">
        {list.map((item, index) => (
          <li key={`${title}-${index}`}>{item}</li>
        ))}
      </ul>
    </div>
  );
}
