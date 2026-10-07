import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Briefcase,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  Wallet,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import {
  api,
  type PortfolioAction,
  type PortfolioHoldingView,
  type PortfolioView,
} from "@/lib/api";

function money(v?: number | null, dp = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  return v.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

function pct(v?: number | null, withSign = false): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  const s = (v * 100).toFixed(1);
  return `${withSign && v > 0 ? "+" : ""}${s}%`;
}

const ACTION_STYLE: Record<PortfolioAction, string> = {
  ADD: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/40",
  HOLD: "bg-slate-500/15 text-slate-600 dark:text-slate-300 border-slate-500/30",
  TRIM: "bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/40",
  CLOSE: "bg-red-500/15 text-red-700 dark:text-red-300 border-red-500/40",
};

const PNL_CLASS = (v?: number | null) =>
  v === null || v === undefined ? "" : v > 0 ? "text-emerald-600 dark:text-emerald-400" : v < 0 ? "text-red-600 dark:text-red-400" : "";

const BADGE_LABEL: Record<string, string> = {
  validated: "已验证",
  low_edge: "低把握",
  experimental: "未验证",
};

function DistributionRiskTag({ h }: { h: PortfolioHoldingView }) {
  const risk = h.distribution_risk;
  if (!risk?.available || !risk.triggered) return null;
  const high = risk.level === "high";
  const title = [
    ...(risk.reasons ?? []),
    risk.volume_ratio ? `量比 ${risk.volume_ratio}x` : "",
    risk.close_location != null ? `收盘位置 ${(risk.close_location * 100).toFixed(0)}%` : "",
  ].filter(Boolean).join("；");
  return (
    <div
      className={cn(
        "mt-1 inline-flex rounded border px-1.5 py-0.5 text-[10px] font-semibold",
        high
          ? "border-red-500/40 bg-red-500/15 text-red-700 dark:text-red-300"
          : "border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-300",
      )}
      title={title || risk.method}
    >
      {risk.label || "高位派发风险"}
    </div>
  );
}

export function Portfolio() {
  const [view, setView] = useState<PortfolioView | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState<PortfolioHoldingView | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [cashDraft, setCashDraft] = useState<string>("");
  const [editingCash, setEditingCash] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const v = await api.getPortfolioView();
      setView(v);
      setCashDraft(String(v.account.available_cash ?? 0));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const saveCash = async () => {
    const cash = Number(cashDraft);
    if (Number.isNaN(cash) || cash < 0) {
      toast.error("请输入有效的现金金额");
      return;
    }
    setSaving(true);
    try {
      const v = await api.setPortfolioAccount(cash);
      setView(v);
      setEditingCash(false);
      toast.success("已更新可用现金");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const removeHolding = async (symbol: string) => {
    try {
      const v = await api.deletePortfolioHolding(symbol);
      setView(v);
      toast.success(`已删除 ${symbol}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "删除失败");
    }
  };

  const account = view?.account;
  const counts = view?.action_counts;

  return (
    <div className="h-full overflow-y-auto bg-background">
      <main className="mx-auto max-w-6xl px-6 py-6 space-y-5">
        <section className="flex items-end justify-between border-b pb-4">
          <div>
            <div className="mb-1 inline-flex items-center gap-2 text-xs font-medium text-primary">
              <Briefcase className="h-3.5 w-3.5" aria-hidden="true" />
              持仓决策
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">我的持仓与加减仓建议</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              录入持仓与可用现金，系统按【已验证信号 + 风控纪律】给出加仓 / 持有 / 减仓 / 平仓建议与价位。
            </p>
          </div>
          <button
            onClick={load}
            className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-muted"
          >
            <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} /> 刷新
          </button>
        </section>

        {/* Account summary */}
        <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <SummaryCard label="总资产" value={`$${money(account?.total_equity)}`} />
          <SummaryCard label="持仓市值" value={`$${money(account?.market_value)}`} sub={`仓位 ${pct(account?.invested_pct)}`} />
          <SummaryCard
            label="浮动盈亏"
            value={`${account && account.unrealized_pnl > 0 ? "+" : ""}$${money(account?.unrealized_pnl)}`}
            sub={pct(account?.unrealized_pct, true)}
            valueClass={PNL_CLASS(account?.unrealized_pnl)}
          />
          <div className="rounded-lg border bg-card p-3">
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground inline-flex items-center gap-1">
                <Wallet className="h-3 w-3" /> 可用现金
              </span>
              {!editingCash && (
                <button onClick={() => setEditingCash(true)} className="text-muted-foreground hover:text-foreground">
                  <Pencil className="h-3 w-3" />
                </button>
              )}
            </div>
            {editingCash ? (
              <div className="mt-1 flex items-center gap-1">
                <input
                  type="number"
                  value={cashDraft}
                  onChange={(e) => setCashDraft(e.target.value)}
                  className="w-full rounded border bg-background px-2 py-1 text-sm"
                  autoFocus
                />
                <button onClick={saveCash} disabled={saving} className="rounded bg-primary px-2 py-1 text-xs text-primary-foreground disabled:opacity-60">
                  {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : "存"}
                </button>
                <button onClick={() => { setEditingCash(false); setCashDraft(String(account?.available_cash ?? 0)); }} className="rounded border px-2 py-1 text-xs">
                  <X className="h-3 w-3" />
                </button>
              </div>
            ) : (
              <div className="mt-1 text-lg font-semibold tabular-nums">${money(account?.available_cash)}</div>
            )}
            <div className="mt-0.5 text-[11px] text-muted-foreground">现金占比 {pct(account?.cash_pct)}</div>
          </div>
        </section>

        {/* Action summary + regime */}
        <section className="flex flex-wrap items-center gap-2 text-xs">
          <ActionChip action="CLOSE" n={counts?.CLOSE} label="平仓" />
          <ActionChip action="TRIM" n={counts?.TRIM} label="减仓" />
          <ActionChip action="ADD" n={counts?.ADD} label="加仓" />
          <ActionChip action="HOLD" n={counts?.HOLD} label="持有" />
          {view?.regime && (
            <span className="ml-auto rounded-md border px-2 py-1 text-muted-foreground">
              市况择时 ×{view.regime.gross_exposure_multiplier.toFixed(2)}
              {view.regime.regime_cn ? `（${view.regime.regime_cn}）` : ""}
            </span>
          )}
        </section>

        {/* Add / edit holding */}
        <section>
          {!showForm && !editing ? (
            <button
              onClick={() => setShowForm(true)}
              className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            >
              <Plus className="h-4 w-4" /> 添加持仓
            </button>
          ) : (
            <HoldingForm
              initial={editing}
              onClose={() => { setShowForm(false); setEditing(null); }}
              onSaved={(v) => { setView(v); setShowForm(false); setEditing(null); }}
            />
          )}
        </section>

        {/* Holdings table */}
        <section className="rounded-lg border bg-card">
          {loading && !view ? (
            <div className="p-10 text-center text-muted-foreground">加载中...</div>
          ) : !view || view.holdings.length === 0 ? (
            <div className="p-10 text-center text-sm text-muted-foreground">
              暂无持仓。点击上方“添加持仓”，并设置可用现金后，即可获得加减仓建议。
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b text-left text-xs text-muted-foreground">
                    <th className="px-3 py-2">建议</th>
                    <th className="px-3 py-2">代码</th>
                    <th className="px-3 py-2 text-right">现价/成本</th>
                    <th className="px-3 py-2 text-right">浮盈</th>
                    <th className="px-3 py-2 text-right">仓位</th>
                    <th className="px-3 py-2">信号</th>
                    <th className="px-3 py-2">关键价位</th>
                    <th className="px-3 py-2">理由</th>
                    <th className="px-3 py-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {view.holdings.map((h) => (
                    <tr key={h.symbol} className="border-b last:border-0 align-top hover:bg-muted/40">
                      <td className="px-3 py-3">
                        <span className={cn("inline-flex items-center rounded-md border px-2 py-1 text-xs font-semibold", ACTION_STYLE[h.decision.action])}>
                          {h.decision.action_cn}
                        </span>
                      </td>
                      <td className="px-3 py-3">
                        <Link to={h.detail_url} className="font-mono font-semibold text-primary hover:underline">
                          {h.symbol}
                        </Link>
                        <div className="text-[11px] text-muted-foreground">{money(h.shares, 0)} 股</div>
                        <DistributionRiskTag h={h} />
                        {typeof h.earnings?.days_until === "number" && h.earnings.days_until >= 0 && h.earnings.days_until <= 14 && (
                          <div
                            className={cn(
                              "mt-0.5 inline-block rounded px-1.5 py-0.5 text-[10px] font-medium",
                              h.earnings.days_until <= 7
                                ? "bg-red-500/15 text-red-700 dark:text-red-300"
                                : "bg-amber-500/15 text-amber-700 dark:text-amber-300",
                            )}
                            title={`下次财报 ${h.earnings.next_date}`}
                          >
                            财报{h.earnings.days_until}天后
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-3 text-right tabular-nums">
                        <div>${money(h.current_price)}</div>
                        <div className="text-[11px] text-muted-foreground">成本 ${money(h.avg_cost)}</div>
                      </td>
                      <td className={cn("px-3 py-3 text-right tabular-nums", PNL_CLASS(h.unrealized_pnl))}>
                        <div>{h.unrealized_pnl > 0 ? "+" : ""}${money(h.unrealized_pnl)}</div>
                        <div className="text-[11px]">{pct(h.unrealized_pct, true)}</div>
                      </td>
                      <td className="px-3 py-3 text-right tabular-nums">
                        <div>${money(h.position_value, 0)}</div>
                        <div className="text-[11px] text-muted-foreground">{pct(h.weight_pct)}</div>
                      </td>
                      <td className="px-3 py-3">
                        {h.signal_available ? (
                          <div className="space-y-0.5 text-xs">
                            <div>
                              胜率{" "}
                              <span className="font-semibold">{h.calibrated_win_rate != null ? `${(h.calibrated_win_rate * 100).toFixed(0)}%` : "--"}</span>
                              {h.confidence_badge && (
                                <span className="ml-1 text-[10px] text-muted-foreground">
                                  ({BADGE_LABEL[h.confidence_badge] ?? h.confidence_badge})
                                </span>
                              )}
                            </div>
                            <div className="text-muted-foreground">{h.pullback_state ?? "--"}</div>
                            {h.hv_state && <div className="text-[11px] text-muted-foreground">{h.hv_state}</div>}
                          </div>
                        ) : (
                          <span className="text-xs text-muted-foreground">信号不可用</span>
                        )}
                      </td>
                      <td className="px-3 py-3 text-xs tabular-nums">
                        <div className="text-red-600 dark:text-red-400">止损 ${money(h.decision.levels.stop_price)}</div>
                        <div className="text-emerald-600 dark:text-emerald-400">止盈 ${money(h.decision.levels.take_profit)}</div>
                        {h.decision.action === "ADD" && h.decision.levels.add_zone_low != null && (
                          <div className="text-muted-foreground">
                            加仓 ${money(h.decision.levels.add_zone_low)}~${money(h.decision.levels.add_zone_high)}
                            {h.decision.add_qty ? ` · ${h.decision.add_qty}股` : ""}
                          </div>
                        )}
                        {h.decision.action === "TRIM" && h.decision.levels.trim_shares ? (
                          <div className="text-amber-600 dark:text-amber-400">减约 {h.decision.levels.trim_shares} 股</div>
                        ) : null}
                      </td>
                      <td className="px-3 py-3 text-xs text-muted-foreground max-w-xs">
                        {h.decision.reason}
                        {h.decision.rotation && h.decision.rotation.targets.length > 0 && (
                          <div className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-2">
                            <div className="mb-1 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                              换仓建议 · 腾出 ${money(h.decision.rotation.freed_capital)} → 高流动性·深超卖
                            </div>
                            <ul className="space-y-1">
                              {h.decision.rotation.targets.map((t) => (
                                <li key={t.symbol} className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                                  <a href={t.detail_url} className="font-semibold text-primary hover:underline">{t.symbol}</a>
                                  <span className="font-mono text-foreground">${money(t.current_price)}</span>
                                  <span className="rounded bg-rose-500/15 px-1 text-[10px] text-rose-700 dark:text-rose-300">{t.deep_oversold_cn}</span>
                                  {t.liquidity_tier && <span className="text-[10px] text-muted-foreground">{t.liquidity_tier}</span>}
                                  {t.sector && <span className="rounded bg-muted px-1 text-[10px] text-muted-foreground">{t.sector}</span>}
                                  {t.news_label && (
                                    <span className={cn("rounded px-1 text-[10px]", (t.news_neg ?? 0) > 0 ? "bg-amber-500/15 text-amber-700 dark:text-amber-300" : "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300")}>{t.news_label}</span>
                                  )}
                                  <span>胜率 {pct(t.calibrated_win_rate)}</span>
                                  {t.est_shares ? <span>约 {t.est_shares} 股</span> : null}
                                  {t.est_excess_8d != null ? (
                                    <span className={cn("font-medium", t.est_excess_8d >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400")}>
                                      预计8日净超额 {t.est_excess_8d >= 0 ? "+" : "-"}${money(Math.abs(t.est_excess_8d))}
                                      {t.expected_excess_8d != null ? `（${pct(t.expected_excess_8d, true)}）` : ""}
                                      {t.excess_is_estimate ? "·系统均值" : ""}
                                    </span>
                                  ) : (
                                    <span className="text-muted-foreground/70">预计收益·样本不足</span>
                                  )}
                                </li>
                              ))}
                            </ul>
                            <div className="mt-1 text-[10px] text-muted-foreground/80">预计收益=校准的 8 日净成本收益期望，非保证，edge 偏小。</div>
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex gap-1">
                          <button
                            onClick={() => { setEditing(h); setShowForm(false); }}
                            className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                            title="编辑"
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </button>
                          <button
                            onClick={() => removeHolding(h.symbol)}
                            className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-red-600"
                            title="删除"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {view?.method_note && (
          <p className="text-xs leading-relaxed text-muted-foreground">{view.method_note}</p>
        )}
      </main>
    </div>
  );
}

function SummaryCard({ label, value, sub, valueClass }: { label: string; value: string; sub?: string; valueClass?: string }) {
  return (
    <div className="rounded-lg border bg-card p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-lg font-semibold tabular-nums", valueClass)}>{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

function ActionChip({ action, n, label }: { action: PortfolioAction; n?: number; label: string }) {
  if (!n) return null;
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-md border px-2 py-1 font-medium", ACTION_STYLE[action])}>
      {label} {n}
    </span>
  );
}

function HoldingForm({
  initial,
  onClose,
  onSaved,
}: {
  initial: PortfolioHoldingView | null;
  onClose: () => void;
  onSaved: (v: PortfolioView) => void;
}) {
  const [symbol, setSymbol] = useState(initial?.symbol ?? "");
  const [shares, setShares] = useState(initial ? String(initial.shares) : "");
  const [avgCost, setAvgCost] = useState(initial ? String(initial.avg_cost) : "");
  const [note, setNote] = useState(initial?.note ?? "");
  const [saving, setSaving] = useState(false);
  const isEdit = Boolean(initial);

  const valid = useMemo(
    () => symbol.trim() && Number(shares) > 0 && Number(avgCost) > 0,
    [symbol, shares, avgCost],
  );

  const submit = async () => {
    if (!valid) {
      toast.error("请填写代码、股数、成本");
      return;
    }
    setSaving(true);
    try {
      const v = await api.upsertPortfolioHolding({
        symbol: symbol.trim().toUpperCase(),
        shares: Number(shares),
        avg_cost: Number(avgCost),
        note: note.trim(),
      });
      onSaved(v);
      toast.success(isEdit ? "已更新持仓" : "已添加持仓");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold">{isEdit ? `编辑 ${initial?.symbol}` : "添加持仓"}</h3>
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground"><X className="h-4 w-4" /></button>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
        <Field label="代码">
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            disabled={isEdit}
            placeholder="AAPL"
            className="w-full rounded border bg-background px-2 py-1.5 text-sm uppercase disabled:opacity-60"
          />
        </Field>
        <Field label="持仓数 (股)">
          <input type="number" value={shares} onChange={(e) => setShares(e.target.value)} placeholder="100" className="w-full rounded border bg-background px-2 py-1.5 text-sm" />
        </Field>
        <Field label="平均成本 ($)">
          <input type="number" value={avgCost} onChange={(e) => setAvgCost(e.target.value)} placeholder="150.00" className="w-full rounded border bg-background px-2 py-1.5 text-sm" />
        </Field>
        <Field label="备注 (可选)">
          <input value={note} onChange={(e) => setNote(e.target.value)} className="w-full rounded border bg-background px-2 py-1.5 text-sm" />
        </Field>
      </div>
      <div className="mt-3 flex gap-2">
        <button onClick={submit} disabled={saving || !valid} className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60">
          {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
          {isEdit ? "保存" : "添加"}
        </button>
        <button onClick={onClose} className="rounded-md border px-3 py-2 text-sm hover:bg-muted">取消</button>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1 block text-xs text-muted-foreground">{label}</label>
      {children}
    </div>
  );
}
