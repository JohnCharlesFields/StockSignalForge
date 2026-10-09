import { Fragment, useEffect, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, Loader2, RefreshCw, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { api, type LongOptionReview, type LongOptionScreenJob, type LongOptionScreenResponse, type LongOptionScreenRow, type LongOptionShadowScorecard, type PriorityBoardResponse } from "@/lib/api";
import { cn } from "@/lib/utils";
import { GildataEvidencePanel } from "@/components/GildataEvidence";

const STATUS: Record<string, string> = {
  signal: "方向信号", no_direction: "方向不明", missing_daily_history: "正股日线不足",
  missing_signal: "共识数据不足", stale_stock_data: "正股行情过旧", data_unavailable: "数据不可用",
};
const dollars = (value?: number | null) => value == null ? "--" : `$${value.toFixed(2)}`;
const percent = (value?: number | null) => value == null ? "--" : `${(value * 100).toFixed(1)}%`;
const WAVE_RULES: Record<string, string> = {
  wave2_above_start: "2浪未跌破起点", wave3_beyond_wave1: "3浪越过1浪终点",
  wave4_no_overlap: "4浪未重叠1浪", wave5_beyond_wave3: "5浪越过3浪终点",
  wave3_not_shortest: "3浪不是最短推动段", b_below_impulse_end: "B未越过5浪终点",
  c_beyond_a: "C越过A终点",
};

function direction(row: LongOptionScreenRow) {
  if (row.status !== "signal") return "观望";
  return row.side === "C" ? "研究 Call" : row.side === "P" ? "研究 Put" : "观望";
}

function SignalDetails({ row }: { row: LongOptionScreenRow }) {
  const [review, setReview] = useState<LongOptionReview | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const technical = row.technical_context;
  const wave = technical?.wave_structure;
  const waveName = wave?.status === "abc_candidate" ? "A-B-C 调整候选"
    : wave?.status === "impulse_candidate" ? "已完成的 1-5 推动浪候选"
    : wave?.status === "impulse_forming" ? "1-4 候选，第5浪待确认"
    : wave?.status === "stale_pattern" ? "旧浪形，仅供复盘"
    : wave?.status === "unverified" ? "数据不足，未核验" : "未识别标准浪形";
  const reviewStock = async () => {
    setReviewing(true);
    try { setReview(await api.reviewLongOption(row.symbol)); }
    catch (error) { toast.error(error instanceof Error ? error.message : "DeepSeek 复核未完成"); }
    finally { setReviewing(false); }
  };
  return <div className="space-y-3 text-xs">
    <div className="grid gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-4">
      <div>正股行情日期 <b className="block">{row.stock_data_as_of || "--"}</b></div>
      <div>SPY 基准日期 <b className="block">{row.benchmark_data_as_of || "--"}</b></div>
      <div>多头 / 空头共识 <b className="block font-mono">{percent(row.bull_consensus)} / {percent(row.bear_consensus)}</b></div>
      <div>数据状态 <b className="block">{row.reason || STATUS[row.status] || row.status}</b></div>
      <div>10日突破参考 <b className="block font-mono">{dollars(technical?.breakout_price)}</b></div>
      <div>10日支撑参考 <b className="block font-mono">{dollars(technical?.support_price)}</b></div>
      <div>60日波段 <b className="block font-mono">{dollars(technical?.range_low)} - {dollars(technical?.range_high)}</b></div>
      <div>浪形 <b className="block">{waveName}{wave?.direction ? ` · ${wave.direction === "up" ? "上行" : "下行"}` : ""}</b></div>
    </div>
    {wave && <div className="space-y-1 border-t pt-2">
      {wave.points.length > 0 ? <ol className="flex flex-wrap gap-x-4 gap-y-1 font-mono">
        {wave.points.map((point) => <li key={`${point.label}-${point.date}`} title={`摆动点于 ${point.confirmed_on} 确认`}>
          {point.label} · {point.date} · {dollars(point.price)} · {point.confirmed_on.slice(5)}确认
        </li>)}
      </ol> : wave.recent_pivots.length > 0 ? <p className="text-muted-foreground">
        最近已确认摆动：{wave.recent_pivots.map((point) => `${point.date} ${point.kind === "high" ? "高" : "低"} ${dollars(point.price)}（${point.confirmed_on.slice(5)}确认）`).join(" · ")}
      </p> : <p className="text-muted-foreground">暂无可确认摆动点。</p>}
      {wave.rule_checks.length > 0 && <p className="text-muted-foreground">符合的结构约束：{wave.rule_checks.map((rule) => WAVE_RULES[rule] || rule).join("、")}。</p>}
      <p className="text-muted-foreground">摆动点需后续 {wave.pivot_confirmation_bars} 个交易日确认，日线相邻浪段至少隔 2 日，最小摆幅取 2.5% 与 1 ATR 的较大者。结构可能随新行情修订；匹配规则不等于预测胜率，也不计入方向评分。</p>
    </div>}
    {technical?.fib_retracement && <p className="text-muted-foreground">斐波那契回撤参考：38.2% {dollars(technical.fib_retracement["38.2"])} · 50% {dollars(technical.fib_retracement["50"])} · 61.8% {dollars(technical.fib_retracement["61.8"])}。按近 60 日高低点计算，仅供定位，不是独立买入信号。</p>}
    <button type="button" onClick={() => void reviewStock()} disabled={reviewing || row.status !== "signal"} className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 font-medium hover:bg-muted disabled:opacity-50">
      {reviewing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}DeepSeek 方向复核
    </button>
    {review && (review.available ? <div className="space-y-2 border-t pt-3">
      <p className="font-semibold">模型 {review.model_view === "WAIT" ? "建议观望" : review.model_view === "CALL" ? "偏向 Call" : "偏向 Put"} · 综合 {review.final_view === "WAIT" ? "观望" : review.final_view === "CALL" ? "研究 Call" : "研究 Put"}</p>
      {review.conflict && <p className="text-amber-700 dark:text-amber-300">模型与规则方向冲突，综合结论降级为观望。</p>}
      <p>{review.summary}</p>
      <GildataEvidencePanel evidence={review.reference_evidence} marketDate={row.stock_data_as_of} />
      {review.supporting_points?.length ? <p>支持：{review.supporting_points.join("；")}</p> : null}
      {review.objections?.length ? <p className="text-amber-700 dark:text-amber-300">反对：{review.objections.join("；")}</p> : null}
      {review.watch_condition && <p>确认条件：{review.watch_condition}</p>}
      {review.invalidation_condition && <p>失效条件：{review.invalidation_condition}</p>}
      {review.wave_note && <p className="text-muted-foreground">浪形参考：{review.wave_note}</p>}
      <p className="text-muted-foreground">{review.model || "DeepSeek"} · 正股数据 {review.stock_data_as_of || "--"}；未估计真实期权收益。</p>
    </div> : <p role="status" className="text-amber-700 dark:text-amber-300">{review.reason}</p>)}
    <p className="text-muted-foreground">模型只复核正股方向，不提供期权盈利概率或具体合约。下单前仍需在券商核对权利金、价差与流动性。</p>
  </div>;
}

export function LongOptionBoard({ stockBoard }: { stockBoard: PriorityBoardResponse | null }) {
  const [data, setData] = useState<LongOptionScreenResponse | null>(null);
  const [job, setJob] = useState<LongOptionScreenJob | null>(null);
  const [shadow, setShadow] = useState<LongOptionShadowScorecard | null>(null);
  const [loading, setLoading] = useState(true);
  const [settling, setSettling] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    try {
      const [snapshot, state, shadowState] = await Promise.all([api.getLongOptionScreen(), api.getLongOptionScreenStatus(), api.getLongOptionShadow().catch(() => null)]);
      setData(snapshot);
      setJob(state);
      if (shadowState) setShadow(shadowState);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "方向快照读取失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void refresh(); }, []);
  useEffect(() => {
    if (job?.status !== "running") return;
    const timer = window.setInterval(async () => {
      try {
        const state = await api.getLongOptionScreenStatus();
        setJob(state);
        if (state.status === "completed") {
          setData(await api.getLongOptionScreen());
          setShadow(await api.getLongOptionShadow().catch(() => null));
          toast.success("正股方向研究快照已更新");
        } else if (state.status === "failed") {
          toast.error(state.error || "筛选失败；上一份快照仍可查看");
        }
      } catch { /* Keep the last completed snapshot visible during a transient poll failure. */ }
    }, 2500);
    return () => window.clearInterval(timer);
  }, [job?.status]);

  const run = async () => {
    try {
      const state = await api.runLongOptionScreen();
      setJob(state);
      if (state.status === "unavailable") toast.error(state.reason || "数据源不可用");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "无法启动方向筛选");
    }
  };

  const settle = async () => {
    setSettling(true);
    try {
      const result = await api.resolveLongOptionShadow();
      setShadow(result.scorecard);
      toast.success(result.resolve.resolved ? `已结算 ${result.resolve.resolved} 条方向记录` : "暂无到期且行情完整的记录");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "方向对账暂不可用");
    } finally {
      setSettling(false);
    }
  };

  const snapshot = data?.snapshot?.source === "equity:daily_ohlcv" ? data.snapshot : null;
  const oldSnapshot = Boolean(data?.snapshot && !snapshot);
  const stockPicks = new Map((stockBoard?.picks ?? []).map((pick) => [pick.symbol, pick]));

  return <>
    <section className="flex flex-wrap items-center justify-between gap-3 border-b pb-4">
      <div>
        <h2 className="text-lg font-semibold">单腿期权方向研究榜</h2>
        <p className="text-xs text-muted-foreground">基于正股走势与多空共识，区分研究 Call、研究 Put 或观望；不使用期权报价。</p>
      </div>
      <div className="flex flex-wrap gap-2">
        <button type="button" onClick={() => void refresh()} disabled={loading} className="inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-60">
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}刷新快照
        </button>
        <button type="button" onClick={() => void run()} disabled={job?.status === "running"} className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-60">
          {job?.status === "running" && <Loader2 className="h-4 w-4 animate-spin" />}运行筛选
        </button>
      </div>
    </section>
    {job?.status === "running" && <section role="status" className="rounded-md border bg-card px-3 py-2 text-sm">{job.stage || "正股方向筛选中"} · {job.processed ?? 0}/{job.total ?? "--"}；完成后自动更新快照。</section>}
    {job?.status === "failed" && <section role="alert" className="rounded-md border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-700 dark:text-rose-300">筛选失败：{job.error}</section>}
    {job?.shadow_warning && <p role="status" className="text-xs text-amber-700 dark:text-amber-300">影子对账暂未记录：{job.shadow_warning}</p>}
    {oldSnapshot && <section role="alert" className="flex gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-300"><AlertTriangle className="h-4 w-4 shrink-0" />旧版期权报价快照不适用于正股方向榜，请点击“运行筛选”生成新快照。</section>}
    {data?.stale && snapshot && <section className="flex gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-300"><AlertTriangle className="h-4 w-4 shrink-0" />正股方向快照已过期，仅供复盘；运行筛选后核对行情日期。</section>}
    {!snapshot && !oldSnapshot && <section className="rounded-md border bg-card p-8 text-center text-sm text-muted-foreground">{loading ? "正在读取快照…" : data?.reason || "尚无已完成的方向研究快照。"}</section>}
    {snapshot && <>
      <section className="flex flex-wrap items-center gap-x-5 gap-y-2 border-y py-2 text-xs" aria-label="正股方向前向验证">
        <span className="font-semibold">正股方向前向验证</span>
        <span>已记录 {shadow?.recorded ?? "--"}</span>
        <span>待结算 {shadow?.pending ?? "--"}</span>
        <span>到期结算 {shadow?.resolved ?? "--"}</span>
        <span>已记录信号日 {shadow?.recorded_signal_dates ?? "--"}</span>
        {shadow && shadow.directional.n > 0 && <span>5日方向命中 {percent(shadow.directional.hit_net)} · Call {percent(shadow.call.hit_net)} / Put {percent(shadow.put.hit_net)}</span>}
        {shadow && shadow.resolved > 0 && <span>出手覆盖 {percent(shadow.coverage)} · 对照始终看多 {percent(shadow.matched_always_call.delta_policy_minus_always_call)}</span>}
        <button type="button" onClick={() => void settle()} disabled={settling} className="inline-flex items-center gap-1 text-primary hover:underline disabled:opacity-50" title="仅用缓存结算已到期的正股方向记录">
          <RefreshCw className={cn("h-3.5 w-3.5", settling && "animate-spin")} />更新对账
        </button>
        <span className="w-full text-muted-foreground">{shadow?.evidence_status === "insufficient_live_sample" ? "前向样本尚不足，不作策略有效结论。" : "仅为正股方向代理，不是期权盈利概率。"}</span>
      </section>
      <section className="flex flex-wrap gap-x-4 gap-y-1 rounded-md border bg-card px-3 py-2 text-xs text-muted-foreground">
        <span>快照 {new Date(snapshot.generated_at).toLocaleString("zh-CN")}</span>
        <span>正股参考交易日 {snapshot.data_as_of}</span>
        <span>已核验赛道龙头 {snapshot.verified_sector_groups}/{snapshot.sector_group_count} 组；七姐妹固定纳入</span>
        <span>期权合约报价请求 {snapshot.option_data_request_count}</span>
      </section>
      {snapshot.option_volume_research && <p className="text-xs text-muted-foreground">近一年期权活跃股：{snapshot.option_volume_research.leaders.length} 只 · {snapshot.option_volume_research.window_start || "--"} 至 {snapshot.option_volume_research.window_end || "--"} · {snapshot.option_volume_research.coverage || snapshot.option_volume_research.reason}</p>}
      <p className="text-xs leading-relaxed text-muted-foreground">{snapshot.note}</p>
      <section className="border-y bg-card">
        <div className="hidden grid-cols-[3rem_minmax(5rem,1fr)_minmax(7rem,1.2fr)_minmax(5rem,0.9fr)_minmax(6rem,0.9fr)_minmax(5rem,0.8fr)_minmax(5rem,0.8fr)_minmax(6rem,0.9fr)] gap-2 bg-muted/40 px-3 py-2 text-xs text-muted-foreground md:grid">
          <span>#</span><span>标的 / 现价</span><span>赛道</span><span>方向</span><span>多头 / 空头</span><span>净共识</span><span>相对 SPY</span><span>HV20 / 日期</span>
        </div>
        {snapshot.rows.length === 0 && <p className="px-4 py-8 text-center text-sm text-muted-foreground">当前没有可展示的标的。</p>}
        {snapshot.rows.map((row, index) => {
          const pick = stockPicks.get(row.symbol);
          const open = expanded === row.symbol;
          const label = direction(row);
          return <Fragment key={row.symbol}>
            <div className="grid grid-cols-2 gap-x-3 gap-y-2 border-t px-3 py-3 text-sm md:grid-cols-[3rem_minmax(5rem,1fr)_minmax(7rem,1.2fr)_minmax(5rem,0.9fr)_minmax(6rem,0.9fr)_minmax(5rem,0.8fr)_minmax(5rem,0.8fr)_minmax(6rem,0.9fr)] md:items-start md:gap-2">
              <button type="button" onClick={() => setExpanded(open ? null : row.symbol)} title="查看数据状态与研究风险" aria-label={`查看 ${row.symbol} 详情`} className="col-span-2 inline-flex items-center gap-1 text-left text-muted-foreground hover:text-foreground md:col-span-1">{open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}{index + 1}</button>
              <div className="font-semibold"><a className="text-primary hover:underline" href={`/single-stock-overnight?symbol=${row.symbol}`}>{row.symbol}</a><div className="font-mono text-xs font-normal text-muted-foreground">{dollars(row.spot)}</div></div>
              <div className="text-xs"><span className="md:hidden text-muted-foreground">赛道 </span>{pick?.track_cn || pick?.track || (row.universe_source === "cboe_option_volume_top10" ? "期权活跃股" : "龙头研究")}{row.option_volume_12m != null && <div className="text-muted-foreground">Cboe 12月合约量 {row.option_volume_12m.toLocaleString()}</div>}</div>
              <div className={cn("text-xs font-semibold", label === "研究 Call" ? "text-emerald-600 dark:text-emerald-400" : label === "研究 Put" ? "text-rose-600 dark:text-rose-400" : "text-muted-foreground")}>{label}<div className="font-normal text-muted-foreground">{STATUS[row.status] || row.status}</div></div>
              <div className="font-mono text-xs"><span className="md:hidden text-muted-foreground">多 / 空 </span>{percent(row.bull_consensus)} / {percent(row.bear_consensus)}</div>
              <div className="font-mono text-xs"><span className="md:hidden text-muted-foreground">净共识 </span>{percent(row.net_consensus)}</div>
              <div className="font-mono text-xs"><span className="md:hidden text-muted-foreground">相对 SPY </span>{percent(row.relative_strength_20d)}</div>
              <div className="font-mono text-xs"><span className="md:hidden text-muted-foreground">HV20 </span>{percent(row.hv20)}<div className="text-muted-foreground">{row.stock_data_as_of || "日期 --"}</div></div>
            </div>
            {open && <div className="border-t bg-muted/20 px-4 py-3"><SignalDetails row={row} /></div>}
          </Fragment>;
        })}
      </section>
    </>}
  </>;
}
