import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Loader2, Play, Plus, RefreshCw, Star, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api, type ResearchSignalHubJob, type WatchlistMetricRow, type WatchlistResponse } from "@/lib/api";
import { cn } from "@/lib/utils";

function pct(v?: number | null, digits = 1) {
  if (v === undefined || v === null || !Number.isFinite(Number(v))) return "--";
  const n = Number(v);
  return `${n >= 0 ? "+" : ""}${(n * 100).toFixed(digits)}%`;
}

function plainPct(v?: number | null, digits = 1) {
  if (v === undefined || v === null || !Number.isFinite(Number(v))) return "--";
  return `${(Number(v) * 100).toFixed(digits)}%`;
}

function money(v?: number | null) {
  if (v === undefined || v === null || !Number.isFinite(Number(v))) return "--";
  const n = Number(v);
  if (Math.abs(n) >= 1e12) return `$${(n / 1e12).toFixed(1)}T`;
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(0)}M`;
  return `$${n.toFixed(2)}`;
}

function scoreTone(v?: number | null) {
  const n = Number(v ?? NaN);
  if (!Number.isFinite(n)) return "text-muted-foreground";
  if (n >= 0.55) return "text-emerald-600 dark:text-emerald-400";
  if (n >= 0.5) return "text-amber-600 dark:text-amber-400";
  return "text-muted-foreground";
}

function statusLabel(value?: string | null) {
  if (!value || value === "NONE") return "无信号";
  const map: Record<string, string> = {
    WATCH: "回调承接",
    STRONG_WATCH: "强承接",
    WAITING_CONFIRMATION: "等待确认",
    WEAK_CONFIRMED: "弱确认",
    CONFIRMED: "已确认",
    STRONG_CONFIRMED: "强确认",
    INVALIDATED: "已失效",
  };
  return map[value] ?? value;
}

function compactNumber(v?: number | null) {
  if (v === undefined || v === null || !Number.isFinite(Number(v))) return "--";
  const n = Number(v);
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(0)}M`;
  return n.toFixed(0);
}

function EarningsTag({ row }: { row: WatchlistMetricRow }) {
  const earnings = row.earnings;
  if (!earnings?.available || !earnings.next_date) return <span className="text-muted-foreground/50">--</span>;
  const days = typeof earnings.days_until === "number" ? earnings.days_until : null;
  const near = days !== null && days >= 0 && days <= 10;
  const tone = earnings.estimate_tone || "neutral";
  return (
    <div className="space-y-1 text-xs">
      <span
        className={cn(
          "inline-flex rounded border px-1.5 py-0.5 font-medium",
          tone === "positive"
            ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
            : tone === "warning" || near
              ? "border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-300"
              : "border-slate-500/25 bg-slate-500/10 text-slate-600 dark:text-slate-300",
        )}
        title={earnings.estimate_basis || "财报预期未形成明确方向"}
      >
        {near ? "临近财报" : "财报"}{days !== null ? `${days}天` : ""}
      </span>
      <div className="text-muted-foreground">{earnings.next_date}</div>
      <div className="text-muted-foreground">EPS {earnings.eps_estimated ?? "--"}</div>
      {earnings.revenue_estimated != null && <div className="text-muted-foreground">营收 {compactNumber(earnings.revenue_estimated)}</div>}
    </div>
  );
}

function DistributionRiskTag({ row }: { row: WatchlistMetricRow }) {
  const risk = row.distribution_risk;
  if (!risk?.available || !risk.triggered) return null;
  const high = risk.level === "high";
  const title = [
    ...(risk.reasons ?? []),
    risk.volume_ratio ? `量比 ${risk.volume_ratio}x` : "",
    risk.close_location != null ? `收盘位置 ${(risk.close_location * 100).toFixed(0)}%` : "",
  ].filter(Boolean).join("；");
  return (
    <span
      className={cn(
        "mt-1 inline-flex rounded border px-1.5 py-0.5 text-[10px] font-semibold",
        high
          ? "border-red-500/40 bg-red-500/15 text-red-700 dark:text-red-300"
          : "border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-300",
      )}
      title={title || risk.method}
    >
      {risk.label || "高位派发风险"}
    </span>
  );
}

export function Watchlist() {
  const [data, setData] = useState<WatchlistResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [symbol, setSymbol] = useState("");
  const [note, setNote] = useState("");
  const [batch, setBatch] = useState("");
  const [job, setJob] = useState<ResearchSignalHubJob | null>(null);
  const pollRef = useRef<number | null>(null);

  const load = (silent = false) => {
    if (!silent) setLoading(true);
    api
      .getWatchlist(true, true)
      .then(setData)
      .catch((err) => toast.error(err instanceof Error ? err.message : "自选池加载失败"))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    return () => {
      if (pollRef.current) window.clearTimeout(pollRef.current);
    };
  }, []);

  useEffect(() => {
    if (!data?.refreshing) return;
    const timer = window.setTimeout(() => load(true), 5000);
    return () => window.clearTimeout(timer);
  }, [data]);

  const rows = useMemo(() => {
    const items = [...(data?.items ?? [])];
    return items.sort((a, b) => {
      const av = Number(a.alpha_win_rate ?? a.research_probability ?? 0);
      const bv = Number(b.alpha_win_rate ?? b.research_probability ?? 0);
      if (bv !== av) return bv - av;
      return Number(b.relative_to_qqq_60d ?? -999) - Number(a.relative_to_qqq_60d ?? -999);
    });
  }, [data]);

  const addOne = async () => {
    const safe = symbol.trim().toUpperCase();
    if (!safe) return;
    try {
      await api.addWatchlistItem({ symbol: safe, note, enabled: true });
      setSymbol("");
      setNote("");
      toast.success(`${safe} 已加入自选池`);
      load(true);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "添加失败");
    }
  };

  const addBatch = async () => {
    const symbols = batch.split(/[\s,;，；]+/).map((x) => x.trim().toUpperCase()).filter(Boolean);
    if (!symbols.length) return;
    try {
      const res = await api.addWatchlistItems({ symbols, enabled: true });
      setBatch("");
      toast.success(`已加入 ${res.count} 个标的`);
      load(true);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "批量导入失败");
    }
  };

  const remove = async (row: WatchlistMetricRow) => {
    try {
      await api.deleteWatchlistItem(row.symbol);
      toast.success(`${row.symbol} 已移出自选池`);
      load(true);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "删除失败");
    }
  };

  const toggle = async (row: WatchlistMetricRow) => {
    try {
      await api.setWatchlistEnabled(row.symbol, !row.enabled);
      load(true);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "更新失败");
    }
  };

  const pollJob = (jobId: string) => {
    if (pollRef.current) window.clearTimeout(pollRef.current);
    pollRef.current = window.setTimeout(async () => {
      try {
        const next = await api.getResearchSignalHubRun(jobId);
        setJob(next);
        if (next.status === "completed") {
          toast.success("自选池三层分析完成");
          load(true);
          return;
        }
        if (next.status === "failed") {
          toast.error(next.message || "自选池三层分析失败");
          return;
        }
        pollJob(jobId);
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "任务状态读取失败");
      }
    }, 1500);
  };

  const run = async () => {
    try {
      const res = await api.runResearchSignalHub({
        universe: "watchlist",
        refresh_evidence: false,
        top: 100,
        enable_llm_review: true,
        llm_review_top: 10,
        enable_event_llm_review: true,
      });
      setJob(res);
      toast.info("自选池三层分析已启动");
      pollJob(res.job_id);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "启动失败");
    }
  };

  return (
    <div className="h-full overflow-y-auto bg-background">
      <main className="mx-auto max-w-7xl space-y-5 px-6 py-6">
        <section className="flex flex-col gap-3 border-b pb-4 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="mb-1 inline-flex items-center gap-2 text-xs font-medium text-amber-600 dark:text-amber-400">
              <Star className="h-3.5 w-3.5 fill-amber-500" />
              Watchlist
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">自选池</h1>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
              手动维护你自己的研究股票池。模型运行时来源池固定显示为“自选池”，Alpha 基准按纳斯达克/QQQ 计算。
            </p>
            {data?.refreshing && (
              <p className="mt-1 text-xs text-muted-foreground">正在后台补齐 {data.pending_count} 只标的的行情与研究指标</p>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => load()}
              disabled={loading}
              className="inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-60"
            >
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              刷新
            </button>
            <button
              type="button"
              onClick={run}
              disabled={Boolean(job && ["queued", "running"].includes(job.status))}
              className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-sm text-primary hover:bg-primary/20 disabled:opacity-60"
            >
              {job && ["queued", "running"].includes(job.status) ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              运行自选池三层分析
            </button>
          </div>
        </section>

        {job && (
          <section className="rounded-lg border bg-card p-3 text-sm">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="font-medium">{job.message || "任务运行中"}</div>
              <div className="font-mono text-xs text-muted-foreground">{Math.round(Number(job.progress || 0) * 100)}%</div>
            </div>
            <div className="mt-2 h-2 overflow-hidden rounded bg-muted">
              <div className="h-full bg-primary transition-all" style={{ width: `${Math.round(Number(job.progress || 0) * 100)}%` }} />
            </div>
          </section>
        )}

        <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
          <div className="rounded-lg border bg-card p-4">
            <div className="text-sm font-medium">添加标的</div>
            <div className="mt-3 grid gap-2 sm:grid-cols-[180px_1fr_auto]">
              <input
                value={symbol}
                onChange={(event) => setSymbol(event.target.value.toUpperCase())}
                placeholder="例如 NVDA"
                className="h-10 rounded-md border bg-background px-3 text-sm outline-none focus:border-primary"
              />
              <input
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder="备注：为什么关注它"
                className="h-10 rounded-md border bg-background px-3 text-sm outline-none focus:border-primary"
              />
              <button type="button" onClick={addOne} className="inline-flex h-10 items-center justify-center gap-1.5 rounded-md bg-primary px-3 text-sm font-medium text-primary-foreground">
                <Plus className="h-4 w-4" />
                添加
              </button>
            </div>
          </div>

          <div className="rounded-lg border bg-card p-4">
            <div className="text-sm font-medium">批量导入</div>
            <textarea
              value={batch}
              onChange={(event) => setBatch(event.target.value)}
              placeholder="一行一个或英文逗号分隔"
              className="mt-3 min-h-20 w-full rounded-md border bg-background px-3 py-2 text-sm outline-none focus:border-primary"
            />
            <button type="button" onClick={addBatch} className="mt-2 inline-flex h-9 items-center gap-1.5 rounded-md border px-3 text-sm hover:bg-muted">
              <Plus className="h-4 w-4" />
              批量加入
            </button>
          </div>
        </section>

        <section className="rounded-lg border bg-card">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
            <div>
              <div className="text-sm font-medium">自选列表</div>
              <div className="text-xs text-muted-foreground">
                {data?.count ?? 0} 个标的 · Alpha 基准 {data?.benchmark_label || "纳斯达克 / QQQ"} · 来源池 自选池
              </div>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-3 text-left font-medium">标的</th>
                  <th className="px-3 py-3 text-right font-medium">现价</th>
                  <th className="px-3 py-3 text-right font-medium">当日</th>
                  <th className="px-3 py-3 text-left font-medium">市值 / PE</th>
                  <th className="px-3 py-3 text-left font-medium">行业</th>
                  <th className="px-3 py-3 text-right font-medium">20日</th>
                  <th className="px-3 py-3 text-right font-medium">60日Alpha</th>
                  <th className="px-3 py-3 text-left font-medium">回调/启动</th>
                  <th className="px-3 py-3 text-left font-medium">GEX</th>
                  <th className="px-3 py-3 text-left font-medium">财报</th>
                  <th className="px-3 py-3 text-right font-medium">Alpha胜率</th>
                  <th className="px-3 py-3 text-left font-medium">备注</th>
                  <th className="px-3 py-3 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {loading && !data && (
                  <tr>
                    <td colSpan={13} className="px-4 py-12 text-center text-muted-foreground">
                      <Loader2 className="mx-auto h-5 w-5 animate-spin" />
                    </td>
                  </tr>
                )}
                {!loading && rows.length === 0 && (
                  <tr>
                    <td colSpan={13} className="px-4 py-12 text-center text-muted-foreground">
                      暂无自选标的。可以从这里添加，也可以在个股研判页点击星标加入。
                    </td>
                  </tr>
                )}
                {rows.map((row) => (
                  <tr key={row.symbol} className={cn("border-t hover:bg-muted/30", row.enabled === false && "opacity-45")}>
                    <td className="px-3 py-3">
                      <Link to={`/single-stock-overnight?symbol=${encodeURIComponent(row.symbol)}`} className="font-semibold text-primary hover:underline">
                        {row.symbol}
                      </Link>
                      <div className="mt-0.5 max-w-[180px] truncate text-xs text-muted-foreground">{row.name || "--"}</div>
                      <span className="mt-1 inline-flex rounded border border-indigo-500/25 bg-indigo-500/10 px-1.5 py-0.5 text-[10px] text-indigo-700 dark:text-indigo-300">
                        自选池
                      </span>
                      <div>
                        <DistributionRiskTag row={row} />
                      </div>
                    </td>
                    <td className="px-3 py-3 text-right font-mono">{money(row.current_price)}</td>
                    <td className={cn("px-3 py-3 text-right font-mono", Number(row.day_change_pct) >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400")}>
                      {pct(row.day_change_pct)}
                    </td>
                    <td className="px-3 py-3 text-xs">
                      <div>{money(row.market_cap)}</div>
                      <div className="text-muted-foreground">PE {row.pe != null ? Number(row.pe).toFixed(1) : "--"}</div>
                    </td>
                    <td className="max-w-[180px] px-3 py-3 text-xs">
                      <div className="truncate">{row.sector || "--"}</div>
                      <div className="truncate text-muted-foreground">{row.industry || "--"}</div>
                    </td>
                    <td className={cn("px-3 py-3 text-right font-mono", Number(row.return_20d) >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400")}>
                      {pct(row.return_20d)}
                    </td>
                    <td className={cn("px-3 py-3 text-right font-mono", Number(row.relative_to_qqq_60d) >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400")}>
                      {pct(row.relative_to_qqq_60d)}
                    </td>
                    <td className="px-3 py-3 text-xs">
                      <div>{statusLabel(row.pullback_rejection_status)}</div>
                      <div className="text-muted-foreground">{statusLabel(row.pullback_confirmation_status)}</div>
                    </td>
                    <td className="px-3 py-3 text-xs">
                      <div>{row.gex_level || "--"}</div>
                      <div className="text-muted-foreground">{row.gex_regime || "--"}</div>
                    </td>
                    <td className="px-3 py-3"><EarningsTag row={row} /></td>
                    <td className={cn("px-3 py-3 text-right font-mono font-medium", scoreTone(row.alpha_win_rate))}>
                      {plainPct(row.alpha_win_rate)}
                    </td>
                    <td className="max-w-[220px] px-3 py-3 text-xs text-muted-foreground">{row.note || "--"}</td>
                    <td className="px-3 py-3 text-right">
                      <div className="flex justify-end gap-1">
                        <button type="button" onClick={() => toggle(row)} className="rounded border px-2 py-1 text-xs hover:bg-muted">
                          {row.enabled === false ? "启用" : "停用"}
                        </button>
                        <button type="button" onClick={() => remove(row)} className="rounded border px-2 py-1 text-xs text-rose-600 hover:bg-rose-500/10">
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </main>
    </div>
  );
}
