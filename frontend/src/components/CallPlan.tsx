import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Calculator, ChevronDown, ChevronUp, Loader2, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Outcome { estimated_bid: number; net_usd: number; return_on_premium: number }
interface Scenario { hold_days: number; exit_date: string; calendar_days: number; stock_target: number; target_name: string; iv_down: Outcome; iv_flat: Outcome; iv_up: Outcome }
interface Candidate {
  contract: string; expiry: string; strike: number; dte: number; bid: number; ask: number; iv: number;
  delta: number; quote_at: string; cash_usd: number; planned_stop_risk_usd: number; max_loss_usd: number;
  comparison_eligible: boolean; spread_fraction: number; risk_flags: string[]; bid_size: number | null; ask_size: number | null;
  quote_samples: number; target_exit_bid: number; stop_exit_bid: number; pricing_basis: string; scenarios: Scenario[];
}
interface Snapshot {
  symbol: string; status: string; generated_at: string; quote_date: string; plan_start_date: string; spot?: number;
  reason: string; candidates: Candidate[]; model_note: string; priority_pool?: string; estimated_cost_usd: number;
  config: { capital: number; risk_fraction: number; hold_days: number };
  support_price?: number; breakout_price?: number;
  events?: { status: string; next_date?: string; latest_exit_date?: string; risk_flags: string[] };
}
export interface CallPlanData {
  available: boolean; stale: boolean; snapshot?: Snapshot | null;
  current_request_blocked?: boolean;
  job: { status: string; reason?: string; estimated_cost_usd?: number; next_estimated_cost_usd?: number | null; parameters?: Record<string, number> };
}
const money = (v?: number | null) => v == null || !Number.isFinite(v) ? "--" : `$${v.toFixed(2)}`;
const pct = (v?: number | null) => v == null || !Number.isFinite(v) ? "--" : `${(v * 100).toFixed(1)}%`;
const stateNames: Record<string, string> = { idle: "尚未计算", queued: "等待计算", running: "计算中", completed: "情景计算完成", partial: "部分数据可用", unavailable: "数据/费用受限", blocked: "入场条件未通过", failed: "计算未完成", interrupted: "计算已中断" };

export function CallPlan({ symbol }: { symbol: string }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<CallPlanData>();
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [capital, setCapital] = useState(2000);
  const [risk, setRisk] = useState(1.5);
  const [hold, setHold] = useState(10);
  const [cost, setCost] = useState(0);
  const polling = useRef(false);
  const snapshot = data?.snapshot;
  const candidates = snapshot?.candidates || [];
  const active = candidates.find(c => c.contract === selected) || candidates[0];
  const running = busy || ["running", "queued"].includes(data?.job.status || "");
  const refresh = async () => {
    setError("");
    try { setData(await api.getCallPlan(symbol)); }
    catch (e) { setError(e instanceof Error ? e.message : "读取失败，已有计划保留"); }
  };
  useEffect(() => {
    if (!open) return;
    let alive = true;
    const controller = new AbortController();
    const load = async () => {
      if (polling.current) return;
      polling.current = true;
      try { const value = await api.getCallPlan(symbol, controller.signal); if (alive) setData(value); }
      catch (e) { if (alive) setError(e instanceof Error ? e.message : "读取计划失败"); }
      finally { polling.current = false; }
    };
    void load();
    const timer = window.setInterval(() => { if (alive) void load(); }, 5000);
    return () => { alive = false; controller.abort(); window.clearInterval(timer); };
  }, [open, symbol]);
  const run = async () => {
    if (![capital, risk, hold, cost].every(Number.isFinite) || capital < 100 || risk < 0.1 || risk > 5 || hold < 1 || hold > 10 || cost < 0 || cost > 1) {
      setError("请检查资金、风险比例、持有天数与费用上限。"); return;
    }
    if (cost > 0 && !window.confirm(`本次仅 ${symbol}，允许 Databento 请求预估费用累计最多 $${cost.toFixed(2)}。页面读取不会产生期权下载费用，是否计算？`)) return;
    setBusy(true); setError("");
    try {
      const state = await api.runCallPlan(symbol, { capital, risk_pct: risk, hold_days: hold, max_cost: cost });
      if (state.status === "busy") setError("个股后台任务繁忙，请稍后重试。");
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : "提交失败"); }
    finally { setBusy(false); }
  };
  return <section className="min-w-0 border-y py-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="text-sm font-semibold">60–90天 Call 合约与退出计划</h2>
      <button type="button" onClick={() => setOpen(v => !v)} className="inline-flex items-center gap-2 rounded border px-3 py-2 text-xs" aria-expanded={open}>
        <Calculator size={15} />{open ? "收起计划" : "查看 Call 计划"}{open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>
    </div>
    {open && <div className="mt-3 min-w-0 space-y-3">
      <div className="flex flex-wrap items-end gap-3 text-xs">
        <label className="space-y-1">资金 USD<input aria-label="计划资金" type="number" min={100} max={10000000} value={capital} onChange={e => setCapital(Number(e.target.value))} className="block w-28 rounded border bg-background px-2 py-2" /></label>
        <label className="space-y-1">单笔风险 %<input aria-label="单笔风险比例" type="number" min={0.1} max={5} step={0.1} value={risk} onChange={e => setRisk(Number(e.target.value))} className="block w-24 rounded border bg-background px-2 py-2" /></label>
        <label className="space-y-1">最多交易日<input aria-label="最长持有交易日" type="number" min={1} max={10} step={1} value={hold} onChange={e => setHold(Number(e.target.value))} className="block w-24 rounded border bg-background px-2 py-2" /></label>
        <label className="space-y-1">本次数据费上限 USD<input aria-label="本次数据费用上限" type="number" min={0} max={1} step={0.01} value={cost} onChange={e => setCost(Number(e.target.value))} className="block w-28 rounded border bg-background px-2 py-2" /></label>
        <button type="button" title="只读取已有计划，不下载期权行情" onClick={() => void refresh()} className="inline-flex items-center gap-1 rounded border px-3 py-2"><RefreshCw size={14} />刷新快照</button>
        <button type="button" disabled={running} onClick={() => void run()} className="inline-flex items-center gap-1 rounded bg-primary px-3 py-2 text-primary-foreground disabled:opacity-50">{running ? <Loader2 size={14} className="animate-spin" /> : <Calculator size={14} />}手动计算</button>
      </div>
      <p className="text-xs text-muted-foreground">{stateNames[data?.job.status || "idle"] || data?.job.status} · 本次已执行请求预估费用 {money(data?.job.estimated_cost_usd ?? snapshot?.estimated_cost_usd)}{data?.job.reason ? ` · ${data.job.reason}` : ""}</p>
      {data?.job.next_estimated_cost_usd != null && <p className="text-xs text-amber-700 dark:text-amber-300">下一步请求预估 ${data.job.next_estimated_cost_usd.toFixed(6)}，未下载；后续报价请求仍受累计上限约束。</p>}
      {error && <p role="alert" className="text-xs text-rose-700 dark:text-rose-300">{error}</p>}
      {snapshot && <>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>参考收盘 {snapshot.quote_date} · {money(snapshot.spot)}</span><span>计划起始 {snapshot.plan_start_date}</span>
          <span>{snapshot.priority_pool}</span><span>财报 {snapshot.events?.next_date || "未知，需人工核验"}</span>
          <span>最晚退出 {snapshot.events?.latest_exit_date || "不生成入场计划"}</span>
          <span>快照资金 {money(snapshot.config.capital)} / 风险 {pct(snapshot.config.risk_fraction)}</span>
        </div>
        {data?.stale && <p className="text-xs text-amber-700 dark:text-amber-300">快照行情已过期，仅用于复盘；手动计算后核验新报价。</p>}
        {data?.current_request_blocked && <p className="text-xs text-amber-700 dark:text-amber-300">当前入场前置条件未通过；以下保留的旧合约计划仅供复盘。</p>}
        <p className="text-xs">{snapshot.reason}</p>
        <div className="grid min-w-0 gap-3 lg:grid-cols-3">
          {candidates.map((c, index) => <button key={c.contract} type="button" onClick={() => setSelected(c.contract)} className={cn("min-w-0 rounded border p-3 text-left text-xs", active?.contract === c.contract && "border-primary ring-1 ring-primary")}>
            <div className="flex flex-wrap justify-between gap-1 font-semibold"><span>{index + 1}. {c.expiry} · {c.dte}天</span><span>Call {money(c.strike)}</span></div>
            <p className="mt-1 break-all text-[10px] text-muted-foreground">{c.contract}</p>
            <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1">
              <dt>买 Ask / 卖 Bid</dt><dd>{money(c.ask)} / {money(c.bid)}</dd><dt>一张成本</dt><dd>{money(c.cash_usd)}</dd>
              <dt>Delta / IV</dt><dd>{c.delta.toFixed(2)} / {pct(c.iv)}</dd><dt>报价价差</dt><dd>{pct(c.spread_fraction)}</dd>
              <dt>止损计划风险</dt><dd>{money(c.planned_stop_risk_usd)}</dd><dt>全部权利金风险</dt><dd>{money(c.max_loss_usd)}</dd>
              <dt>双边数量</dt><dd>{c.bid_size ?? "--"} / {c.ask_size ?? "--"}</dd><dt>有效分钟样本</dt><dd>{c.quote_samples}</dd>
            </dl>
            <p className="mt-2 break-words text-[10px] text-muted-foreground">报价 {c.quote_at} · OI/成交量未验证</p>
            <p className={cn("mt-2 text-xs", c.comparison_eligible ? "text-foreground" : "text-amber-700 dark:text-amber-300")}>{c.risk_flags.length ? c.risk_flags.join(" · ") : "通过报价与预算筛查，仅供合约比较"}</p>
          </button>)}
        </div>
        {active && <>
          <div className="flex flex-wrap gap-4 text-xs"><span>正股突破参考 {money(snapshot.breakout_price)}</span><span>正股失效参考 {money(snapshot.support_price)}</span><span>+50%退出 Bid {money(active.target_exit_bid)}</span><span>−25%退出 Bid {money(active.stop_exit_bid)}</span></div>
          <h3 className="text-xs font-semibold">持有窗口与价格情景 · 不代表到达概率</h3>
          <div className="space-y-2">
            {active.scenarios.map(s => <div key={`${s.hold_days}-${s.target_name}`} className="grid min-w-0 grid-cols-2 gap-2 border-b py-2 text-xs sm:grid-cols-5">
              <div><b>{s.hold_days}个交易日</b><div className="text-muted-foreground">{s.exit_date}</div></div>
              <div>{s.target_name}<div>{money(s.stock_target)}</div></div>
              {([["iv_down", "IV回落20%"], ["iv_flat", "IV不变"], ["iv_up", "IV上升20%"]] as const).map(([key, label]) => <div key={key}><span className="text-muted-foreground">{label}</span><div>估计 Bid {money(s[key].estimated_bid)}</div><div className={s[key].net_usd >= 0 ? "text-emerald-700 dark:text-emerald-300" : "text-rose-700 dark:text-rose-300"}>{money(s[key].net_usd)} · {pct(s[key].return_on_premium)}</div></div>)}
            </div>)}
          </div>
          <p className="text-[11px] text-muted-foreground">{active.pricing_basis}</p>
        </>}
        <p className="flex items-start gap-2 text-[11px] leading-5 text-muted-foreground"><AlertTriangle size={14} className="mt-1 shrink-0" /><span>{snapshot.model_note}。情景退出价扣当前半价差、每股$0.02滑点和双边费用，未来价差可能更宽。历史报价不是可成交承诺；止损跳空可能损失全部权利金。已知财报之外的事件日历未完整核验。</span></p>
      </>}
      {!snapshot && !running && <p className="text-xs text-muted-foreground">尚无合约计划。默认费用上限为0，仅允许已有缓存或免费请求；需付费时会停止并提示。</p>}
    </div>}
  </section>;
}
