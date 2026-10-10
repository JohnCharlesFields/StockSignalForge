import { useEffect, useState } from "react";
import { Activity, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { api, type VSwingStatus } from "@/lib/api";

export function VSwingPanel({ onComplete }: { onComplete?: () => void }) {
  const [data, setData] = useState<VSwingStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const running = starting || data?.job.status === "running";
  useEffect(() => { api.getVSwingStatus().then(setData).catch(() => {}); }, []);
  useEffect(() => {
    if (data?.job.status !== "running") return;
    const timer = window.setInterval(() => api.getVSwingStatus().then(next => {
      setData(next);
      if (next.job.status === "completed") onComplete?.();
    }).catch(() => {}), 3000);
    return () => window.clearInterval(timer);
  }, [data?.job.status, onComplete]);
  const run = async () => {
    setStarting(true);
    try {
      const job = await api.runVSwing();
      setData(previous => ({ ...(previous ?? { snapshot: {}, validation: {} }), job }));
    } catch (error) { toast.error(error instanceof Error ? error.message : "波段打分未能启动"); }
    finally { setStarting(false); }
  };
  if (data?.enabled === false) return null;
  return <section className="border-y py-3 text-sm">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="font-semibold">波段研究 · v-swing <span className="ml-2 text-xs text-amber-700 dark:text-amber-300">实验性策略</span></h2>
        <p className="mt-1 text-xs text-muted-foreground">交易日 {data?.snapshot.session ?? "尚未打分"} · 新买点 {data?.snapshot.today_buys ?? "--"} · 持续监测 {data?.snapshot.open_buys ?? "--"}</p></div>
      <button type="button" onClick={run} disabled={running} className="inline-flex items-center gap-2 rounded border px-3 py-2 text-sm hover:bg-muted disabled:opacity-60">
        {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Activity className="h-4 w-4" />}{running ? "波段打分中" : "运行波段打分"}
      </button>
    </div>
    {(data?.job.status === "failed" || data?.job.status === "interrupted") && <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">本次打分未完成，保留原快照，可重试。{data.job.error_type}</p>}
    <details className="mt-2 text-xs text-muted-foreground"><summary className="cursor-pointer">验证口径与回放结果</summary>
      <div className="mt-2 space-y-1">
        <p>原标注 {data?.validation.data_audit?.supplied_labels ?? "--"} 条 · 可用买/卖 {data?.validation.data_audit?.used_buy ?? "--"}/{data?.validation.data_audit?.used_sell ?? "--"} · {data?.validation.data_audit?.symbols ?? "--"} 只训练证券。</p>
        {(["buy", "sell"] as const).map(side => { const metric = data?.validation.group_sides?.[side]; return <p key={side}>{side === "buy" ? "买点" : "卖点"}跨股票 PR-AUC {metric?.rf.pr_auc?.toFixed(3) ?? "--"} · 随机基线 {metric?.rf.baseline_pr_auc?.toFixed(3) ?? "--"} · 逻辑回归 {metric?.logistic.pr_auc?.toFixed(3) ?? "--"}</p>; })}
        <p>逐月前推已结算 {data?.validation.resolved_buys ?? "--"} 笔 · 净盈利占比 {data?.validation.net_win_rate != null ? `${(data.validation.net_win_rate*100).toFixed(1)}%` : "--"}；这是含复盘标注的历史回放，不是实盘验证。</p>
        <p>盈利概率独立校准：{data?.snapshot.profit_calibration?.eligible ? "实验性评估通过" : "样本不足或未通过，未替代主榜概率"}。</p>
        <p>买卖匹配强度不等于盈利胜率；卖点仅为持仓离场参考，不做空。本报告为数据决策参考，不构成投资建议。</p>
      </div>
    </details>
  </section>;
}
