import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Loader2, Search, Star, StarOff } from "lucide-react";
import { toast } from "sonner";
import { api, type PriceBar, type TradeMarker } from "@/lib/api";
import { authHeaders } from "@/lib/apiAuth";
import { cn } from "@/lib/utils";
import { CandlestickChart } from "@/components/charts/CandlestickChart";
import { Chip, EmptyState, ErrorState, LoadingState, MCard, StatTile, money, moveClass, pct, signedPct } from "../components/ui";

interface ActionPlan {
  available?: boolean;
  buy_strength_price?: number;
  buy_pullback_price?: number;
  stop_chasing_above?: number;
  stop_buying_below?: number;
  stop_loss?: number;
  take_profit?: number;
  risk_per_share?: number;
  reward_risk?: number;
  reference_label?: string;
  as_of_price?: number;
  plain_cn?: string;
  reason?: string;
}

interface StockBundle {
  symbol?: string;
  row?: { current_price?: number; calibrated_win_rate?: number | null };
  day_change?: { price?: number; change?: number; change_pct?: number; as_of?: string };
  action_plan?: ActionPlan;
  deepseek_summary?: { status?: string; summary?: string };
  company_profile?: { available?: boolean; name?: string; sector?: string; industry?: string; market_cap?: number };
}

interface Suggestion {
  symbol: string;
  name?: string;
}

function fmtCap(v?: number | null): string {
  if (v == null || !Number.isFinite(v)) return "--";
  if (v >= 1e12) return `$${(v / 1e12).toFixed(2)}万亿`;
  if (v >= 1e8) return `$${(v / 1e8).toFixed(0)}亿`;
  return `$${(v / 1e6).toFixed(0)}百万`;
}

function ActionRow({ tone, label, value, sub }: { tone: "good" | "warn" | "bad"; label: string; value: string; sub?: string }) {
  const border = tone === "good" ? "border-emerald-500/30" : tone === "warn" ? "border-amber-500/30" : "border-rose-500/30";
  const text = tone === "good" ? "text-emerald-600 dark:text-emerald-400" : tone === "warn" ? "text-amber-600 dark:text-amber-400" : "text-rose-600 dark:text-rose-400";
  return (
    <div className={cn("flex items-center justify-between gap-2 rounded-lg border bg-muted/20 px-3 py-2", border)}>
      <div className="min-w-0">
        <div className="text-xs font-medium">{label}</div>
        {sub && <div className="m-break text-[11px] text-muted-foreground">{sub}</div>}
      </div>
      <div className={cn("shrink-0 font-mono text-sm font-semibold", text)}>{value}</div>
    </div>
  );
}

function Candles({ symbol, priceLines }: { symbol: string; priceLines?: { price: number; label: string; color?: string; dashed?: boolean }[] }) {
  const [bars, setBars] = useState<PriceBar[]>([]);
  const [markers, setMarkers] = useState<TradeMarker[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(`/single-stock-overnight/${encodeURIComponent(symbol)}/candles?timeframe=daily`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((resp) => {
        if (cancelled) return;
        const conv: PriceBar[] = (resp?.candles || []).map((c: { t: number; o: number; h: number; l: number; c: number; v?: number }) => ({
          time: new Date(c.t).toISOString().slice(0, 10),
          open: c.o,
          high: c.h,
          low: c.l,
          close: c.c,
          volume: c.v ?? 0,
        }));
        setBars(conv);
        setMarkers((resp?.markers || []) as TradeMarker[]);
      })
      .catch(() => {
        if (!cancelled) {
          setBars([]);
          setMarkers([]);
        }
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  if (loading && bars.length === 0) return <div className="flex h-56 items-center justify-center text-sm text-muted-foreground"><Loader2 className="h-5 w-5 animate-spin" /></div>;
  if (bars.length < 2) return <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">暂无日线行情</div>;
  return <CandlestickChart data={bars} markers={markers} height={340} initialOverlays={["ema5", "ema10", "ema20"]} priceLines={priceLines} initialRange="3M" />;
}

export function MobileStock() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [symbol, setSymbol] = useState("");
  const [query, setQuery] = useState("");
  const [data, setData] = useState<StockBundle | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [inWatch, setInWatch] = useState(false);
  const searchTimer = useRef<number | undefined>(undefined);
  const summaryTimer = useRef<number | undefined>(undefined);
  const suppress = useRef(false);
  const didDeepLink = useRef(false);

  const pollSummary = (sym: string) => {
    if (summaryTimer.current) window.clearTimeout(summaryTimer.current);
    summaryTimer.current = window.setTimeout(async () => {
      try {
        const resp = await fetch(`/single-stock-overnight/${encodeURIComponent(sym)}/summary`, { headers: authHeaders() });
        const s = await resp.json();
        setData((prev) => (prev ? { ...prev, deepseek_summary: s } : prev));
        if (s?.status === "pending") pollSummary(sym);
      } catch {
        /* ignore */
      }
    }, 4000);
  };

  const load = async (next: string) => {
    const safe = next.trim().toUpperCase();
    if (!safe) return;
    suppress.current = true;
    setLoading(true);
    setError("");
    setSuggestOpen(false);
    setSuggestions([]);
    try {
      const resp = await fetch(`/single-stock-overnight/${encodeURIComponent(safe)}?universe=auto&period=2y`, { headers: authHeaders() });
      const payload = await resp.json();
      if (!resp.ok) throw new Error(payload.detail || "加载失败");
      setData(payload);
      setSymbol(safe);
      setQuery(safe);
      setSearchParams({ symbol: safe }, { replace: true });
      api.getWatchlistItem(safe).then((w) => setInWatch(Boolean(w))).catch(() => setInWatch(false));
      if (payload?.deepseek_summary?.status === "pending") pollSummary(safe);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  };

  // deep link ?symbol=
  useEffect(() => {
    if (didDeepLink.current) return;
    const linked = searchParams.get("symbol");
    if (linked && linked.trim()) {
      didDeepLink.current = true;
      void load(linked.trim().toUpperCase());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // debounced search
  useEffect(() => {
    if (suppress.current) {
      suppress.current = false;
      return;
    }
    if (searchTimer.current) window.clearTimeout(searchTimer.current);
    const q = query.trim();
    if (!q) {
      setSuggestions([]);
      return;
    }
    // Already showing this exact symbol (e.g. opened via deep-link) — no dropdown.
    if (q.toUpperCase() === symbol.toUpperCase()) {
      setSuggestions([]);
      setSuggestOpen(false);
      return;
    }
    searchTimer.current = window.setTimeout(async () => {
      try {
        const resp = await fetch(`/single-stock-overnight/search?q=${encodeURIComponent(q)}&limit=8`, { headers: authHeaders() });
        const payload = await resp.json();
        setSuggestions(Array.isArray(payload.items) ? payload.items : []);
        setSuggestOpen(true);
      } catch {
        setSuggestions([]);
      }
    }, 250);
    return () => {
      if (searchTimer.current) window.clearTimeout(searchTimer.current);
    };
  }, [query]);

  useEffect(() => () => {
    if (summaryTimer.current) window.clearTimeout(summaryTimer.current);
  }, []);

  const plan = (data?.action_plan ?? {}) as ActionPlan;
  const dc = data?.day_change;
  const price = dc?.price ?? data?.row?.current_price;

  const priceLines = useMemo(() => {
    const lines: { price: number; label: string; color?: string; dashed?: boolean }[] = [];
    const cur = Number(price);
    if (Number.isFinite(cur)) lines.push({ price: cur, label: "现价", color: "#3b82f6" });
    if (plan.available) {
      const g = "#10b981";
      const r = "#ef4444";
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
    return lines;
  }, [price, plan]);

  const toggleWatch = async () => {
    if (!symbol) return;
    try {
      if (inWatch) {
        await api.deleteWatchlistItem(symbol);
        setInWatch(false);
        toast.success(`已移出自选 ${symbol}`);
      } else {
        await api.addWatchlistItem({ symbol, name: String(data?.company_profile?.name || symbol), enabled: true });
        setInWatch(true);
        toast.success(`已加入自选 ${symbol}`);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "操作失败");
    }
  };

  const profile = data?.company_profile;
  const summary = data?.deepseek_summary;

  return (
    <div className="space-y-3 p-3 pb-6">
      {/* search */}
      <div className="relative">
        <div className="flex items-center gap-2 rounded-lg border bg-card px-3">
          <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value.toUpperCase())}
            onKeyDown={(e) => {
              if (e.key === "Enter") void load(query);
            }}
            onFocus={() => suggestions.length && setSuggestOpen(true)}
            placeholder="搜索代码或公司名"
            className="min-w-0 flex-1 bg-transparent py-2.5 uppercase outline-none"
            autoCapitalize="characters"
          />
          <button
            type="button"
            onClick={() => void load(query)}
            className="shrink-0 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground active:opacity-90"
          >
            查询
          </button>
        </div>
        {suggestOpen && suggestions.length > 0 && query !== symbol && (
          <div className="absolute z-20 mt-1 w-full overflow-hidden rounded-lg border bg-card shadow-lg">
            {suggestions.map((s) => (
              <button
                key={s.symbol}
                type="button"
                onClick={() => void load(s.symbol)}
                className="flex w-full items-center justify-between gap-2 border-b px-3 py-2.5 text-left text-sm active:bg-muted last:border-b-0"
              >
                <span className="font-semibold">{s.symbol}</span>
                {s.name && <span className="m-break truncate text-xs text-muted-foreground">{s.name}</span>}
              </button>
            ))}
          </div>
        )}
      </div>

      {loading && <LoadingState />}
      {error && !loading && <ErrorState message={error} onRetry={() => load(symbol || query)} />}
      {!data && !loading && !error && <EmptyState label="搜索一个标的开始研判" hint="支持代码或公司名" />}

      {data && !loading && (
        <>
          {/* snapshot */}
          <MCard className="space-y-2">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="m-break text-xl font-bold">{data.symbol || symbol}</div>
                {profile?.name && <div className="m-break text-xs text-muted-foreground">{profile.name}</div>}
              </div>
              <button
                type="button"
                onClick={toggleWatch}
                className="grid h-10 w-10 shrink-0 place-items-center rounded-lg border text-muted-foreground active:bg-muted"
                aria-label={inWatch ? "移出自选" : "加入自选"}
              >
                {inWatch ? <Star className="h-5 w-5 fill-amber-400 text-amber-400" /> : <StarOff className="h-5 w-5" />}
              </button>
            </div>
            <div className="flex items-baseline gap-2">
              <span className="font-mono text-3xl font-bold tabular-nums">{money(price, 2)}</span>
              {dc && typeof dc.change_pct === "number" && (
                <span className={cn("font-mono text-sm font-semibold", moveClass(dc.change_pct))}>
                  {signedPct(dc.change_pct)} ({(dc.change ?? 0) >= 0 ? "+" : ""}{(dc.change ?? 0).toFixed(2)})
                </span>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              {data.row?.calibrated_win_rate != null && (
                <Chip tone={data.row.calibrated_win_rate >= 0.5 ? "good" : "warn"}>校准胜率 {pct(data.row.calibrated_win_rate)}</Chip>
              )}
              {profile?.sector && <Chip tone="neutral">{profile.sector}</Chip>}
              {profile?.market_cap != null && <Chip tone="neutral">市值 {fmtCap(profile.market_cap)}</Chip>}
            </div>
            {dc?.as_of && <div className="text-[11px] text-muted-foreground">截至 {dc.as_of} 收盘（缓存，延迟~15min）</div>}
          </MCard>

          {/* action plan */}
          <MCard className="space-y-2.5">
            <div className="text-sm font-semibold">行动区间复核</div>
            {plan.available ? (
              <>
                <div className="space-y-2">
                  <ActionRow tone="good" label="强势买入复核" value={money(plan.buy_strength_price, 2)} sub="站稳且量价配合再考虑" />
                  <ActionRow tone="good" label="回踩买入复核" value={money(plan.buy_pullback_price, 2)} sub="靠近支撑企稳，不破再复核" />
                  <ActionRow tone="warn" label="停止追高" value={money(plan.stop_chasing_above, 2)} sub="超过后追高性价比下降" />
                  <ActionRow tone="bad" label="停止买入" value={money(plan.stop_buying_below, 2)} sub="跌破则取消当日买入" />
                  <ActionRow tone="bad" label="执行后止损" value={money(plan.stop_loss, 2)} sub={plan.risk_per_share != null ? `单股风险约 ${money(plan.risk_per_share, 2)}` : undefined} />
                  <ActionRow tone="good" label="执行后止盈" value={money(plan.take_profit, 2)} sub={plan.reward_risk != null ? `风报比约 ${plan.reward_risk.toFixed(2)}` : undefined} />
                </div>
                {plan.plain_cn && <p className="m-break text-xs leading-relaxed text-muted-foreground">{plan.plain_cn}</p>}
              </>
            ) : (
              <div className="rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning">{plan.reason || "暂无可用行动区间"}</div>
            )}
          </MCard>

          {/* chart */}
          <MCard className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold">行情图表</span>
              <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-300">延迟~15min</span>
            </div>
            {symbol && <Candles symbol={symbol} priceLines={priceLines} />}
          </MCard>

          {/* AI summary */}
          <MCard className="space-y-1.5">
            <div className="text-sm font-semibold">AI 复核摘要</div>
            {summary?.summary ? (
              <p className="m-break whitespace-pre-wrap text-xs leading-relaxed text-muted-foreground">{summary.summary}</p>
            ) : (
              <p className="flex items-center gap-2 text-xs text-muted-foreground">
                {summary?.status === "pending" && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                {summary?.status === "pending" ? "AI 摘要生成中…" : "暂无 AI 摘要，请按行动区间人工复核。"}
              </p>
            )}
          </MCard>

          <div className="grid grid-cols-2 gap-2">
            <StatTile label="参考基准" value={plan.reference_label || "前收"} />
            <StatTile label="基准价" value={money(plan.as_of_price, 2)} />
          </div>
        </>
      )}
    </div>
  );
}
