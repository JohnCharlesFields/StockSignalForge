import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronRight, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { toast } from "sonner";
import { api, type WatchlistMetricRow } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Chip, EmptyState, ErrorState, LoadingState, MCard, StatTile, money, moveClass, pct, signedPct } from "../components/ui";

function Row({ r, onDelete }: { r: WatchlistMetricRow; onDelete: (s: string) => void }) {
  const [confirm, setConfirm] = useState(false);
  return (
    <MCard className="space-y-2.5">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <Link to={`/m/stock?symbol=${encodeURIComponent(r.symbol)}`} className="m-break text-base font-bold text-primary">
            {r.symbol}
          </Link>
          {r.name && <div className="m-break text-xs text-muted-foreground">{r.name}</div>}
        </div>
        <div className="shrink-0 text-right">
          {r.current_price != null && <div className="font-mono text-sm font-semibold">{money(r.current_price, 2)}</div>}
          <div className={cn("font-mono text-xs", moveClass(r.day_change_pct))}>{signedPct(r.day_change_pct)}</div>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2">
        <StatTile label="20日" value={signedPct(r.return_20d)} tone={moveClass(r.return_20d)} />
        <StatTile label="60日" value={signedPct(r.return_60d)} tone={moveClass(r.return_60d)} />
        <StatTile label="相对QQQ" value={signedPct(r.relative_to_qqq_60d)} tone={moveClass(r.relative_to_qqq_60d)} />
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {r.trend_status && <Chip tone="neutral">{r.trend_status}</Chip>}
        {r.research_probability != null && <Chip tone={r.research_probability >= 0.5 ? "good" : "warn"}>研究分 {pct(r.research_probability)}</Chip>}
        {typeof r.earnings?.days_until === "number" && <Chip tone={r.earnings.days_until <= 10 ? "warn" : "neutral"}>财报{r.earnings.days_until}天</Chip>}
      </div>

      <div className="flex items-center justify-between border-t pt-2">
        {confirm ? (
          <div className="flex items-center gap-2 text-xs">
            <span className="text-muted-foreground">确认移除？</span>
            <button type="button" onClick={() => onDelete(r.symbol)} className="rounded px-2 py-1 font-medium text-rose-600">移除</button>
            <button type="button" onClick={() => setConfirm(false)} className="rounded px-2 py-1 text-muted-foreground">取消</button>
          </div>
        ) : (
          <button type="button" onClick={() => setConfirm(true)} className="inline-flex min-h-[40px] items-center gap-1 text-xs text-muted-foreground active:text-rose-600">
            <Trash2 className="h-3.5 w-3.5" /> 移除
          </button>
        )}
        <Link to={`/m/stock?symbol=${encodeURIComponent(r.symbol)}`} className="inline-flex items-center text-xs text-primary">
          研判 <ChevronRight className="h-3.5 w-3.5" />
        </Link>
      </div>
    </MCard>
  );
}

export function MobileWatchlist() {
  const [items, setItems] = useState<WatchlistMetricRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [symbol, setSymbol] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => {
    setLoading(true);
    setError("");
    api
      .getWatchlist(true, true)
      .then((r) => setItems(Array.isArray(r.items) ? r.items : []))
      .catch((e) => setError(e instanceof Error ? e.message : "无法加载自选池"))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  const add = async () => {
    const s = symbol.trim().toUpperCase();
    if (!s) {
      toast.error("请输入代码");
      return;
    }
    setBusy(true);
    try {
      await api.addWatchlistItem({ symbol: s, name: s, enabled: true });
      toast.success(`已加入 ${s}`);
      setSymbol("");
      setShowAdd(false);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "添加失败");
    } finally {
      setBusy(false);
    }
  };

  const del = async (s: string) => {
    try {
      await api.deleteWatchlistItem(s);
      toast.success(`已移除 ${s}`);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "移除失败");
    }
  };

  return (
    <div className="space-y-3 p-3 pb-6">
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={() => setShowAdd((v) => !v)}
          className="inline-flex min-h-[40px] items-center gap-1.5 rounded-lg border border-primary/40 bg-primary/10 px-3 text-sm font-medium text-primary active:bg-primary/20"
        >
          {showAdd ? <X className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
          {showAdd ? "取消" : "加自选"}
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
        <MCard className="flex items-center gap-2">
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && add()}
            placeholder="代码 (如 NVDA)"
            className="min-w-0 flex-1 rounded-lg border bg-background px-3 py-2.5 uppercase outline-none focus:border-primary"
            autoCapitalize="characters"
          />
          <button
            type="button"
            onClick={add}
            disabled={busy}
            className="shrink-0 rounded-lg bg-primary px-4 py-2.5 text-sm font-semibold text-primary-foreground active:opacity-90 disabled:opacity-60"
          >
            加入
          </button>
        </MCard>
      )}

      {loading && items.length === 0 && <LoadingState />}
      {error && items.length === 0 && <ErrorState message={error} onRetry={load} />}
      {!loading && !error && items.length === 0 && <EmptyState label="自选池为空" hint="点「加自选」添加关注的标的" />}

      {items.map((r) => (
        <Row key={r.symbol} r={r} onDelete={del} />
      ))}
    </div>
  );
}
