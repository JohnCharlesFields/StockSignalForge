import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlertTriangle, Check, ChevronLeft, ChevronRight, ExternalLink, List, Newspaper, RefreshCcw, X } from "lucide-react";
import { api, type PremarketNewsAutoStatus, type PremarketNewsItem } from "@/lib/api";
import { cn } from "@/lib/utils";

const pct = (value?: number | null, digits = 1) =>
  value === null || value === undefined || Number.isNaN(Number(value)) ? "--" : `${(Number(value) * 100).toFixed(digits)}%`;

const money = (value?: number | null) =>
  value === null || value === undefined || Number.isNaN(Number(value)) ? "--" : `$${Number(value).toFixed(2)}`;

const compactDate = (value?: string | null) => {
  if (!value) return "--";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value.slice(0, 16);
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
};

const sentimentLabel = (value?: string) => {
  if (value === "positive") return "正面";
  if (value === "negative") return "负面";
  return "中性";
};

const sentimentClass = (value?: string) => {
  if (value === "positive") return "border-emerald-500/35 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
  if (value === "negative") return "border-rose-500/35 bg-rose-500/10 text-rose-700 dark:text-rose-300";
  return "border-slate-500/30 bg-slate-500/10 text-muted-foreground";
};

const scoreClass = (score?: number | null) => {
  const s = Number(score || 0);
  if (s >= 80) return "text-rose-600 dark:text-rose-300";
  if (s >= 60) return "text-amber-600 dark:text-amber-300";
  return "text-muted-foreground";
};

const tierClass = (tone?: string) => {
  if (tone === "good") return "border-emerald-500/35 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
  if (tone === "warn") return "border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300";
  if (tone === "caution") return "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300";
  return "border-slate-500/30 bg-slate-500/10 text-muted-foreground";
};

function relatedTickers(item: PremarketNewsItem): string[] {
  const self = String(item.symbol || "").toUpperCase();
  return (item.alternatives || [])
    .filter((a) => (a.reason || "").includes("同一新闻提及") && a.symbol && a.symbol.toUpperCase() !== self)
    .map((a) => a.symbol.toUpperCase())
    .slice(0, 6);
}

function StatusLine({ status }: { status: PremarketNewsAutoStatus | null }) {
  if (!status) return null;
  const interval = status.interval_seconds ? Math.round(status.interval_seconds / 60) : 15;
  const failed = status.status === "error";
  const text = !status.enabled
    ? "后台自动刷新已关闭"
    : failed
      ? `后台每 ${interval} 分钟自动刷新 · 暂时失败，当前仍显示已入库新闻`
      : `后台每 ${interval} 分钟自动刷新 · ${status.status || "idle"}`;
  return (
    <div className={cn("text-xs", failed ? "text-amber-600 dark:text-amber-300" : "text-muted-foreground")}>
      {text}
      {status.last_success_at ? ` · 上次成功 ${compactDate(status.last_success_at)}` : ""}
      {status.last_result?.matched_item_count !== undefined ? ` · 命中 ${status.last_result.matched_item_count}` : ""}
      {failed && status.last_error ? ` · ${status.last_error.split(":")[0]}` : ""}
    </div>
  );
}

function Metric({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className="rounded-md border bg-background/55 px-3 py-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-lg font-semibold", className)}>{value}</div>
    </div>
  );
}

function NewsCard({ item, index, total }: { item: PremarketNewsItem; index: number; total: number }) {
  const title = item.title_cn || item.title_original;
  const desc = item.description_cn || item.description_original;
  const score = Number(item.adjusted_importance_score || item.importance_score || 0);
  const highImpact = score >= 75 || Math.abs(Number(item.estimated_gap_pct || 0)) >= 0.04;
  const poolHint = item.source_pools?.length ? item.source_pools.slice(0, 2).join(" / ") : "";
  const priceMissing = item.current_price === null || item.current_price === undefined;
  const isMarket = item.symbol === "MARKET";

  return (
    <section className="flex w-full flex-1 flex-col rounded-xl border bg-card shadow-sm">
      <div className="border-b p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            {isMarket ? (
              <div className="inline-flex flex-wrap items-baseline gap-2">
                <span className="text-3xl font-bold tracking-tight text-purple-600 dark:text-purple-300">大盘 · 全市场</span>
                <span className="text-lg text-muted-foreground">{item.company_name || "影响全市场的宏观新闻"}</span>
              </div>
            ) : (
              <Link
                to={`/single-stock-overnight?symbol=${encodeURIComponent(item.symbol)}`}
                className="inline-flex flex-wrap items-baseline gap-2 rounded-md outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-primary"
                title="打开个股研判"
              >
                <span className="text-3xl font-bold tracking-tight">{item.symbol}</span>
                <span className="text-lg text-muted-foreground">{item.company_name || item.symbol}</span>
              </Link>
            )}
            <div className="mt-3 flex flex-wrap gap-2">
              {isMarket && <span className="rounded-full border border-purple-500/45 bg-purple-500/10 px-2.5 py-1 text-xs font-medium text-purple-700 dark:text-purple-300">全市场·大盘</span>}
              <span className={cn("rounded-full border px-2.5 py-1 text-xs font-medium", sentimentClass(item.sentiment))}>
                {sentimentLabel(item.sentiment)}
              </span>
              {item.event_type_cn && <span className="rounded-full border px-2.5 py-1 text-xs font-medium">{item.event_type_cn}</span>}
              {item.sector_effect && <span className="rounded-full border border-sky-500/35 bg-sky-500/10 px-2.5 py-1 text-xs font-medium text-sky-700 dark:text-sky-300">可能带动板块</span>}
              {highImpact && <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs font-medium text-amber-700 dark:text-amber-300">开盘冲击较高</span>}
              {item.source_tier_cn && (
                <span className={cn("rounded-full border px-2.5 py-1 text-xs font-medium", tierClass(item.source_tier_tone))} title="来源类型(启发式)：权威媒体/一般媒体/散户荐股/企业通稿——非内容真伪判断">{item.source_tier_cn}</span>
              )}
            </div>
            {relatedTickers(item).length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
                <span className="text-muted-foreground">本新闻涉及：</span>
                <a href={`/single-stock-overnight?symbol=${item.symbol}`} className="rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 font-medium text-primary hover:underline">{item.symbol}</a>
                {relatedTickers(item).map((s) => (
                  <a key={s} href={`/single-stock-overnight?symbol=${s}`} className="rounded-full border border-primary/30 bg-primary/5 px-2 py-0.5 font-medium text-primary hover:underline">{s}</a>
                ))}
              </div>
            )}
            <div className="mt-3 text-sm text-muted-foreground">
              {item.publisher || "Unknown"} · {compactDate(item.published_utc)} · 来源 {item.source || "massive:news"}
            </div>
            {poolHint && <div className="mt-1 text-xs text-muted-foreground">来源池：{poolHint}</div>}
            {!isMarket && priceMissing && (
              <div className="mt-2 rounded-md border border-amber-500/35 bg-amber-500/10 px-2 py-1 text-xs text-amber-700 dark:text-amber-300">
                行情待补齐：新闻已命中股票池，但当前行情源暂未返回价格。
              </div>
            )}
          </div>
          <div className="rounded-full border px-3 py-1 text-sm text-muted-foreground">
            {Math.min(index + 1, total)} / {total || 0}
          </div>
        </div>

        {isMarket ? (
          <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Metric label="预计大盘影响" value={pct(item.estimated_gap_pct)} className={Number(item.estimated_gap_pct || 0) >= 0 ? "text-emerald-600" : "text-rose-600"} />
            <Metric label="波动带宽" value={`±${pct(item.impact_band_pct)}`} />
            <Metric label="大盘冲击分" value={score.toFixed(1)} className={scoreClass(score)} />
            <Metric label="范围" value="全市场 / 大盘" />
          </div>
        ) : (
        <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Metric label="现价" value={priceMissing ? "待补齐" : money(item.current_price)} className={priceMissing ? "text-amber-600" : ""} />
          <Metric label="前收" value={money(item.previous_close)} />
          <Metric
            label="涨跌"
            value={pct(item.change_pct)}
            className={Number(item.change_pct || 0) >= 0 ? "text-emerald-600" : "text-rose-600"}
          />
          <Metric label="冲击分" value={score.toFixed(1)} className={scoreClass(score)} />
          <Metric label="赛道" value={item.sector_name || "--"} />
        </div>
        )}
      </div>

      <div className="grid flex-1 gap-5 overflow-y-auto p-5 lg:grid-cols-[1.35fr_0.75fr]">
        <div className="min-w-0 space-y-4">
          <div>
            <h3 className="text-2xl font-semibold leading-9">{title}</h3>
            {desc && <p className="mt-3 text-base leading-7 text-muted-foreground">{desc}</p>}
          </div>

          <details className="rounded-lg border bg-muted/20 p-4">
            <summary className="cursor-pointer text-sm font-medium text-muted-foreground">英文原文与出处</summary>
            <div className="mt-3 space-y-3 text-sm leading-6">
              <p className="font-semibold text-foreground">{item.title_original}</p>
              {item.description_original && <p className="text-muted-foreground">{item.description_original}</p>}
              {item.article_url && (
                <a className="inline-flex items-center gap-1 text-primary hover:underline" href={item.article_url} target="_blank" rel="noreferrer">
                  打开原文 <ExternalLink className="h-4 w-4" />
                </a>
              )}
            </div>
          </details>
        </div>

        <aside className="space-y-4">
          <div className="rounded-lg border bg-muted/20 p-4">
            <div className="grid grid-cols-2 gap-3">
              <Metric
                label="预计开盘影响"
                value={pct(item.estimated_gap_pct)}
                className={Number(item.estimated_gap_pct || 0) >= 0 ? "text-emerald-600" : "text-rose-600"}
              />
              <Metric label="波动带宽" value={`±${pct(item.impact_band_pct)}`} />
              <Metric label="ATR 代理" value={pct(item.atr_pct)} />
              <Metric label="新闻方向" value={sentimentLabel(item.sentiment)} className={item.sentiment === "negative" ? "text-rose-600" : item.sentiment === "positive" ? "text-emerald-600" : ""} />
            </div>
            {item.sentiment_reasoning && <p className="mt-4 text-sm leading-6 text-muted-foreground">{item.sentiment_reasoning}</p>}
          </div>

          <div className="rounded-lg border bg-muted/20 p-4">
            <div className="text-sm font-semibold">跳空过大时的延迟备选</div>
            {item.alternatives?.length ? (
              <div className="mt-3 flex flex-wrap gap-2">
                {item.alternatives.map((alt) => (
                  <a key={alt.symbol} href={`/single-stock-overnight?symbol=${alt.symbol}`} title={alt.action_hint || alt.reason} className="rounded-full border bg-background px-3 py-1 text-sm font-medium text-primary hover:bg-primary/10 hover:underline">
                    {alt.symbol}
                  </a>
                ))}
              </div>
            ) : (
              <div className="mt-3 text-sm text-muted-foreground">暂无明确备选。</div>
            )}
          </div>
        </aside>
      </div>
    </section>
  );
}

const POS_KEY = "premarket_swipe_news_id";

export function PremarketNewsSwipe() {
  const [searchParams] = useSearchParams();
  const [items, setItems] = useState<PremarketNewsItem[]>([]);
  const [active, setActive] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState<PremarketNewsAutoStatus | null>(null);
  const [latestNewsAt, setLatestNewsAt] = useState<string | null>(null);

  const current = items[active];
  const resumeNewsId = searchParams.get("resume");

  const load = async (keepActive = true) => {
    setError("");
    try {
      const [queue, auto] = await Promise.all([
        api.getPremarketNews({ reviewed: "unreviewed", limit: 120, min_score: 0, recent_days: 7 }),
        api.getPremarketNewsAutoStatus().catch(() => null),
      ]);
      setItems(queue.items || []);
      setStatus(auto);
      setLatestNewsAt(queue.latest_update?.latest_news || null);
      setActive((prev) => {
        if (!queue.items?.length) return 0;
        // 1) explicit ?resume= from the list page wins.
        if (resumeNewsId) {
          const resumeIndex = queue.items.findIndex((x) => x.news_id === resumeNewsId);
          if (resumeIndex >= 0) return resumeIndex;
        }
        // 2) restore the last-viewed card across remounts / tab returns / reloads.
        const savedId = typeof localStorage !== "undefined" ? localStorage.getItem(POS_KEY) : null;
        if (savedId) {
          const savedIndex = queue.items.findIndex((x) => x.news_id === savedId);
          if (savedIndex >= 0) return savedIndex;
        }
        if (!keepActive) return 0;
        // 3) periodic refresh: hold the current card by id.
        const oldId = items[prev]?.news_id;
        const nextIndex = oldId ? queue.items.findIndex((x) => x.news_id === oldId) : 0;
        return nextIndex >= 0 ? nextIndex : 0;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(false);
    const timer = window.setInterval(() => {
      if (!document.hidden) void load(true);  // don't churn / reset while tab is in background
    }, 60_000);
    return () => window.clearInterval(timer);
  }, []);

  // Remember where the user is so a remount / tab return / reload resumes here.
  useEffect(() => {
    if (current?.news_id && typeof localStorage !== "undefined") {
      localStorage.setItem(POS_KEY, current.news_id);
    }
  }, [current?.news_id]);

  const manualRefresh = async () => {
    setRefreshing(true);
    setError("");
    try {
      await api.refreshPremarketNews({ translate_top: 100, max_pages: 3, refresh_universe: false });
      await load(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshing(false);
    }
  };

  const mark = async (decision: "important" | "not_important" | "skip") => {
    if (!current) return;
    const id = current.news_id;
    setItems((prev) => prev.filter((item) => item.news_id !== id));
    setActive((prev) => Math.max(0, Math.min(prev, items.length - 2)));
    try {
      await api.markPremarketNewsFeedback(id, { decision });
      void load(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      void load(true);
    }
  };

  const move = (direction: -1 | 1) => {
    setActive((prev) => Math.max(0, Math.min(prev + direction, items.length - 1)));
  };

  const stats = useMemo(() => {
    const urgent = items.filter((x) => Number(x.adjusted_importance_score || x.importance_score || 0) >= 75).length;
    return { urgent, total: items.length };
  }, [items]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <header className="shrink-0 border-b bg-card/75">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div>
            <div className="flex items-center gap-2">
              <Newspaper className="h-5 w-5 text-primary" />
              <h1 className="text-xl font-semibold">盘前新闻</h1>
              {stats.urgent > 0 && (
                <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-xs text-amber-700 dark:text-amber-300">
                  {stats.urgent} 条高冲击
                </span>
              )}
            </div>
            <StatusLine status={status} />
            <div className="text-xs text-muted-foreground">
              近 7 天未读 · 最新入库新闻 {compactDate(latestNewsAt)}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Link
              to={current ? `/premarket-news/list?resume=${encodeURIComponent(current.news_id)}` : "/premarket-news/list"}
              className="inline-flex h-9 items-center gap-2 rounded-md border bg-background px-3 text-sm font-medium hover:bg-muted"
            >
              <List className="h-4 w-4" />
              新闻清单
            </Link>
          <button
            type="button"
            onClick={manualRefresh}
            disabled={refreshing}
            className="inline-flex h-9 items-center gap-2 rounded-md border bg-background px-3 text-sm font-medium hover:bg-muted disabled:opacity-60"
          >
            <RefreshCcw className={cn("h-4 w-4", refreshing && "animate-spin")} />
            立即刷新
          </button>
          </div>
        </div>
      </header>

      <main className="flex min-h-0 flex-1 flex-col overflow-y-auto px-4 py-4">
        {error && (
          <div className="mx-auto mb-3 flex w-full max-w-5xl items-start gap-2 rounded-lg border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-700 dark:text-rose-300">
            <AlertTriangle className="mt-0.5 h-4 w-4" />
            <span>{error}</span>
          </div>
        )}

        {loading ? (
          <div className="mx-auto grid min-h-[60vh] w-full max-w-5xl place-items-center rounded-xl border bg-card text-muted-foreground">
            加载盘前新闻...
          </div>
        ) : current ? (
          <div className="mx-auto grid w-full max-w-[1760px] flex-1 grid-cols-[72px_minmax(0,1fr)_72px] items-stretch gap-4 px-1">
            <button
              type="button"
              onClick={() => move(-1)}
              disabled={active <= 0}
              className="flex items-center justify-center rounded-xl border bg-card text-muted-foreground shadow-sm transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-35"
              title="上一条"
              aria-label="上一条新闻"
            >
              <ChevronLeft className="h-10 w-10" />
            </button>
            <NewsCard item={current} index={active} total={items.length} />
            <button
              type="button"
              onClick={() => move(1)}
              disabled={active >= items.length - 1}
              className="flex items-center justify-center rounded-xl border bg-card text-muted-foreground shadow-sm transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-35"
              title="下一条"
              aria-label="下一条新闻"
            >
              <ChevronRight className="h-10 w-10" />
            </button>
          </div>
        ) : (
          <div className="mx-auto grid min-h-[60vh] w-full max-w-5xl place-items-center rounded-xl border bg-card p-8 text-center text-muted-foreground">
            <div>
              <div className="text-lg font-semibold text-foreground">当前没有待勾选新闻</div>
              <p className="mt-2">近 7 天暂无待勾选新闻。历史未读可在新闻清单查看。</p>
            </div>
          </div>
        )}
      </main>

      <footer className="shrink-0 border-t bg-card/95 px-4 py-3 backdrop-blur">
        <div className="mx-auto grid max-w-5xl grid-cols-2 gap-4">
          <button
            type="button"
            disabled={!current}
            onClick={() => void mark("important")}
            className="flex min-h-16 items-center justify-center rounded-xl bg-emerald-600 text-white shadow-sm transition hover:bg-emerald-700 disabled:opacity-40"
            title="重要，加入自选池"
          >
            <Check className="h-11 w-11" />
          </button>
          <button
            type="button"
            disabled={!current}
            onClick={() => void mark("not_important")}
            className="flex min-h-16 items-center justify-center rounded-xl bg-rose-600 text-white shadow-sm transition hover:bg-rose-700 disabled:opacity-40"
            title="不重要，跳过"
          >
            <X className="h-11 w-11" />
          </button>
        </div>
      </footer>
    </div>
  );
}
