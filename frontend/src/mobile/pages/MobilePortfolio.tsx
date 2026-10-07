import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronRight, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { toast } from "sonner";
import { api, type PortfolioHoldingView, type PortfolioView } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Chip, ErrorState, LoadingState, MCard, StatTile, money, moveClass, pct, signedPct } from "../components/ui";

const ACTION_TONE: Record<string, "strong" | "good" | "warn" | "bad" | "neutral"> = {
  buy: "strong",
  hold: "neutral",
  warn: "warn",
  sell: "bad",
};

function HoldingCard({ h, onDelete }: { h: PortfolioHoldingView; onDelete: (s: string) => void }) {
  const [confirm, setConfirm] = useState(false);
  const d = h.decision;
  return (
    <MCard className="space-y-2.5">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <Link to={`/m/stock?symbol=${encodeURIComponent(h.symbol)}`} className="m-break text-base font-bold text-primary">
            {h.symbol}
          </Link>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {h.shares} 股 @ {money(h.avg_cost, 2)}
            {h.current_price != null && <span className="ml-1">· 现 {money(h.current_price, 2)}</span>}
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className={cn("font-mono text-sm font-bold", moveClass(h.unrealized_pnl))}>{signedPct(h.unrealized_pct)}</div>
          <div className={cn("font-mono text-[11px]", moveClass(h.unrealized_pnl))}>{money(h.unrealized_pnl, 0)}</div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {d && <Chip tone={ACTION_TONE[d.tone] ?? "neutral"}>{d.action_cn}</Chip>}
        {h.calibrated_win_rate != null && <Chip tone={h.calibrated_win_rate >= 0.5 ? "good" : "warn"}>胜率 {pct(h.calibrated_win_rate)}</Chip>}
        {h.pullback_state && <Chip tone="neutral">{h.pullback_state}</Chip>}
        {typeof h.earnings?.days_until === "number" && <Chip tone={h.earnings.days_until <= 10 ? "warn" : "neutral"}>财报{h.earnings.days_until}天</Chip>}
      </div>

      {d?.reason && <p className="m-break text-xs leading-relaxed text-muted-foreground">{d.reason}</p>}

      {d?.levels && (d.levels.stop_price || d.levels.take_profit || d.levels.add_zone_low) && (
        <div className="grid grid-cols-3 gap-2">
          {d.levels.stop_price != null && <StatTile label="止损" value={money(d.levels.stop_price, 2)} tone="text-rose-600 dark:text-rose-400" />}
          {d.levels.take_profit != null && <StatTile label="止盈" value={money(d.levels.take_profit, 2)} tone="text-emerald-600 dark:text-emerald-400" />}
          {d.levels.add_zone_low != null && (
            <StatTile label="加仓区" value={`${money(d.levels.add_zone_low, 0)}~${money(d.levels.add_zone_high, 0)}`} />
          )}
        </div>
      )}

      <div className="flex items-center justify-between border-t pt-2">
        {confirm ? (
          <div className="flex items-center gap-2 text-xs">
            <span className="text-muted-foreground">确认删除？</span>
            <button type="button" onClick={() => onDelete(h.symbol)} className="rounded px-2 py-1 font-medium text-rose-600">删除</button>
            <button type="button" onClick={() => setConfirm(false)} className="rounded px-2 py-1 text-muted-foreground">取消</button>
          </div>
        ) : (
          <button type="button" onClick={() => setConfirm(true)} className="inline-flex min-h-[40px] items-center gap-1 text-xs text-muted-foreground active:text-rose-600">
            <Trash2 className="h-3.5 w-3.5" /> 删除
          </button>
        )}
        <Link to={`/m/stock?symbol=${encodeURIComponent(h.symbol)}`} className="inline-flex items-center text-xs text-primary">
          研判 <ChevronRight className="h-3.5 w-3.5" />
        </Link>
      </div>
    </MCard>
  );
}

export function MobilePortfolio() {
  const [view, setView] = useState<PortfolioView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [form, setForm] = useState({ symbol: "", shares: "", avg_cost: "" });
  const [busy, setBusy] = useState(false);

  const load = () => {
    setLoading(true);
    setError("");
    api
      .getPortfolioView()
      .then(setView)
      .catch((e) => setError(e instanceof Error ? e.message : "无法加载持仓"))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  const submitAdd = async () => {
    const symbol = form.symbol.trim().toUpperCase();
    const shares = Number(form.shares);
    const avg_cost = Number(form.avg_cost);
    if (!symbol || !Number.isFinite(shares) || shares <= 0 || !Number.isFinite(avg_cost) || avg_cost <= 0) {
      toast.error("请填写正确的代码、股数和成本价");
      return;
    }
    setBusy(true);
    try {
      await api.upsertPortfolioHolding({ symbol, shares, avg_cost });
      toast.success(`已添加 ${symbol}`);
      setForm({ symbol: "", shares: "", avg_cost: "" });
      setShowAdd(false);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "添加失败");
    } finally {
      setBusy(false);
    }
  };

  const del = async (symbol: string) => {
    try {
      await api.deletePortfolioHolding(symbol);
      toast.success(`已删除 ${symbol}`);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    }
  };

  return (
    <div className="space-y-3 p-3 pb-6">
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={() => setShowAdd((s) => !s)}
          className="inline-flex min-h-[40px] items-center gap-1.5 rounded-lg border border-primary/40 bg-primary/10 px-3 text-sm font-medium text-primary active:bg-primary/20"
        >
          {showAdd ? <X className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
          {showAdd ? "取消" : "添加持仓"}
        </button>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          aria-label="刷新"
          className="grid h-9 w-9 place-items-center rounded-lg border text-muted-foreground active:bg-muted disabled:opacity-60"
        >
          <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
        </button>
      </div>

      {showAdd && (
        <MCard className="space-y-2.5">
          <input
            value={form.symbol}
            onChange={(e) => setForm((f) => ({ ...f, symbol: e.target.value }))}
            placeholder="代码 (如 AAPL)"
            className="w-full rounded-lg border bg-background px-3 py-2.5 uppercase outline-none focus:border-primary"
            autoCapitalize="characters"
          />
          <div className="grid grid-cols-2 gap-2">
            <input
              value={form.shares}
              onChange={(e) => setForm((f) => ({ ...f, shares: e.target.value }))}
              placeholder="股数"
              inputMode="decimal"
              className="w-full rounded-lg border bg-background px-3 py-2.5 font-mono outline-none focus:border-primary"
            />
            <input
              value={form.avg_cost}
              onChange={(e) => setForm((f) => ({ ...f, avg_cost: e.target.value }))}
              placeholder="成本价"
              inputMode="decimal"
              className="w-full rounded-lg border bg-background px-3 py-2.5 font-mono outline-none focus:border-primary"
            />
          </div>
          <button
            type="button"
            onClick={submitAdd}
            disabled={busy}
            className="w-full rounded-lg bg-primary py-2.5 text-sm font-semibold text-primary-foreground active:opacity-90 disabled:opacity-60"
          >
            {busy ? "保存中…" : "保存"}
          </button>
        </MCard>
      )}

      {loading && !view && <LoadingState />}
      {error && !view && <ErrorState message={error} onRetry={load} />}

      {view && (
        <>
          <MCard className="space-y-3">
            <div>
              <div className="text-xs text-muted-foreground">总权益</div>
              <div className="font-mono text-2xl font-bold tabular-nums">{money(view.account.total_equity, 0)}</div>
              <div className={cn("font-mono text-xs", moveClass(view.account.unrealized_pnl))}>
                浮盈亏 {money(view.account.unrealized_pnl, 0)}（{signedPct(view.account.unrealized_pct)}）
              </div>
            </div>
            <div className="grid grid-cols-3 gap-2">
              <StatTile label="可用现金" value={money(view.account.available_cash, 0)} />
              <StatTile label="持仓市值" value={money(view.account.market_value, 0)} />
              <StatTile label="仓位" value={pct(view.account.invested_pct)} />
            </div>
            {view.regime?.regime_cn && (
              <div className="flex flex-wrap items-center gap-1.5 text-xs">
                <Chip tone="neutral">择时 {view.regime.regime_cn}</Chip>
                <span className="text-muted-foreground">敞口× {view.regime.gross_exposure_multiplier?.toFixed?.(2) ?? "--"}</span>
              </div>
            )}
          </MCard>

          {view.holdings.length === 0 ? (
            <div className="rounded-xl border bg-card p-6 text-center text-sm text-muted-foreground">暂无持仓，点「添加持仓」录入。</div>
          ) : (
            view.holdings.map((h) => <HoldingCard key={h.symbol} h={h} onDelete={del} />)
          )}

          {view.method_note && <p className="px-1 text-[11px] leading-relaxed text-muted-foreground">{view.method_note}</p>}
        </>
      )}
    </div>
  );
}
