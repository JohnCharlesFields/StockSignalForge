import { Fragment, useEffect, useState } from "react";
import { Activity, AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, ClipboardCheck, Crown, Droplets, History, Loader2, RefreshCw, ShieldQuestion, X } from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { api, type ConfidenceBadge, type LeaderCompareData, type PredictionScorecard, type PriorityBasketResponse, type PriorityBoardResponse, type PriorityMonitorResponse, type PriorityPick } from "@/lib/api";
import { authHeaders } from "@/lib/apiAuth";
import { LongOptionBoard } from "./LongOptionBoard";

function fmtAdvShort(v?: number | null): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "--";
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(0)}M`;
  return `$${(v / 1e3).toFixed(0)}K`;
}

function pct(v: number | undefined | null, d = 0): string {
  if (v === undefined || v === null || !Number.isFinite(v)) return "--";
  return `${(v * 100).toFixed(d)}%`;
}
function signedPct(v: number | undefined | null, d = 1): string {
  if (v === undefined || v === null || !Number.isFinite(v)) return "--";
  return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(d)}%`;
}

const SIGNAL_LABEL: Record<string, string> = {
  pullback_hv: "回调+波动扩张(10日)",
  launch_contrarian: "回调买入(5日)",
  daily_tunnel_contrarian: "日隧道回调(5日)",
  launch: "启动分(5日·研究)",
};

function signalLabel(basis?: string | null): string {
  if (!basis) return "--";
  return SIGNAL_LABEL[basis] ?? basis;
}

const BADGE: Record<ConfidenceBadge, { label: string; cls: string }> = {
  validated: { label: "已验证", cls: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30" },
  low_edge: { label: "低把握·未验证", cls: "bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30" },
  experimental: { label: "实验·未校准", cls: "bg-slate-500/15 text-slate-600 dark:text-slate-300 border-slate-500/30" },
};

const LIQ_TONE: Record<string, string> = {
  strong: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border border-emerald-500/30",
  good: "bg-sky-500/15 text-sky-700 dark:text-sky-300 border border-sky-500/30",
  ok: "bg-slate-500/15 text-slate-600 dark:text-slate-300 border border-slate-500/30",
  warn: "bg-amber-500/15 text-amber-700 dark:text-amber-300 border border-amber-500/40",
  unknown: "bg-muted text-muted-foreground border border-transparent",
};

function SourceUniverseBadges({ ids, labels }: { ids?: string[] | null; labels?: string[] | null }) {
  const cleanIds = Array.from(new Set((ids ?? []).map((x) => String(x || "").trim()).filter(Boolean)));
  const cleanLabels = Array.from(new Set((labels ?? []).map((x) => String(x || "").trim()).filter(Boolean)));
  const items = (cleanIds.length ? cleanIds : cleanLabels).slice(0, 3);
  if (!items.length) return null;
  const title = cleanLabels.length ? cleanLabels.join(" / ") : cleanIds.join(" / ");
  return (
    <div className="mt-1 flex flex-wrap gap-1" title={`来源股票池：${title}`}>
      {items.map((item) => (
        <span
          key={item}
          className="inline-flex rounded border border-indigo-500/25 bg-indigo-500/10 px-1.5 py-0.5 text-[10px] font-medium leading-none text-indigo-700 dark:text-indigo-300"
        >
          {cleanIds.length ? item.toUpperCase() : item}
        </span>
      ))}
      {(cleanIds.length || cleanLabels.length) > items.length && (
        <span className="inline-flex rounded border border-indigo-500/20 bg-indigo-500/5 px-1.5 py-0.5 text-[10px] leading-none text-indigo-700/80 dark:text-indigo-300/80">
          +{(cleanIds.length || cleanLabels.length) - items.length}
        </span>
      )}
    </div>
  );
}

function tagToneClass(tone: "strong" | "good" | "neutral" | "warn" | "bad" | string | undefined) {
  switch (tone) {
    case "strong":
      return "border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300";
    case "good":
      return "border-green-500/30 bg-green-500/10 text-green-700 dark:text-green-300";
    case "warn":
      return "border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-300";
    case "bad":
      return "border-rose-500/40 bg-rose-500/15 text-rose-700 dark:text-rose-300";
    default:
      return "border-slate-500/25 bg-slate-500/10 text-slate-600 dark:text-slate-300";
  }
}

function PickTags({ pick }: { pick: PriorityPick }) {
  const tags: Array<{ key: string; label: string; tone?: string; title?: string }> = [];

  const badge = BADGE[pick.confidence_badge];
  if (badge) {
    tags.push({
      key: "confidence",
      label: badge.label,
      tone: pick.confidence_badge === "validated" ? "strong" : pick.confidence_badge === "low_edge" ? "warn" : "neutral",
      title: "信号校准状态",
    });
  }

  const days = typeof pick.earnings?.days_until === "number" ? pick.earnings.days_until : null;
  if (pick.earnings?.available && pick.earnings.next_date) {
    const near = days !== null && days >= 0 && days <= 10;
    const tone = near ? "warn" : pick.earnings.estimate_tone === "positive" ? "good" : pick.earnings.estimate_tone === "warning" ? "warn" : "neutral";
    tags.push({
      key: "earnings",
      label: days !== null ? `财报${days}天` : "财报",
      tone,
      title: [
        `下一次财报：${pick.earnings.next_date}`,
        pick.earnings.eps_estimated != null ? `EPS预期：${pick.earnings.eps_estimated}` : "",
        pick.earnings.estimate_basis || "",
      ].filter(Boolean).join("；"),
    });
  }

  if (pick.deep_oversold && pick.deep_oversold.level !== "none") {
    tags.push({
      key: "deep_oversold",
      label: pick.deep_oversold.level === "deep" ? "深超卖" : "超卖",
      tone: pick.deep_oversold.level === "deep" ? "strong" : "good",
      title: `回测正向标签：z=${pick.deep_oversold.z ?? "--"} RSI2=${pick.deep_oversold.rsi2 ?? "--"}`,
    });
  }

  for (const item of pick.playbook_enhancements?.labels ?? []) {
    if (!item?.label) continue;
    tags.push({
      key: `playbook:${item.id || item.label}`,
      label: item.label,
      tone: item.tone === "strong" ? "strong" : item.tone === "warn" ? "warn" : "good",
      title: item.reason || item.label,
    });
  }

  if (pick.soft_enhancement_nudge && pick.soft_enhancement_nudge > 0) {
    tags.push({
      key: "soft_nudge",
      label: `软增强+${pick.soft_enhancement_nudge.toFixed(3)}`,
      tone: pick.soft_enhancement_nudge >= 0.02 ? "strong" : "good",
      title: "已回测为正向但权重较轻的增强项",
    });
  }

  if (pick.news?.trump) {
    tags.push({ key: "trump", label: "TRUMP", tone: "bad", title: "政治人物提及，短线双向波动风险升高" });
  }
  if (pick.news?.label) {
    const negative = (pick.news.neg ?? 0) > (pick.news.pos ?? 0);
    const clean = pick.news.label.replace("舆情·", "").replace("舆情路", "");
    tags.push({
      key: "news",
      label: clean,
      tone: negative ? "warn" : "good",
      title: `近10日新闻：正${pick.news.pos ?? 0} / 负${pick.news.neg ?? 0}`,
    });
  }

  const sourceLabels = Array.from(new Set((pick.source_universe_labels?.length ? pick.source_universe_labels : pick.source_universe_ids ?? [])
    .map((x) => String(x || "").trim()).filter(Boolean))).slice(0, 3);
  for (const source of sourceLabels) {
    tags.push({ key: `source:${source}`, label: source.length > 16 ? `${source.slice(0, 16)}...` : source.toUpperCase(), tone: "neutral", title: `来源池：${source}` });
  }

  if (!tags.length) {
    return <span className="text-muted-foreground/60">--</span>;
  }
  return (
    <div className="flex max-w-full flex-wrap gap-1.5">
      {tags.map((tag) => (
        <span
          key={tag.key}
          className={cn("inline-flex max-w-full rounded border px-1.5 py-0.5 text-[10px] font-medium leading-tight", tagToneClass(tag.tone))}
          title={tag.title}
        >
          <span className="truncate">{tag.label}</span>
        </span>
      ))}
    </div>
  );
}

function LiquidityBadge({ liq }: { liq?: { adv?: number | null; tier?: string; tone?: string } | null }) {
  if (!liq?.tier || liq.tier === "未知") return <span className="text-muted-foreground/50">--</span>;
  return (
    <span
      className={cn("inline-flex items-center rounded px-2 py-0.5 text-[11px] font-medium", LIQ_TONE[liq.tone || "unknown"] ?? LIQ_TONE.unknown)}
      title={`日均成交额 ${fmtAdvShort(liq.adv)}/日 · 流动性=判断错时的脱身/容错空间，不等于一定回本；弱流动性需更小仓位`}
    >
      {liq.tier}
    </span>
  );
}

function Badge({ badge }: { badge: ConfidenceBadge }) {
  const b = BADGE[badge] ?? BADGE.experimental;
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium", b.cls)}>
      <ShieldQuestion className="h-3 w-3" aria-hidden="true" />
      {b.label}
    </span>
  );
}

type LeaderState = "loading" | LeaderCompareData | { error: string };

const MONITOR_STATUS: Record<string, { label: string; cls: string }> = {
  NEW: { label: "新出现", cls: "bg-sky-500/15 text-sky-700 dark:text-sky-300" },
  WATCHING: { label: "持续观察", cls: "bg-slate-500/15 text-slate-700 dark:text-slate-300" },
  IMPROVING: { label: "排名改善", cls: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300" },
  WEAKENING: { label: "转弱", cls: "bg-amber-500/15 text-amber-700 dark:text-amber-300" },
  INVALIDATED: { label: "已失效", cls: "bg-rose-500/15 text-rose-700 dark:text-rose-300" },
};

function MonitorStatusBadge({ status }: { status?: string | null }) {
  const item = MONITOR_STATUS[status || ""] ?? MONITOR_STATUS.WATCHING;
  return <span className={cn("inline-flex rounded px-2 py-0.5 text-[11px] font-medium", item.cls)}>{item.label}</span>;
}

export function PriorityBoard() {
  const [mode, setMode] = useState<"stocks" | "options">("stocks");
  const [board, setBoard] = useState<PriorityBoardResponse | null>(null);
  const [monitor, setMonitor] = useState<PriorityMonitorResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [monitorLoading, setMonitorLoading] = useState(true);
  const [view, setView] = useState<"today" | "monitor" | "scorecard" | "basket">("today");
  const [liquidOnly, setLiquidOnly] = useState<boolean>(() => {
    const v = typeof localStorage !== "undefined" ? localStorage.getItem("priorityBoard.liquidOnly") : null;
    return v === null ? true : v === "1"; // default ON
  });
  const [oversoldOnly, setOversoldOnly] = useState<boolean>(() => {
    return (typeof localStorage !== "undefined" ? localStorage.getItem("priorityBoard.oversoldOnly") : null) === "1"; // default OFF
  });
  const [liveBanner, setLiveBanner] = useState<boolean>(() => {
    const v = typeof localStorage !== "undefined" ? localStorage.getItem("priorityBoard.liveBannerDismissed") : null;
    return v !== "1"; // default shown
  });
  const dismissLiveBanner = () => {
    setLiveBanner(false);
    if (typeof localStorage !== "undefined") localStorage.setItem("priorityBoard.liveBannerDismissed", "1");
  };
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [leaders, setLeaders] = useState<Record<string, LeaderState>>({});

  const toggleLeader = (symbol: string) => {
    setExpanded((prev) => ({ ...prev, [symbol]: !prev[symbol] }));
    if (!leaders[symbol]) {
      setLeaders((prev) => ({ ...prev, [symbol]: "loading" }));
      api
        .getTrackLeader(symbol)
        .then((r) => setLeaders((prev) => ({ ...prev, [symbol]: r.leader_compare ?? { available: false } })))
        .catch((err) => setLeaders((prev) => ({ ...prev, [symbol]: { error: err instanceof Error ? err.message : "加载失败" } })));
    }
  };

  const load = (force = false, liquid = liquidOnly, oversold = oversoldOnly) => {
    setLoading(true);
    api
      .getPriorityBoard(force ? 30 : 20, liquid, oversold, force)
      .then(setBoard)
      .catch((err) => toast.error(err instanceof Error ? err.message : "无法加载优先级看板"))
      .finally(() => setLoading(false));
    setMonitorLoading(true);
    api
      .getPriorityMonitor(120, 0, 30)
      .then(setMonitor)
      .catch(() => setMonitor(null))
      .finally(() => setMonitorLoading(false));
  };

  const toggleLiquidOnly = () => {
    const next = !liquidOnly;
    setLiquidOnly(next);
    if (typeof localStorage !== "undefined") localStorage.setItem("priorityBoard.liquidOnly", next ? "1" : "0");
    load(false, next, oversoldOnly);
  };
  const toggleOversoldOnly = () => {
    const next = !oversoldOnly;
    setOversoldOnly(next);
    if (typeof localStorage !== "undefined") localStorage.setItem("priorityBoard.oversoldOnly", next ? "1" : "0");
    load(false, liquidOnly, next);
  };

  const [exporting, setExporting] = useState(false);
  const exportPdf = async () => {
    setExporting(true);
    toast.info("正在生成前 3 标的研判 PDF（约 10–20 秒）…");
    try {
      const resp = await fetch(`/priority-board/export-pdf?top=3&liquid_only=${liquidOnly}&oversold_only=${oversoldOnly}`, { headers: authHeaders() });
      if (!resp.ok) throw new Error("导出失败");
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "回调买入榜_前3研判.pdf";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast.success("PDF 已导出");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导出失败");
    } finally {
      setExporting(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  return (
    <div className="h-full overflow-y-auto bg-background">
      <main className="mx-auto max-w-6xl px-6 py-6 space-y-5">
        {mode === "stocks" && liveBanner && (
          <section className="flex items-start gap-2.5 rounded-lg border-2 border-amber-500/50 bg-amber-500/10 p-3 text-sm">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600 dark:text-amber-400" />
            <div className="flex-1">
              <div className="font-semibold text-amber-700 dark:text-amber-300">实盘验证期 · 这不是已验证策略</div>
              <div className="mt-0.5 text-xs leading-relaxed text-amber-700/90 dark:text-amber-200/90">
                历史校准不等于前向盈利；实时兑现笔数与净超额请以「兑现对账」为准。
                <b> validated 徽章只代表历史校准口径，≠ 实盘已验证</b>；这里的胜率是「跑赢自身基线」的概率，非赚钱概率。
                榜单仅供研究，详见仓库 <code>LIVE_TRADING_CHECKLIST.md</code>。
              </div>
            </div>
            <button type="button" onClick={dismissLiveBanner} className="shrink-0 rounded p-1 text-amber-600 hover:bg-amber-500/20 dark:text-amber-400" title="本机不再提示">
              <X className="h-4 w-4" />
            </button>
          </section>
        )}
        <section className="flex flex-col gap-3 border-b pb-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="mb-1 inline-flex items-center gap-2 text-xs font-medium text-amber-600 dark:text-amber-400">
              <Crown className="h-3.5 w-3.5" aria-hidden="true" />
              回调买入榜
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">{mode === "stocks" ? "优先级看板" : "单腿期权"}</h1>
            {mode === "stocks" && <>
            <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
              复合优先级 = 相对大盘强度 + 校准胜率（已验证的回调买入信号）+ 组合择时。按"最可能跑赢自身基线"的回调标的排序。
            </p>
            <p className="mt-2 max-w-2xl text-xs leading-relaxed text-muted-foreground">
              当前排名逻辑：先按校准胜率排序；同胜率档内再用优先级分、组合择时、相对强度，以及“市场+流动性+RS前40%”“强于行业ETF”两个软增强标签做微调。软增强不会改写胜率，只进入排序微调和前向兑现对账。
            </p>
            </>}
          </div>
          <div className="flex flex-wrap items-center gap-2 self-start">
            <button
              type="button"
              onClick={() => setMode(mode === "stocks" ? "options" : "stocks")}
              title={mode === "stocks" ? "查看最近一次单腿期权筛选快照" : "返回正股回调买入榜"}
              aria-pressed={mode === "options"}
              className={cn("inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm", mode === "options" ? "border-primary bg-primary/10 text-primary" : "hover:bg-muted")}
            >
              <Activity className="h-4 w-4" />{mode === "stocks" ? "单腿期权" : "返回正股"}
            </button>
            {mode === "stocks" && <>
            <button
              type="button"
              onClick={toggleLiquidOnly}
              title="只看高流动性：SPX(∪NDX) 按美元日均成交额取前 N，低于该门槛的候选不显示。仅影响展示，全量候选仍入库对账。"
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm",
                liquidOnly ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "text-muted-foreground hover:bg-muted",
              )}
            >
              <Droplets className="h-4 w-4" />
              仅高流动性{liquidOnly ? "·开" : "·关"}
            </button>
            <button
              type="button"
              onClick={toggleOversoldOnly}
              title="只看深超卖：跌破布林下轨/Z<-2/RSI2<10。回测显示在 pullback_hv 顶桶里深超卖净超额更高(+1.23% vs +0.40%, 胜率60%)。仅影响展示，全量仍入库对账。"
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm",
                oversoldOnly ? "border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300" : "text-muted-foreground hover:bg-muted",
              )}
            >
              仅深超卖{oversoldOnly ? "·开" : "·关"}
            </button>
            <button
              type="button"
              onClick={exportPdf}
              disabled={exporting}
              title="把当前筛选下的前 3 标的导出为手机版研判 PDF（行情图+基本面+指标+行动价位+AI 复核，长图不分页）"
              className="inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-60"
            >
              {exporting ? <Loader2 className="h-4 w-4 animate-spin" /> : <ClipboardCheck className="h-4 w-4" />}
              导出PDF
            </button>
            <button
              type="button"
              onClick={() => load(true)}
              disabled={loading}
              className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-60"
            >
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              刷新
            </button>
            </>}
          </div>
        </section>

        {mode === "options" && <LongOptionBoard stockBoard={board} />}

        {mode === "stocks" && <>

        {board && (
          <section className="flex flex-wrap items-center gap-3 rounded-lg border bg-card p-3 text-sm">
            <Badge badge={board.confidence.badge} />
            <span className="text-muted-foreground">{board.confidence.note}</span>
            <span className="ml-auto text-xs text-muted-foreground">
              候选 {board.n_candidates} · 基准 SPY 20日 {signedPct(board.benchmark_return_20d)} ·
              信号 {signalLabel(board.signal_basis)}
            </span>
          </section>
        )}

        {board && (
          <section className={cn(
            "flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm",
            board.data_stale ? "border-amber-500/50 bg-amber-500/10 text-amber-800 dark:text-amber-200" : "bg-card text-muted-foreground",
          )}>
            {board.data_stale && <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />}
            <span>
              {board.data_as_of ? `候选批次参考交易日：${board.data_as_of}` : "候选批次日期未核实"}
              {board.data_stale ? " · 数据已过期，榜单仅供历史复盘；请等待当日扫描与候选落库完成。" : " · 个别标的行情缓存可能更早，请在个股页核对。"}
            </span>
          </section>
        )}

        {board?.liquidity_floor?.applied && (board.liquidity_floor.threshold_adv ?? 0) > 0 && (
          <section className="flex flex-wrap items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-xs">
            <Droplets className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
            <span className="font-medium text-emerald-700 dark:text-emerald-300">高流动性门槛已开</span>
            <span className="text-muted-foreground">
              仅显示 $ADV ≥ {fmtAdvShort(board.liquidity_floor.threshold_adv)}/日（{board.liquidity_floor.base}前{board.liquidity_floor.top_n}）。
              低流动性候选已隐藏（仍入库对账）。点右上「仅高流动性」可关。
            </span>
          </section>
        )}

        <section className="rounded-lg border bg-card p-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <div className="text-sm font-medium">回调买入生命周期监测</div>
              <div className="mt-1 text-xs text-muted-foreground">
                每日把所有跑取候选入库，TopN 只是榜单标记；连续出现、排名改善和失效会在这里追踪。
              </div>
            </div>
            <div className="inline-flex rounded-md border p-1 text-sm">
              <button
                type="button"
                onClick={() => setView("today")}
                className={cn("rounded px-3 py-1.5", view === "today" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted")}
              >
                最新候选
              </button>
              <button
                type="button"
                onClick={() => setView("monitor")}
                className={cn("rounded px-3 py-1.5", view === "monitor" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted")}
              >
                持续监测
              </button>
              <button
                type="button"
                onClick={() => setView("scorecard")}
                className={cn("rounded px-3 py-1.5", view === "scorecard" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted")}
              >
                兑现对账
              </button>
              <button
                type="button"
                onClick={() => setView("basket")}
                className={cn("rounded px-3 py-1.5", view === "basket" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted")}
              >
                今日篮子
              </button>
            </div>
          </div>
          <div className="mt-3 grid gap-2 text-xs sm:grid-cols-5">
            <div className="rounded border bg-muted/30 p-2">
              <div className="text-muted-foreground">监测日期</div>
              <div className="font-mono">{monitor?.as_of_date || "--"}</div>
            </div>
            <div className="rounded border bg-muted/30 p-2">
              <div className="text-muted-foreground">已入库标的</div>
              <div className="font-mono">{monitor?.summary.tracked_symbols ?? "--"}</div>
            </div>
            <div className="rounded border bg-muted/30 p-2">
              <div className="text-muted-foreground">排名改善</div>
              <div className="font-mono">{monitor?.summary.improving ?? "--"}</div>
            </div>
            <div className="rounded border bg-muted/30 p-2">
              <div className="text-muted-foreground">持续观察</div>
              <div className="font-mono">{monitor?.summary.watching ?? "--"}</div>
            </div>
            <div className="rounded border bg-muted/30 p-2">
              <div className="text-muted-foreground">今日展示</div>
              <div className="font-mono">{view === "monitor" ? monitor?.rows.length ?? 0 : board?.picks.length ?? 0}</div>
            </div>
          </div>
        </section>

        {board?.no_edge_today && (
          <section className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm text-warning">
            截至 {board.data_as_of || "最近一次数据"} 无显著买点：最高校准胜率仅 {pct(board.top_calibrated_win_rate)}（&lt;50%，即最好的名次也预计跑不赢自身基线）。
            这是上涨市里"逆水行舟"的信号——建议观望，不要为了交易而交易。
          </section>
        )}

        {board?.win_rate_explainer && (
          <details className="rounded-lg border bg-muted/30 p-3 text-sm">
            <summary className="cursor-pointer font-medium">ℹ️ 这个「胜率」是怎么算的？（点开看口径）</summary>
            <p className="mt-2 leading-relaxed text-muted-foreground">{board.win_rate_explainer}</p>
            <div className="mt-2 rounded border border-sky-500/30 bg-sky-500/5 p-2 text-xs leading-relaxed text-muted-foreground">
              <div className="font-medium text-foreground">当前算法口径</div>
              <div>主胜率：只看 pullback_hv 校准曲线，不包含软增强标签。</div>
              <div>主排名：先按校准胜率，再按优先级/组合择时；若胜率接近，再用“市场+流动性+RS前40%”“强于行业ETF”做小幅排序微调。</div>
              <div>
                数据来源：
                <span className="ml-1 font-medium text-foreground">
                  {board.calibration_provenance?.source_label || board.calibration_provenance?.price_source || board.calibration_provenance?.source || "未知来源"}
                </span>
                {board.calibration_provenance?.provenance_complete === false && (
                  <span className="ml-2 rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-700 dark:text-amber-300">来源审计不完整</span>
                )}
              </div>
            </div>
            {board.calibration_provenance?.available && (
              <div className="mt-2 space-y-1 border-t pt-2 text-xs text-muted-foreground">
                <div>
                  数据来源：<span className="font-medium text-foreground">{board.calibration_provenance.price_source || board.calibration_provenance.source}</span>
                  {board.calibration_provenance.survivorship_controlled && (
                    <span className="ml-1 rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-700 dark:text-emerald-300">已去生存者偏差</span>
                  )}
                </div>
                {board.calibration_provenance.window && (
                  <div>校准窗口：{board.calibration_provenance.window[0]} ~ {board.calibration_provenance.window[1]}
                    {board.calibration_provenance.event_count ? ` · 样本 ${board.calibration_provenance.event_count.toLocaleString()} 事件` : ""}</div>
                )}
                {board.calibration_provenance.in_sample && (
                  <div className="text-amber-600 dark:text-amber-400">⚠️ 样本内、单一区间（偏牛市），尚未跨熊市样本外验证——别当成保证。</div>
                )}
              </div>
            )}
          </details>
        )}

        {view === "monitor" && (
          <section className="rounded-lg border bg-card">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[900px] text-sm">
                <thead className="bg-muted/40 text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3 text-left font-medium">排名</th>
                    <th className="px-4 py-3 text-left font-medium">标的</th>
                    <th className="px-4 py-3 text-left font-medium">监测状态</th>
                    <th className="px-4 py-3 text-right font-medium">连续观察</th>
                    <th className="px-4 py-3 text-right font-medium">排名变化</th>
                    <th className="px-4 py-3 text-right font-medium">校准胜率</th>
                    <th className="px-4 py-3 text-right font-medium">优先级</th>
                    <th className="px-4 py-3 text-left font-medium">波动状态</th>
                    <th className="px-4 py-3 text-left font-medium">理由</th>
                  </tr>
                </thead>
                <tbody>
                  {monitorLoading && (
                    <tr>
                      <td colSpan={9} className="px-4 py-12 text-center text-muted-foreground">
                        <Loader2 className="mx-auto h-5 w-5 animate-spin" />
                      </td>
                    </tr>
                  )}
                  {!monitorLoading && (!monitor || monitor.rows.length === 0) && (
                    <tr>
                      <td colSpan={9} className="px-4 py-12 text-center text-muted-foreground">
                        暂无每日切片。点击刷新或等待每日扫描后，会把所有跑取候选写入数据库。
                      </td>
                    </tr>
                  )}
                  {monitor?.rows.map((r) => (
                    <tr key={`${r.as_of_date}-${r.symbol}`} className="border-t hover:bg-muted/30">
                      <td className="px-4 py-3 text-muted-foreground">
                        #{r.candidate_rank ?? "--"}
                        {r.is_board_pick && <span className="ml-2 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">Top</span>}
                      </td>
                      <td className="px-4 py-3 font-semibold">
                        <a className="text-primary hover:underline" href={r.detail_url || `/single-stock-overnight?symbol=${r.symbol}`}>
                          {r.symbol}
                        </a>
                        {r.current_price != null && Number.isFinite(r.current_price) && (
                          <span className="ml-2 font-mono text-xs text-muted-foreground">${r.current_price.toFixed(2)}</span>
                        )}
                        {(r.track || r.track_cn) && (
                          <div className="mt-0.5 text-[11px] font-normal text-muted-foreground">{r.track_cn || r.track}</div>
                        )}
                        <SourceUniverseBadges ids={r.source_universe_ids} labels={r.source_universe_labels} />
                      </td>
                      <td className="px-4 py-3"><MonitorStatusBadge status={r.status} /></td>
                      <td className="px-4 py-3 text-right font-mono">{r.days_seen ?? "--"}天</td>
                      <td className={cn("px-4 py-3 text-right font-mono", (r.rank_delta ?? 0) > 0 ? "text-emerald-600 dark:text-emerald-400" : (r.rank_delta ?? 0) < 0 ? "text-amber-600 dark:text-amber-400" : "text-muted-foreground")}>
                        {r.rank_delta == null ? "--" : r.rank_delta > 0 ? `+${r.rank_delta}` : String(r.rank_delta)}
                      </td>
                      <td className="px-4 py-3 text-right font-mono">{pct(r.calibrated_probability)}</td>
                      <td className="px-4 py-3 text-right font-mono">{r.priority_score != null ? r.priority_score.toFixed(3) : "--"}</td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">
                        {r.hv?.hv_state || "--"}
                        {r.hv?.hv_rise != null && <span className="ml-1 font-mono">{signedPct(r.hv.hv_rise, 0)}</span>}
                      </td>
                      <td className="max-w-[320px] px-4 py-3 text-xs text-muted-foreground">{r.reason || "--"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {view === "scorecard" && <ScorecardView />}

        {view === "basket" && <BasketView />}

        {view === "today" && (
        <section className="rounded-lg border bg-card">
          <div className="overflow-x-auto">
            <table className="w-full table-fixed text-sm">
              <colgroup>
                <col className="w-[5%]" />
                <col className="w-[12%]" />
                <col className="w-[11%]" />
                <col className="w-[10%]" />
                <col className="w-[8%]" />
                <col className="w-[9%]" />
                <col className="w-[10%]" />
                <col className="w-[11%]" />
                <col className="w-[7%]" />
                <col className="w-[17%]" />
              </colgroup>
              <thead className="bg-muted/40 text-xs text-muted-foreground">
                <tr>
                  <th className="px-2.5 py-3 text-left font-medium">#</th>
                  <th className="px-2.5 py-3 text-left font-medium">标的</th>
                  <th className="px-2.5 py-3 text-left font-medium">赛道</th>
                  <th className="px-2.5 py-3 text-left font-medium">流动性</th>
                  <th className="px-2.5 py-3 text-right font-medium">优先级</th>
                  <th className="px-2.5 py-3 text-right font-medium">校准胜率</th>
                  <th className="px-2.5 py-3 text-right font-medium" title="相对大盘强度（近20日）">相对大盘</th>
                  <th className="px-2.5 py-3 text-left font-medium">波动趋势</th>
                  <th className="px-2.5 py-3 text-right font-medium">择时×</th>
                  <th className="px-2.5 py-3 text-left font-medium">Tags</th>
                </tr>
              </thead>
              <tbody>
                {loading && !board && (
                  <tr>
                    <td colSpan={10} className="px-4 py-12 text-center text-muted-foreground">
                      <Loader2 className="mx-auto h-5 w-5 animate-spin" />
                    </td>
                  </tr>
                )}
                {board && board.picks.length === 0 && (
                  <tr>
                    <td colSpan={10} className="px-4 py-12 text-center text-muted-foreground">
                      暂无候选（等待当日快照生成）。
                    </td>
                  </tr>
                )}
                {board?.picks.map((p, i) => (
                  <Fragment key={p.symbol}>
                  <tr className="border-t align-top transition-colors hover:bg-muted/30">
                    <td className="px-2.5 py-3 text-muted-foreground">
                      <button
                        type="button"
                        onClick={() => toggleLeader(p.symbol)}
                        className="mr-1 inline-flex items-center text-muted-foreground hover:text-foreground"
                        title="展开赛道龙头对比"
                      >
                        {expanded[p.symbol] ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                      </button>
                      {i + 1}
                    </td>
                    <td className="px-2.5 py-3 font-semibold">
                      <a className="text-primary hover:underline" href={p.detail_url} title={p.options_sentiment?.label}>
                        {p.symbol}
                      </a>
                      {p.current_price > 0 && (
                        <span className="ml-1.5 font-mono text-xs text-muted-foreground">${p.current_price.toFixed(2)}</span>
                      )}
                    </td>
                    <td className="break-words px-2.5 py-3 text-xs text-foreground" title={[p.track, p.track_cn].filter(Boolean).join(" · ") || "--"}>
                      {p.track_cn || p.track || "--"}
                    </td>
                    <td className="px-2.5 py-3"><LiquidityBadge liq={p.liquidity} /></td>
                    <td className="px-2.5 py-3 text-right font-mono font-medium" title={p.soft_enhancement_nudge ? `含软增强微调 +${p.soft_enhancement_nudge.toFixed(3)}` : "未触发软增强微调"}>
                      {(p.ranking_score ?? p.priority_score).toFixed(3)}
                    </td>
                    <td className={cn("px-2.5 py-3 text-right font-mono", p.calibrated_probability >= 0.5 ? "text-success font-medium" : "text-warning")} title="净成本后跑赢该股自身基线的概率；<50%=预计跑不赢自身基线">
                      {pct(p.calibrated_probability)}
                    </td>
                    <td
                      className={cn(
                        "px-2.5 py-3 text-right font-mono",
                        p.relative_strength_20d >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400",
                      )}
                    >
                      {signedPct(p.relative_strength_20d)}
                    </td>
                    <td className="px-2.5 py-3 text-xs">
                      {p.hv?.hv_state ? (
                        <span
                          className={cn(
                            "inline-flex flex-wrap items-center gap-1",
                            (p.hv.hv_state || "").includes("扩张") ? "font-medium text-emerald-600 dark:text-emerald-400" : "text-muted-foreground",
                          )}
                          title={`HV(已实现波动)趋势${p.iv?.iv_state && p.iv.iv_state !== "IV数据积累中" ? ` · IV: ${p.iv.iv_state}` : "（IV 历史积累中）"}`}
                        >
                          {p.hv.hv_state}
                          {p.hv.hv_rise != null && <span className="font-mono">{signedPct(p.hv.hv_rise, 0)}</span>}
                        </span>
                      ) : (
                        <span className="text-muted-foreground/60">--</span>
                      )}
                    </td>
                    <td className="px-2.5 py-3 text-right font-mono text-muted-foreground">{p.regime_multiplier.toFixed(2)}</td>
                    <td className="px-2.5 py-3"><PickTags pick={p} /></td>
                  </tr>
                  {expanded[p.symbol] && (
                    <tr className="bg-muted/20">
                      <td colSpan={10} className="px-4 py-3">
                        <LeaderPanel state={leaders[p.symbol]} symbol={p.symbol} />
                      </td>
                    </tr>
                  )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        )}

        <p className="text-xs text-muted-foreground">
          {board?.method_note}
        </p>
        </>}
      </main>
    </div>
  );
}

function LeaderPanel({ state, symbol }: { state?: LeaderState; symbol: string }) {
  if (state === "loading" || state === undefined) {
    return (
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> 加载赛道龙头对比…
      </div>
    );
  }
  if ("error" in state) {
    return <div className="text-xs text-rose-600 dark:text-rose-400">龙头对比加载失败：{state.error}</div>;
  }
  if (!state.available || !state.members || state.members.length < 2) {
    return <div className="text-xs text-muted-foreground">{state.reason || "无同赛道可比公司"}</div>;
  }
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
        <span className="font-medium">赛道龙头对比</span>
        {state.track_cn && <span className="rounded bg-primary/10 px-1.5 py-0.5 text-primary">{state.track_cn}</span>}
        <span className="text-muted-foreground">龙头 = {state.leader}（按日均成交额）</span>
      </div>
      {state.verdict && (
        <div className="mb-2 rounded border-l-2 border-primary bg-primary/5 px-2 py-1.5 text-xs leading-5">{state.verdict}</div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 pr-3">代码</th>
              <th className="py-1 pr-3 text-right">流动性</th>
              <th className="py-1 pr-3">流动性档</th>
              <th className="py-1 pr-3 text-right">校准胜率</th>
              <th className="py-1 pr-3">回调状态</th>
            </tr>
          </thead>
          <tbody>
            {state.members.map((m) => (
              <tr key={m.symbol} className={cn(m.symbol === state.leader && "bg-emerald-500/5", m.symbol === symbol && "font-medium")}>
                <td className="py-1 pr-3 font-mono">
                  <a className="text-primary hover:underline" href={`/single-stock-overnight?symbol=${m.symbol}`} title="查看个股研判">{m.symbol}</a>
                  {m.symbol === state.leader && <span className="ml-1 rounded bg-emerald-500/15 px-1 text-[9px] text-emerald-700 dark:text-emerald-300">龙头</span>}
                  {m.is_query && m.symbol !== state.leader && <span className="ml-1 rounded bg-amber-500/15 px-1 text-[9px] text-amber-700 dark:text-amber-300">本标的</span>}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums">{fmtAdvShort(m.adv)}</td>
                <td className="py-1 pr-3 text-muted-foreground">{m.liquidity?.tier ?? "--"}</td>
                <td className="py-1 pr-3 text-right tabular-nums">{m.calibrated_win_rate != null ? `${(m.calibrated_win_rate * 100).toFixed(0)}%` : "--"}</td>
                <td className={cn("py-1 pr-3", m.in_pullback ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground")}>{m.pullback_state ?? "--"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {state.note && <p className="mt-1.5 text-[10px] text-muted-foreground">{state.note}</p>}
    </div>
  );
}

function ScStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border bg-card p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-lg font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function ScCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="text-xs text-muted-foreground">{title}</div>
      <div className="mt-1">{children}</div>
    </div>
  );
}

function BasketView() {
  const [budget, setBudget] = useState<number>(() => {
    const v = typeof localStorage !== "undefined" ? localStorage.getItem("priorityBoard.basketBudget") : null;
    return v ? Number(v) || 10000 : 10000;
  });
  const [n, setN] = useState<number>(8);
  const [weighting, setWeighting] = useState<"inverse_vol" | "equal">("inverse_vol");
  const [bk, setBk] = useState<PriorityBasketResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const run = (w: "inverse_vol" | "equal" = weighting) => {
    setLoading(true);
    if (typeof localStorage !== "undefined") localStorage.setItem("priorityBoard.basketBudget", String(budget));
    api.getPriorityBasket(budget, n, w).then(setBk).catch((e) => toast.error(e instanceof Error ? e.message : "生成失败")).finally(() => setLoading(false));
  };
  const setW = (w: "inverse_vol" | "equal") => {
    setWeighting(w);
    run(w);
  };
  useEffect(() => {
    run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fmtMoney = (v?: number | null) => (v == null || !Number.isFinite(v) ? "--" : `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`);

  return (
    <section className="space-y-4">
      <div className="rounded-lg border bg-card p-4">
        <div className="text-sm font-medium">等权篮子 · 分散降噪</div>
        <p className="mt-0.5 text-xs text-muted-foreground">
          edge 小、单票方差大 → <b>别赌单票</b>。按预算等权买 N 只（已过滤：校准胜率≥50% + 过流动性门槛）。这是参考清单，不是下单指令。
        </p>
        <div className="mt-3 flex flex-wrap items-end gap-3 text-sm">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">预算 (USD)</span>
            <input type="number" min={100} step={1000} value={budget} onChange={(e) => setBudget(Number(e.target.value) || 0)}
              className="w-32 rounded-md border bg-background px-2 py-1.5 font-mono" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">只数 N</span>
            <input type="number" min={2} max={20} value={n} onChange={(e) => setN(Math.max(2, Math.min(20, Number(e.target.value) || 8)))}
              className="w-20 rounded-md border bg-background px-2 py-1.5 font-mono" />
          </label>
          <div className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">定仓方式</span>
            <div className="inline-flex rounded-md border p-1 text-xs">
              <button type="button" onClick={() => setW("inverse_vol")}
                className={cn("rounded px-2.5 py-1", weighting === "inverse_vol" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted")}
                title="按 1/波动率 定仓：高波动票仓位更小，各只风险贡献≈相等(更稳)">波动率反比</button>
              <button type="button" onClick={() => setW("equal")}
                className={cn("rounded px-2.5 py-1", weighting === "equal" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted")}
                title="每只相同金额">等权</button>
            </div>
          </div>
          <button onClick={() => run()} disabled={loading}
            className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-sm text-primary hover:bg-primary/20 disabled:opacity-60">
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}生成篮子
          </button>
        </div>
      </div>

      {bk?.no_edge_today && (
        <div className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm text-warning">
          ⚠️ 今日无显著买点（合格候选不足）。验证期建议<b>空仓</b>——别为交易而交易。
        </div>
      )}

      {bk?.available && (
        <>
          <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <div className="rounded-lg border bg-card p-3"><div className="text-xs text-muted-foreground">篮子只数</div><div className="mt-1 text-lg font-semibold tabular-nums">{bk.n_filled} 只</div></div>
            <div className="rounded-lg border bg-card p-3"><div className="text-xs text-muted-foreground">每只权重</div><div className="mt-1 text-lg font-semibold tabular-nums">{bk.equal_weight_pct}%</div></div>
            <div className="rounded-lg border bg-card p-3"><div className="text-xs text-muted-foreground">预计投入</div><div className="mt-1 text-lg font-semibold tabular-nums">{fmtMoney(bk.deployed)}</div></div>
            <div className="rounded-lg border bg-card p-3"><div className="text-xs text-muted-foreground">剩余现金</div><div className="mt-1 text-lg font-semibold tabular-nums">{fmtMoney(bk.cash_left)}</div></div>
          </div>

          <section className="rounded-lg border bg-card">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-sm">
                <thead className="bg-muted/40 text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3 text-left font-medium">标的</th>
                    <th className="px-4 py-3 text-left font-medium">赛道</th>
                    <th className="px-4 py-3 text-right font-medium">现价</th>
                    <th className="px-4 py-3 text-right font-medium">校准胜率</th>
                    <th className="px-4 py-3 text-right font-medium">波动率(HV)</th>
                    <th className="px-4 py-3 text-right font-medium">权重</th>
                    <th className="px-4 py-3 text-right font-medium">目标金额</th>
                    <th className="px-4 py-3 text-right font-medium">股数</th>
                    <th className="px-4 py-3 text-right font-medium">预计成本</th>
                    <th className="px-4 py-3 text-right font-medium">止损建议(-8%)</th>
                  </tr>
                </thead>
                <tbody>
                  {bk.basket.map((it) => (
                    <tr key={it.symbol} className="border-t hover:bg-muted/30">
                      <td className="px-4 py-3 font-semibold">
                        <a className="text-primary hover:underline" href={it.detail_url || `/single-stock-overnight?symbol=${it.symbol}`}>{it.symbol}</a>
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">{it.track_cn || it.track || "--"}</td>
                      <td className="px-4 py-3 text-right font-mono">${it.current_price.toFixed(2)}</td>
                      <td className={cn("px-4 py-3 text-right font-mono", (it.calibrated_probability ?? 0) >= 0.5 ? "text-success" : "text-warning")}>{pct(it.calibrated_probability, 1)}</td>
                      <td className="px-4 py-3 text-right font-mono text-muted-foreground">{it.hv20 != null ? pct(it.hv20, 0) : "--"}</td>
                      <td className="px-4 py-3 text-right font-mono">{pct(it.weight, 1)}</td>
                      <td className="px-4 py-3 text-right font-mono">{fmtMoney(it.target_amount)}</td>
                      <td className="px-4 py-3 text-right font-mono font-semibold">{it.shares}</td>
                      <td className="px-4 py-3 text-right font-mono text-muted-foreground">{fmtMoney(it.est_cost)}</td>
                      <td className="px-4 py-3 text-right font-mono text-rose-600 dark:text-rose-400">${it.stop_suggest.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}

      {bk?.note && <p className="text-xs leading-relaxed text-muted-foreground">{bk.note}</p>}
    </section>
  );
}

function ScorecardView() {
  const [mode, setMode] = useState<"live" | "backfill">("live");
  const [evaluationVersion, setEvaluationVersion] = useState("legacy");
  const [parameterStatus, setParameterStatus] = useState<Awaited<ReturnType<typeof api.getPullbackParameterStatus>> | null>(null);
  const [sc, setSc] = useState<PredictionScorecard | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const fetchSc = (m: "live" | "backfill") => {
    setLoading(true);
    api.getPredictionScorecard(m, evaluationVersion).then(setSc).catch(() => setSc(null)).finally(() => setLoading(false));
    api.getPullbackParameterStatus().then(setParameterStatus).catch(() => setParameterStatus(null));
  };
  useEffect(() => {
    fetchSc(mode);
  }, [mode, evaluationVersion]);

  const runResolve = async () => {
    setBusy(true);
    try {
      await api.resolvePredictionsNow();
      fetchSc(mode);
      toast.success("已结算到期预测并刷新对账");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "结算失败");
    } finally {
      setBusy(false);
    }
  };
  const runLog = async () => {
    setBusy(true);
    try {
      await api.logPredictionsNow();
      toast.success("已记录今日全量候选预测");
      fetchSc(mode);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "记录失败");
    } finally {
      setBusy(false);
    }
  };
  const runBackfill = async () => {
    setBusy(true);
    try {
      await api.backfillPredictionsNow(30);
      setMode("backfill");
      toast.info("后台回放中（按交易日逐只 as-of 重算，约 1–3 分钟）…");
      // Poll the background job until done (or give up after ~5 min).
      for (let i = 0; i < 30; i++) {
        await new Promise((res) => setTimeout(res, 10_000));
        const st = await api.getBackfillStatus().catch(() => null);
        if (st?.status === "done") {
          fetchSc("backfill");
          toast.success(`历史回放完成（样本内，已结算 ${st.n_resolved ?? 0} 笔）`);
          break;
        }
        if (st?.status === "failed") {
          toast.error(`回放失败：${st.error ?? "未知错误"}`);
          break;
        }
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "回放失败");
    } finally {
      setBusy(false);
    }
  };

  const c = sc?.counts;
  const ci = sc?.net_excess_ci;
  const negativeEvidence = ci?.[1] != null && ci[1] < 0;
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <label htmlFor="prediction-evaluation">结算口径</label>
        <select id="prediction-evaluation" value={evaluationVersion} onChange={e => setEvaluationVersion(e.target.value)} className="rounded border bg-background px-2 py-1">
          <option value="legacy">历史口径 · 待审计</option>
          <option value="recorded_open_v3">新口径 · 记录后开盘</option>
        </select>
        {parameterStatus && <span className="text-xs text-muted-foreground">参数验证 · 每 {parameterStatus.interval_days} 天 · {parameterStatus.latest?.status === "completed" ? "已生成研究报告" : parameterStatus.latest?.status === "running" ? "运行中" : parameterStatus.latest?.status === "failed" ? "上次未完成" : "等待运行"} · {parameterStatus.active ? "已有验证版本" : "沿用当前参数"}</span>}
        <button type="button" className="text-xs text-primary underline" onClick={async () => {const result = await api.trainPullbackParameters().catch(() => null); toast.info(result?.status === "running" ? "参数验证已启动" : "参数任务暂未启动，稍后刷新状态");}}>运行参数验证</button>
      </div>
      {sc?.evaluation_note && <p className="text-xs text-muted-foreground">{sc.evaluation_note}</p>}
      <div className="flex flex-col gap-3 rounded-lg border bg-card p-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="mb-1 inline-flex items-center gap-2 text-xs font-medium text-primary">
            <ClipboardCheck className="h-3.5 w-3.5" /> 前向对账（全量候选，非只 TopN）
          </div>
          <p className="max-w-2xl text-sm text-muted-foreground">
            每天把所有跑取候选在出结果前落账，到期(N交易日)才结算，把"预测胜率"对到"实际有没有跑赢自身基线"。这是模型活的前向战绩，不是回测切片。
          </p>
          <div className="mt-3 inline-flex rounded-md border p-1 text-xs">
            <button
              type="button"
              onClick={() => setMode("live")}
              className={cn("rounded px-2.5 py-1", mode === "live" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted")}
              title="真实走步前向：每日批次落账，干净的样本外战绩"
            >
              实盘前向
            </button>
            <button
              type="button"
              onClick={() => setMode("backfill")}
              className={cn("rounded px-2.5 py-1", mode === "backfill" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted")}
              title="按模型回溯历史交易日的 as-of 预测（样本内参考，校准曲线见过这段）"
            >
              历史回放(样本内)
            </button>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <button onClick={runLog} disabled={busy} className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted disabled:opacity-60" title="把今天的全量候选记进预测账本（手动触发）">
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ClipboardCheck className="h-3.5 w-3.5" />}记录今日
          </button>
          <button onClick={runResolve} disabled={busy} className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted disabled:opacity-60" title="结算已到期的预测（手动触发）">
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}结算到期
          </button>
          <button onClick={runBackfill} disabled={busy} className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 px-2.5 py-1.5 text-xs text-primary hover:bg-primary/10 disabled:opacity-60" title="按模型回溯最近 30 个交易日，立刻看到样本内对账（无需等 10 天）">
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <History className="h-3.5 w-3.5" />}回溯历史
          </button>
        </div>
      </div>

      {mode === "backfill" && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
          ⚠️ <b>历史回放=样本内</b>：这些是按当前模型对过去交易日做的 as-of 预测，<b>校准曲线见过这段行情</b>，所以它检验的是"校准是否准 + 名次排序是否单调"，<b>不是干净的样本外前向 edge</b>。真实前向战绩请看「实盘前向」标签（每日批次积累，约 10 个交易日出首批）。
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <ScStat label="累计预测(全量)" value={String(c?.total ?? "--")} />
        <ScStat label="已结算" value={String(c?.resolved ?? "--")} />
        <ScStat label="待结算(未到期)" value={String(c?.pending ?? "--")} />
        <ScStat label="其中看板决策" value={String(c?.board_decisions ?? "--")} />
      </div>

      {loading && !sc ? (
        <div className="p-10 text-center text-muted-foreground"><Loader2 className="mx-auto h-5 w-5 animate-spin" /></div>
      ) : !sc?.available ? (
        <div className="rounded-lg border border-dashed bg-muted/20 p-6 text-center text-sm text-muted-foreground">
          {mode === "live" ? (
            <>
              实盘前向还没有已结算的预测。每日扫描自动记录全量候选；攒满一个 horizon(5–10个交易日)后才有第一批结算结果。
              <div className="mt-2 text-xs">不想等？点右上「<b>回溯历史</b>」按模型回放最近 30 个交易日，立刻看到<b>样本内</b>对账（含名次单调性）。</div>
            </>
          ) : (
            <>
              历史回放还没有数据。点右上「<b>回溯历史</b>」开始按模型回溯最近 30 个交易日（约 1–2 分钟）。
            </>
          )}
        </div>
      ) : (
        <>
          <div className={cn(
            "rounded-lg border p-3 text-sm",
            negativeEvidence ? "border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300" : sc.significant ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
              : "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300",
          )}>
            {negativeEvidence ? (
              <>当前计算的净超额区间低于零，表现偏弱；需结合数据审计及重叠持有验证复核。</>
            ) : sc.significant ? (
              <span className="inline-flex items-center gap-1.5"><CheckCircle2 className="h-4 w-4" />当前净超额区间为正，仍需核验数据与重叠持有。</span>
            ) : (
              <>样本 {sc.n_resolved} 笔 / {sc.trading_days} 个信号日：{ci?.[0] == null ? "日期不足，暂不报告有效区间。" : "当前区间跨零，尚无明确证据。"} 相邻持有窗口可能重叠，记录笔数不等于独立样本数。</>
            )}
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <ScCard title="预测 vs 实盘胜率">
              <div className="flex items-baseline gap-2">
                <span className="text-2xl font-semibold tabular-nums">{pct(sc.realized_win_rate, 1)}</span>
                <span className="text-xs text-muted-foreground">实盘 · 预测 {pct(sc.predicted_win_rate, 1)}</span>
              </div>
              <div className="mt-1 text-[11px] text-muted-foreground">实盘≈预测=校准准；差太多=漂移</div>
            </ScCard>
            <ScCard title="实盘净超额 vs 基线">
              <div className={cn("text-2xl font-semibold tabular-nums", (sc.mean_net_excess ?? 0) > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400")}>
                {signedPct(sc.mean_net_excess, 1)}
              </div>
              <div className="mt-1 text-[11px] text-muted-foreground">95% CI [{signedPct(ci?.[0] ?? null, 1)}, {signedPct(ci?.[1] ?? null, 1)}]（按交易日聚类）</div>
              <div className="mt-1 text-[11px] text-muted-foreground">重叠调整区间：{sc.overlap_adjusted_ci?.[0] != null ? `[${signedPct(sc.overlap_adjusted_ci[0], 1)}, ${signedPct(sc.overlap_adjusted_ci[1], 1)}]` : "信号日期不足"}；净收益 {signedPct(sc.mean_net_return, 1)}</div>
            </ScCard>
            <ScCard title="Brier 分 / 技巧分">
              <div className="text-2xl font-semibold tabular-nums">{sc.brier?.toFixed(3) ?? "--"}</div>
              <div className="mt-1 text-[11px] text-muted-foreground">技巧分 {sc.brier_skill_score != null ? sc.brier_skill_score.toFixed(3) : "--"}（&gt;0=比拍基准率强）</div>
            </ScCard>
          </div>

          {sc.beta_alpha && (
            <div className={cn(
              "rounded-lg border p-3 text-sm",
              sc.beta_alpha.significant ? "border-emerald-500/40 bg-emerald-500/10" : "border-sky-500/40 bg-sky-500/10",
            )}>
              <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
                <span className="font-medium">市场中性 alpha（剥离大盘 β）</span>
                <span>净 alpha 均值 <b className={cn((sc.beta_alpha.mean_net_alpha ?? 0) > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400")}>{signedPct(sc.beta_alpha.mean_net_alpha, 2)}</b></span>
                <span className="text-muted-foreground">alpha&gt;0 占比 {pct(sc.beta_alpha.win_rate, 1)}</span>
                <span className="text-muted-foreground">95% CI [{signedPct(sc.beta_alpha.ci?.[0] ?? null, 2)}, {signedPct(sc.beta_alpha.ci?.[1] ?? null, 2)}]</span>
                <span className="text-muted-foreground">n={sc.beta_alpha.n}</span>
                <span className={cn("rounded px-1.5 py-0.5 text-[11px]", sc.beta_alpha.significant ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300" : "bg-amber-500/15 text-amber-700 dark:text-amber-300")}>
                  {sc.beta_alpha.ci?.[1] != null && sc.beta_alpha.ci[1] < 0 ? "区间为负" : sc.beta_alpha.significant ? "区间为正" : "证据不足"}
                </span>
              </div>
              {sc.beta_alpha.note && <p className="mt-1 text-xs text-muted-foreground">{sc.beta_alpha.note}</p>}
            </div>
          )}

          {sc.rank_buckets && sc.rank_buckets.length > 0 && (
            <div className="rounded-lg border bg-card p-4">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-sm font-semibold">名次分桶 → 实际命中（单调性检验）</h2>
                {sc.rank_monotone != null && (
                  <span className={cn("rounded px-1.5 py-0.5 text-[11px]", sc.rank_monotone ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300" : "bg-amber-500/15 text-amber-700 dark:text-amber-300")}>
                    {sc.rank_monotone ? "靠前名次确实更准" : "名次单调性未成立"}
                  </span>
                )}
              </div>
              <p className="mt-0.5 text-xs text-muted-foreground">榜单 TopN 的命中率应高于尾部候选；不单调=排序没有把好的排到前面。需全量候选才测得出。</p>
              <table className="mt-3 w-full text-sm">
                <thead><tr className="border-b text-left text-xs text-muted-foreground">
                  <th className="py-1.5">名次桶</th><th className="py-1.5 text-right">样本</th><th className="py-1.5 text-right">预测胜率</th><th className="py-1.5 text-right">实际命中</th><th className="py-1.5 text-right">净超额</th>
                </tr></thead>
                <tbody>
                  {sc.rank_buckets.map((b) => (
                    <tr key={b.label} className="border-b last:border-0">
                      <td className="py-1.5">{b.label}</td>
                      <td className="py-1.5 text-right tabular-nums">{b.n}</td>
                      <td className="py-1.5 text-right tabular-nums">{pct(b.predicted_win_rate, 1)}</td>
                      <td className="py-1.5 text-right tabular-nums font-medium">{pct(b.realized_win_rate, 1)}</td>
                      <td className={cn("py-1.5 text-right tabular-nums", b.mean_net_excess > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400")}>{signedPct(b.mean_net_excess, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {sc.enhancement_buckets && sc.enhancement_buckets.length > 0 && (
            <div className="rounded-lg border bg-card p-4">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-sm font-semibold">软增强标签前向对账</h2>
                <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[11px] text-sky-700 dark:text-sky-300">
                  标签 vs 非标签
                </span>
              </div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                这里验证新增的“市场+流动性+RS前40%”和“强于行业ETF”是否真的提高后续兑现率。它们只是软增强证据，不直接替代主模型胜率。
              </p>
              <table className="mt-3 w-full text-sm">
                <thead>
                  <tr className="border-b text-left text-xs text-muted-foreground">
                    <th className="py-1.5">标签组</th>
                    <th className="py-1.5 text-right">样本</th>
                    <th className="py-1.5 text-right">预测胜率</th>
                    <th className="py-1.5 text-right">实际命中</th>
                    <th className="py-1.5 text-right">净超额</th>
                    <th className="py-1.5 text-right">Beta后Alpha</th>
                  </tr>
                </thead>
                <tbody>
                  {sc.enhancement_buckets.map((b) => (
                    <tr key={b.label} className="border-b last:border-0">
                      <td className="py-1.5">{b.label}</td>
                      <td className="py-1.5 text-right tabular-nums">{b.n}</td>
                      <td className="py-1.5 text-right tabular-nums">{pct(b.predicted_win_rate, 1)}</td>
                      <td className="py-1.5 text-right tabular-nums font-medium">{pct(b.realized_win_rate, 1)}</td>
                      <td className={cn("py-1.5 text-right tabular-nums", b.mean_net_excess > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400")}>
                        {signedPct(b.mean_net_excess, 1)}
                      </td>
                      <td className={cn("py-1.5 text-right tabular-nums", (b.mean_beta_alpha ?? 0) > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground")}>
                        {b.mean_beta_alpha == null ? "--" : signedPct(b.mean_beta_alpha, 1)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {sc.enhancement_note && <p className="mt-2 text-xs text-muted-foreground">{sc.enhancement_note}</p>}
            </div>
          )}

          {sc.calibration_audit?.warnings && sc.calibration_audit.warnings.length > 0 && (
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
              <div className="font-medium">校准曲线来源审计</div>
              <ul className="mt-1 list-disc space-y-1 pl-4">
                {sc.calibration_audit.warnings.slice(0, 4).map((w) => <li key={w}>{w}</li>)}
              </ul>
              {sc.calibration_audit.note && <p className="mt-1 text-muted-foreground">{sc.calibration_audit.note}</p>}
            </div>
          )}

          {sc.reliability_buckets && sc.reliability_buckets.length > 0 && (
            <div className="rounded-lg border bg-card p-4">
              <h2 className="text-sm font-semibold">可靠性（预测分桶 → 实际命中）</h2>
              <p className="mt-0.5 text-xs text-muted-foreground">"预测55% 的那批，实际是不是命中~55%"。越接近对角线越准。</p>
              <table className="mt-3 w-full text-sm">
                <thead><tr className="border-b text-left text-xs text-muted-foreground">
                  <th className="py-1.5">预测区间</th><th className="py-1.5 text-right">样本</th><th className="py-1.5 text-right">平均预测</th><th className="py-1.5 text-right">实际命中</th>
                </tr></thead>
                <tbody>
                  {sc.reliability_buckets.map((b) => (
                    <tr key={b.label} className="border-b last:border-0">
                      <td className="py-1.5">{b.label}</td>
                      <td className="py-1.5 text-right tabular-nums">{b.n}</td>
                      <td className="py-1.5 text-right tabular-nums">{pct(b.predicted, 1)}</td>
                      <td className={cn("py-1.5 text-right tabular-nums font-medium", Math.abs(b.realized - b.predicted) <= 0.05 ? "text-emerald-600 dark:text-emerald-400" : "text-amber-600 dark:text-amber-400")}>{pct(b.realized, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {sc.open_positions && (
            <div className="rounded-lg border bg-muted/20 p-3 text-sm">
              <span className="font-medium">未到期持仓盯市：</span>{" "}
              {sc.open_positions.count} 笔 · 截至今日平均 {signedPct(sc.open_positions.avg_return_so_far ?? null, 1)}
              <span className="ml-1 text-xs text-muted-foreground">（{sc.open_positions.note}）</span>
            </div>
          )}
        </>
      )}

      {sc?.method_note && <p className="text-xs leading-relaxed text-muted-foreground">{sc.method_note}</p>}
    </section>
  );
}
