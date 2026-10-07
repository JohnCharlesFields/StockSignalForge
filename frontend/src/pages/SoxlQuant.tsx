import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, BarChart3, CircleStop, Play, RefreshCw, ShieldCheck, Wifi } from "lucide-react";
import { toast } from "sonner";
import { api, type SoxlBacktestJob, type SoxlBacktestResult, type SoxlLiveStatus } from "@/lib/api";
import { cn } from "@/lib/utils";

function money(value?: number | null) {
  if (value == null || !Number.isFinite(Number(value))) return "--";
  return `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function pct(value?: number | null) {
  if (value == null || !Number.isFinite(Number(value))) return "--";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function metric(value?: number | null, digits = 2) {
  if (value == null || !Number.isFinite(Number(value))) return "--";
  return Number(value).toFixed(digits);
}

function tone(value?: number | null) {
  const n = Number(value ?? NaN);
  if (!Number.isFinite(n)) return "text-muted-foreground";
  return n >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400";
}

function statusText(value?: string) {
  const map: Record<string, string> = {
    stopped: "已停止",
    connecting: "连接中",
    running: "实时模拟中",
    stopping: "停止中",
    error: "连接错误",
    completed: "回测完成",
    idle: "等待运行",
  };
  return map[value || ""] || value || "未知";
}

function StatusPill({ value, positive = false }: { value?: string; positive?: boolean }) {
  const live = value === "running" || value === "completed" || positive;
  const bad = value === "error";
  return (
    <span className={cn(
      "inline-flex items-center gap-1 rounded-full border px-2 py-1 text-xs font-medium",
      live && "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
      bad && "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300",
      !live && !bad && "border-border bg-muted/40 text-muted-foreground",
    )}>
      <span className={cn("h-1.5 w-1.5 rounded-full", live ? "bg-emerald-500" : bad ? "bg-red-500" : "bg-slate-400")} />
      {statusText(value)}
    </span>
  );
}

function MetricCard({ label, value, hint, valueClass }: { label: string; value: string; hint?: string; valueClass?: string }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-xl font-semibold tabular-nums", valueClass)}>{value}</div>
      {hint && <div className="mt-1 text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

function EquitySparkline({ points }: { points?: { equity: number }[] }) {
  const values = (points || []).map((x) => Number(x.equity)).filter(Number.isFinite);
  if (values.length < 2) return <div className="flex h-32 items-center justify-center text-xs text-muted-foreground">暂无权益曲线</div>;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const path = values.map((value, index) => `${(index / (values.length - 1)) * 100},${100 - ((value - min) / range) * 88 - 6}`).join(" ");
  const positive = values[values.length - 1] >= values[0];
  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-32 w-full overflow-visible">
      <line x1="0" y1="94" x2="100" y2="94" stroke="currentColor" className="text-border" strokeWidth="0.6" />
      <polyline fill="none" stroke="currentColor" className={positive ? "text-emerald-500" : "text-red-500"} strokeWidth="1.7" points={path} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function PerformanceBlock({ label, data }: { label: string; data?: SoxlBacktestResult["test"] }) {
  if (!data) return null;
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="font-medium">{label}</h3>
        <span className={cn("text-sm font-semibold", tone(data.net_profit))}>{money(data.net_profit)}</span>
      </div>
      <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div><div className="text-xs text-muted-foreground">期末权益</div><div className="mt-1 font-medium">{money(data.final_equity)}</div></div>
        <div><div className="text-xs text-muted-foreground">收益率</div><div className={cn("mt-1 font-medium", tone(data.total_return))}>{pct(data.total_return)}</div></div>
        <div><div className="text-xs text-muted-foreground">最大回撤</div><div className="mt-1 font-medium text-red-600 dark:text-red-400">{pct(data.max_drawdown)}</div></div>
        <div><div className="text-xs text-muted-foreground">交易 / 胜率</div><div className="mt-1 font-medium">{data.trade_count} / {pct(data.win_rate)}</div></div>
        <div><div className="text-xs text-muted-foreground">Sharpe</div><div className="mt-1 font-medium">{metric(data.sharpe)}</div></div>
        <div><div className="text-xs text-muted-foreground">Profit Factor</div><div className="mt-1 font-medium">{metric(data.profit_factor)}</div></div>
        <div><div className="text-xs text-muted-foreground">多头交易</div><div className="mt-1 font-medium">{data.long_trades} · {pct(data.side_stats?.LONG?.win_rate)}</div></div>
        <div><div className="text-xs text-muted-foreground">空头交易</div><div className="mt-1 font-medium">{data.short_trades} · {pct(data.side_stats?.SHORT?.win_rate)}</div></div>
      </div>
    </div>
  );
}

export function SoxlQuant() {
  const [status, setStatus] = useState<SoxlLiveStatus | null>(null);
  const [dataset, setDataset] = useState("Databento");
  const [job, setJob] = useState<SoxlBacktestJob | null>(null);
  const [result, setResult] = useState<SoxlBacktestResult | null>(null);
  const [capital, setCapital] = useState("2000");
  const [backtestSource, setBacktestSource] = useState<"databento" | "yfinance_proxy">("databento");
  const [loading, setLoading] = useState(false);
  const pollRef = useRef<number | null>(null);

  const load = async () => {
    try {
      const [s, latest] = await Promise.all([api.getSoxlQuantStatus(), api.getSoxlQuantLatest(false, 100)]);
      setStatus(s.live);
      setDataset(`${s.dataset || "Databento"}${s.schema ? ` / ${s.schema}` : ""}`);
      setJob(s.backtest_job);
      setResult(latest.result || s.latest_backtest || null);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "SOXL 模拟状态读取失败");
    }
  };

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      window.clearInterval(timer);
      if (pollRef.current) window.clearTimeout(pollRef.current);
    };
  }, []);

  const runBacktest = async () => {
    setLoading(true);
    try {
      const started = await api.startSoxlQuantBacktest(Number(capital) || 2000, false, backtestSource);
      setJob(started);
      const poll = async () => {
        const next = await api.getSoxlQuantBacktestStatus();
        setJob(next);
        if (next.result) setResult(next.result);
        if (next.status === "running") pollRef.current = window.setTimeout(() => void poll(), 2000);
        else setLoading(false);
      };
      await poll();
    } catch (err) {
      setLoading(false);
      toast.error(err instanceof Error ? err.message : "SOXL 回测启动失败");
    }
  };

  const startLive = async () => {
    try {
      const next = await api.startSoxlQuantLive();
      setStatus(next);
      if (next.status === "error") toast.error(next.message || "实时行情连接失败");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "实时模拟启动失败");
    }
  };

  const stopLive = async () => {
    try {
      setStatus(await api.stopSoxlQuantLive());
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "实时模拟停止失败");
    }
  };

  const quote = status?.last_bar;
  const livePnl = Number(status?.realized_pnl ?? 0);
  const test = result?.test;
  const benchmark = result?.test_always_long_benchmark;
  const excess = result?.test_excess_vs_always_long;
  const recentBars = useMemo(() => (status?.recent_bars || []).slice(-60), [status?.recent_bars]);

  return (
    <div className="mx-auto w-full max-w-[1500px] space-y-5 p-4 sm:p-6">
      <header className="flex flex-col gap-4 border-b border-border pb-5 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-primary"><Activity className="h-4 w-4" /> Local Paper Quant</div>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight">SOXL 实时行情量化模拟</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">仅使用 SOXL，允许多头与空头；数据进入模拟账户，不连接券商、不提交真实订单。实时路径使用 Databento，历史回测与纸面账户共用同一套信号逻辑。</p>
        </div>
        <div className="flex flex-wrap items-center gap-2"><StatusPill value={status?.status} /><span className="rounded-full border border-border px-2 py-1 text-xs text-muted-foreground">SOXL only</span><span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-xs text-amber-700 dark:text-amber-300">Paper only</span></div>
      </header>

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="模拟权益" value={money(status?.equity)} hint={`现金 ${money(status?.cash)}`} />
        <MetricCard label="已实现盈亏" value={money(livePnl)} hint={`${status?.trade_count ?? 0} 笔已结算交易`} valueClass={tone(livePnl)} />
        <MetricCard label="最新中间价" value={money(quote?.mid)} hint={quote?.timestamp || "等待行情"} />
        <MetricCard label="当前信号" value={status?.signal || "FLAT"} hint={`分数 ${metric(status?.signal_score, 4)} · 待执行 ${status?.pending_action || "无"}`} valueClass={status?.signal === "LONG" ? "text-emerald-600 dark:text-emerald-400" : status?.signal === "SHORT" ? "text-red-600 dark:text-red-400" : undefined} />
      </section>

      <section className="grid gap-5 xl:grid-cols-[1.35fr_0.65fr]">
        <div className="rounded-lg border border-border bg-card p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div><h2 className="font-semibold">实时模拟账户</h2><p className="mt-1 text-xs text-muted-foreground">每条完整 BBO 分钟数据推进一次纸面持仓</p></div>
            <div className="flex gap-2"><button type="button" onClick={() => void load()} className="inline-flex h-9 items-center gap-2 rounded-md border border-border px-3 text-sm hover:bg-muted"><RefreshCw className="h-4 w-4" />刷新</button><button type="button" onClick={() => void startLive()} disabled={status?.status === "running" || status?.status === "connecting"} className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-medium text-primary-foreground disabled:opacity-50"><Wifi className="h-4 w-4" />启动实时模拟</button><button type="button" onClick={() => void stopLive()} disabled={status?.status !== "running" && status?.status !== "connecting"} className="inline-flex h-9 items-center gap-2 rounded-md border border-red-500/30 px-3 text-sm text-red-600 hover:bg-red-500/10 disabled:opacity-50"><CircleStop className="h-4 w-4" />停止</button></div>
          </div>
          <div className="mt-4 rounded-md border border-border bg-muted/20 p-3 text-sm"><div className="flex items-start gap-2"><ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" /><span>{status?.message || "尚未启动实时模拟"}</span></div>{status?.error && <div className="mt-2 break-words text-xs text-red-600 dark:text-red-400">{status.error}</div>}</div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2"><div className="rounded-md border border-border p-3"><div className="text-xs text-muted-foreground">最后报价</div><div className="mt-2 grid grid-cols-3 gap-2 text-sm"><div>Bid<br /><b>{money(quote?.bid)}</b></div><div>Mid<br /><b>{money(quote?.mid)}</b></div><div>Ask<br /><b>{money(quote?.ask)}</b></div></div><div className="mt-2 text-xs text-muted-foreground">点差 {quote?.spread_bps == null ? "--" : `${metric(quote.spread_bps)} bps`}</div></div><div className="rounded-md border border-border p-3"><div className="text-xs text-muted-foreground">当前持仓</div>{status?.position ? <div className="mt-2 space-y-1 text-sm"><div className={cn("font-semibold", status.position.side > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400")}>{status.position.side > 0 ? "多头" : "空头"} {status.position.qty} 股</div><div>入场 {money(status.position.entry_price)}</div><div>止损 {money(status.position.stop_price)} · 目标 {money(status.position.target_price)}</div></div> : <div className="mt-2 text-sm text-muted-foreground">当前空仓</div>}</div></div>
          <div className="mt-4"><div className="mb-2 flex items-center justify-between text-xs text-muted-foreground"><span>最近 BBO 中间价</span><span>{recentBars.length} 条</span></div><div className="rounded-md border border-border bg-background/40 p-2"><EquitySparkline points={recentBars.map((bar) => ({ equity: bar.mid }))} /></div></div>
        </div>

        <div className="rounded-lg border border-border bg-card p-5"><h2 className="font-semibold">运行约束</h2><div className="mt-4 space-y-3 text-sm"><div className="flex justify-between gap-3 border-b border-border pb-2"><span className="text-muted-foreground">允许标的</span><b>SOXL</b></div><div className="flex justify-between gap-3 border-b border-border pb-2"><span className="text-muted-foreground">允许方向</span><b>LONG / SHORT</b></div><div className="flex justify-between gap-3 border-b border-border pb-2"><span className="text-muted-foreground">数据源</span><b>{dataset}</b></div><div className="flex justify-between gap-3 border-b border-border pb-2"><span className="text-muted-foreground">交易执行</span><b className="text-amber-600 dark:text-amber-300">已禁用</b></div><div className="flex justify-between gap-3"><span className="text-muted-foreground">初始资金</span><b>{money(status?.initial_capital)}</b></div></div><p className="mt-5 text-xs leading-5 text-muted-foreground">空头结果是假设可借券、可按报价成交的研究模拟；实际账户还要受到融券可得性、保证金、SSR 和隔夜成交规则限制。</p></div>
      </section>

      <section className="rounded-lg border border-border bg-card p-5"><div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between"><div><h2 className="font-semibold">历史分段回测</h2><p className="mt-1 text-xs text-muted-foreground">先在开发区间选参数，再用未参与选参的测试区间报告结果。</p></div><div className="flex flex-wrap items-end gap-2"><label className="text-xs text-muted-foreground">数据路径<select value={backtestSource} onChange={(e) => setBacktestSource(e.target.value as "databento" | "yfinance_proxy")} className="mt-1 block h-9 rounded-md border border-border bg-background px-2 text-sm text-foreground"><option value="databento">Databento 隔夜 BBO</option><option value="yfinance_proxy">Yahoo 5分钟常规时段代理</option></select></label><label className="text-xs text-muted-foreground">初始资金<input value={capital} onChange={(e) => setCapital(e.target.value)} inputMode="decimal" className="mt-1 block h-9 w-28 rounded-md border border-border bg-background px-2 text-sm text-foreground" /></label><button type="button" onClick={() => void runBacktest()} disabled={loading || job?.status === "running"} className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-medium text-primary-foreground disabled:opacity-50"><Play className="h-4 w-4" />{loading ? "回测中" : "运行回测"}</button></div></div>{job && <div className="mt-4 rounded-md border border-border bg-muted/20 p-3"><div className="flex items-center justify-between text-sm"><span>{job.message}</span><span>{job.progress}%</span></div><div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary transition-all" style={{ width: `${Math.max(0, Math.min(100, job.progress || 0))}%` }} /></div></div>}{result ? <div className="mt-5 space-y-4"><div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><MetricCard label="测试期末权益" value={money(test?.final_equity)} hint={`净收益 ${money(test?.net_profit)}`} valueClass={tone(test?.net_profit)} /><MetricCard label="测试收益率" value={pct(test?.total_return)} hint={`对比始终做多 ${pct(benchmark?.total_return)}`} valueClass={tone(test?.total_return)} /><MetricCard label="跑赢基准" value={pct(excess)} hint="测试区间策略收益 - 始终做多收益" valueClass={tone(excess)} /><MetricCard label="测试胜率" value={pct(test?.win_rate)} hint={`${test?.trade_count ?? 0} 笔交易 · Sharpe ${metric(test?.sharpe)}`} /></div><div className="grid gap-4 xl:grid-cols-2"><PerformanceBlock label="训练区间" data={result.train} /><PerformanceBlock label="验证区间" data={result.validation} /><PerformanceBlock label="测试区间（主要结论）" data={result.test} /><PerformanceBlock label="全区间复盘" data={result.all} /></div><div className="rounded-lg border border-border p-4"><div className="mb-3 flex items-center gap-2 font-medium"><BarChart3 className="h-4 w-4" />测试区间权益曲线</div><EquitySparkline points={test?.equity_curve} /><div className="mt-2 text-xs text-muted-foreground">{test?.start || "--"} 至 {test?.end || "--"} · 数据源 {result.source?.source || "--"}</div></div><div className="rounded-lg border border-border p-4"><h3 className="font-medium">最近测试交易</h3><div className="mt-3 overflow-x-auto"><table className="w-full min-w-[760px] text-left text-xs"><thead className="border-b border-border text-muted-foreground"><tr><th className="pb-2 pr-3">方向</th><th className="pb-2 pr-3">数量</th><th className="pb-2 pr-3">入场</th><th className="pb-2 pr-3">出场</th><th className="pb-2 pr-3">价格</th><th className="pb-2 pr-3">盈亏</th><th className="pb-2">原因</th></tr></thead><tbody>{(test?.trades || []).slice(-30).reverse().map((trade, index) => <tr key={`${trade.entry_time}-${trade.exit_time}-${index}`} className="border-b border-border/60"><td className={cn("py-2 pr-3 font-semibold", trade.side === "LONG" ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400")}>{trade.side === "LONG" ? "多头" : "空头"}</td><td className="py-2 pr-3">{trade.qty}</td><td className="py-2 pr-3">{trade.entry_time || "--"}</td><td className="py-2 pr-3">{trade.exit_time || "--"}</td><td className="py-2 pr-3">{money(trade.entry_price)} → {money(trade.exit_price)}</td><td className={cn("py-2 pr-3 font-semibold", tone(trade.pnl))}>{money(trade.pnl)}</td><td className="py-2">{trade.exit_reason}</td></tr>)}</tbody></table></div></div><div className="rounded-md border border-amber-500/25 bg-amber-500/10 p-3 text-xs leading-5 text-amber-800 dark:text-amber-200"><b>回测限制：</b>{(result.limitations || []).join("；")}</div></div> : <div className="mt-5 rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">尚无回测结果。点击“运行回测”后，系统会下载或读取缓存的 SOXL 行情；没有 Databento 凭证时会明确提示，不会生成空收益报告。</div>}</section>
    </div>
  );
}
