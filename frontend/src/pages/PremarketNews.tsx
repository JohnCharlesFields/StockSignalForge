import { Fragment, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlertTriangle, Check, ExternalLink, Newspaper, RefreshCcw, Star, X } from "lucide-react";
import { api, type PremarketNewsItem, type PremarketNewsRefreshResponse } from "@/lib/api";
import { cn } from "@/lib/utils";

const pct = (value?: number | null, digits = 1) =>
  value === null || value === undefined || Number.isNaN(Number(value)) ? "--" : `${(Number(value) * 100).toFixed(digits)}%`;

const money = (value?: number | null) =>
  value === null || value === undefined || Number.isNaN(Number(value)) ? "--" : `$${Number(value).toFixed(2)}`;

const toneClass = (sentiment?: string) => {
  if (sentiment === "positive") return "border-emerald-500/35 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
  if (sentiment === "negative") return "border-rose-500/35 bg-rose-500/10 text-rose-700 dark:text-rose-300";
  return "border-slate-400/30 bg-slate-500/10 text-muted-foreground";
};

const impactClass = (score?: number) => {
  const s = Number(score || 0);
  if (s >= 75) return "text-rose-600 dark:text-rose-300";
  if (s >= 55) return "text-amber-600 dark:text-amber-300";
  return "text-muted-foreground";
};

const tierClass = (tone?: string) => {
  if (tone === "good") return "border-emerald-500/35 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
  if (tone === "warn") return "border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300";
  if (tone === "caution") return "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300";
  return "border-slate-400/30 bg-slate-500/10 text-muted-foreground";
};

function relatedTickers(item: { symbol?: string; alternatives?: { symbol: string; reason?: string }[] }): string[] {
  const self = String(item.symbol || "").toUpperCase();
  return (item.alternatives || [])
    .filter((a) => (a.reason || "").includes("同一新闻提及") && a.symbol && a.symbol.toUpperCase() !== self)
    .map((a) => a.symbol.toUpperCase())
    .slice(0, 5);
}

function compactDate(value?: string | null) {
  if (!value) return "--";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value.slice(0, 16);
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function NewsCard({
  item,
  onFeedback,
}: {
  item: PremarketNewsItem;
  onFeedback: (item: PremarketNewsItem, decision: "important" | "not_important" | "skip") => void;
}) {
  const titleCn = item.title_cn || item.title_original;
  const descCn = item.description_cn || item.description_original;
  const bigGap = Math.abs(Number(item.estimated_gap_pct || 0)) >= 0.04 || Number(item.adjusted_importance_score || 0) >= 75;
  const poolHint = item.source_pools?.length ? item.source_pools.slice(0, 2).join(" / ") : "";
  const priceMissing = item.current_price === null || item.current_price === undefined;
  return (
    <article className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            {item.symbol === "MARKET" ? (
              <span className="text-lg font-semibold text-purple-600 dark:text-purple-300">大盘 · 全市场</span>
            ) : (
              <Link to={`/single-stock-overnight?symbol=${item.symbol}`} className="text-lg font-semibold text-primary hover:underline">{item.symbol}</Link>
            )}
            <span className="text-sm text-muted-foreground">{item.company_name}</span>
            <span className={cn("rounded-full border px-2 py-0.5 text-xs", toneClass(item.sentiment))}>
              {item.sentiment === "unreviewed" ? "待研判" : item.sentiment === "positive" ? "正面" : item.sentiment === "negative" ? "负面" : "中性"}
            </span>
            {item.event_type_cn && <span className="rounded-full border px-2 py-0.5 text-xs text-muted-foreground">{item.event_type_cn}</span>}
            {item.sector_effect && (
              <span className="rounded-full border border-sky-500/35 bg-sky-500/10 px-2 py-0.5 text-xs text-sky-700 dark:text-sky-300">
                可能带动板块
              </span>
            )}
            {bigGap && (
              <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-xs text-amber-700 dark:text-amber-300">
                开盘冲击较高
              </span>
            )}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            {item.publisher || "Unknown"} · {item.source_provenance?.reported_time || compactDate(item.published_utc)} · 来源 {item.source || "massive:news"}
          </div>
          {item.source_provenance?.note && <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">{item.source_provenance.note}</p>}
          {poolHint && <div className="mt-1 text-xs text-muted-foreground">来源池：{poolHint}</div>}
          {item.quote_as_of && <p className="text-xs text-muted-foreground">日收盘参考 · {item.quote_as_of} · {item.quote_source} · 非实时价</p>}
          {!!item.attribution?.channels?.length && <p className="text-xs text-muted-foreground">销售渠道：{item.attribution.channels.join(" / ")}，非新闻主角</p>}
          {priceMissing && (
            <div className="mt-2 rounded-md border border-amber-500/35 bg-amber-500/10 px-2 py-1 text-xs text-amber-700 dark:text-amber-300">
              行情后台补齐中；暂未取得可核验价格，保留新闻，不用其他股票行情代替。
            </div>
          )}
        </div>
        {item.symbol === "MARKET" ? (
          <div className="grid grid-cols-2 gap-3 text-right text-xs">
            <div>
              <div className="text-muted-foreground">预计大盘影响</div>
              <div className={cn("font-semibold", Number(item.estimated_gap_pct || 0) >= 0 ? "text-emerald-600" : "text-rose-600")}>
                {pct(item.estimated_gap_pct)}（±{pct(item.impact_band_pct)}）
              </div>
            </div>
            <div>
              <div className="text-muted-foreground">大盘冲击分</div>
              <div className={cn("font-semibold", impactClass(item.adjusted_importance_score))}>
                {Number(item.adjusted_importance_score || item.importance_score || 0).toFixed(1)}
              </div>
            </div>
          </div>
        ) : (
        <div className="grid grid-cols-4 gap-3 text-right text-xs">
          <div>
            <div className="text-muted-foreground">现价</div>
            <div className={cn("font-semibold", priceMissing && "text-amber-600")}>{priceMissing ? "待补齐" : money(item.current_price)}</div>
          </div>
          <div>
            <div className="text-muted-foreground">前收</div>
            <div className="font-semibold">{money(item.previous_close)}</div>
          </div>
          <div>
            <div className="text-muted-foreground">涨跌</div>
            <div className={cn("font-semibold", Number(item.change_pct || 0) >= 0 ? "text-emerald-600" : "text-rose-600")}>{pct(item.change_pct)}</div>
          </div>
          <div>
            <div className="text-muted-foreground">冲击分</div>
            <div className={cn("font-semibold", impactClass(item.adjusted_importance_score))}>
              {Number(item.adjusted_importance_score || item.importance_score || 0).toFixed(1)}
            </div>
          </div>
        </div>
        )}
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-[1.35fr_0.9fr]">
        <div>
          <h3 className="text-base font-semibold leading-6">{titleCn}</h3>
          {descCn && <p className="mt-2 text-sm leading-6 text-muted-foreground">{descCn}</p>}
          <details className="mt-3 rounded-md border bg-muted/20 p-3 text-xs">
            <summary className="cursor-pointer text-muted-foreground">{item.source === "gildata:news" ? "聚源资讯片段与出处" : "英文原文与出处"}</summary>
            <div className="mt-2 space-y-2">
              <p className="font-medium text-foreground">{item.title_original}</p>
              {item.description_original && <p className="leading-5 text-muted-foreground">{item.description_original}</p>}
              {item.article_url && (
                <a className="inline-flex items-center gap-1 text-primary hover:underline" href={item.article_url} target="_blank" rel="noreferrer">
                  打开原文 <ExternalLink className="h-3 w-3" />
                </a>
              )}
            </div>
          </details>
        </div>
        <div className="rounded-md border bg-muted/20 p-3">
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <div className="text-xs text-muted-foreground">预计开盘影响</div>
              <div className={cn("font-semibold", Number(item.estimated_gap_pct || 0) >= 0 ? "text-emerald-600" : "text-rose-600")}>
                {item.estimated_gap_pct == null ? "不足以量化方向" : pct(item.estimated_gap_pct)}
              </div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">波动带宽</div>
              <div className="font-semibold">±{pct(item.impact_band_pct)}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">ATR 代理</div>
              <div className="font-semibold">{pct(item.atr_pct)}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">赛道</div>
              <div className="font-semibold">{item.sector_name || "--"}</div>
            </div>
          </div>
          {item.sentiment_reasoning && <p className="mt-3 text-xs leading-5 text-muted-foreground">{item.sentiment_reasoning}</p>}
          {item.alternatives?.length ? (
            <div className="mt-3">
              <div className="text-xs font-medium text-muted-foreground">跳空过大时的延迟备选</div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {item.alternatives.map((alt) => (
                  <Link key={alt.symbol} to={`/single-stock-overnight?symbol=${alt.symbol}`} title={alt.action_hint || alt.reason} className="rounded-full border px-2 py-0.5 text-xs text-primary hover:bg-primary/10 hover:underline">
                    {alt.symbol}
                  </Link>
                ))}
              </div>
            </div>
          ) : null}
          <div className="mt-4 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => onFeedback(item, "important")}
              className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-700"
            >
              <Star className="h-4 w-4" /> 重要，加入自选
            </button>
            <button
              type="button"
              onClick={() => onFeedback(item, "not_important")}
              className="inline-flex items-center gap-1 rounded-md border px-3 py-2 text-sm hover:bg-muted"
            >
              <X className="h-4 w-4" /> 不重要
            </button>
            <button
              type="button"
              onClick={() => onFeedback(item, "skip")}
              className="inline-flex items-center gap-1 rounded-md border px-3 py-2 text-sm text-muted-foreground hover:bg-muted"
            >
              <Check className="h-4 w-4" /> 稍后
            </button>
          </div>
        </div>
      </div>
    </article>
  );
}

export function PremarketNews() {
  const [searchParams] = useSearchParams();
  const [items, setItems] = useState<PremarketNewsItem[]>([]);
  const [reviewed, setReviewed] = useState("unreviewed");
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [sourceWarning, setSourceWarning] = useState("");
  const [refreshResult, setRefreshResult] = useState<PremarketNewsRefreshResponse | null>(null);
  const [minScore, setMinScore] = useState(0);
  const [recentDays, setRecentDays] = useState(7);
  const [selected, setSelected] = useState<PremarketNewsItem | null>(null);
  const resumeNewsId = searchParams.get("resume");
  const continuePath = resumeNewsId ? `/premarket-news?resume=${encodeURIComponent(resumeNewsId)}` : "/premarket-news";

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const res = await api.getPremarketNews({ reviewed, limit: 120, min_score: minScore, recent_days: recentDays });
      setItems(res.items || []);
      if (res.items?.some((item) => item.symbol !== "MARKET" && (!item.current_price || !item.quote_as_of || item.sentiment === "unreviewed"))) {
        void api.enrichPremarketNews().catch(() => undefined);
      }
      setSourceWarning(res.source_mix_warning || "");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [reviewed, minScore, recentDays]);

  const refresh = async () => {
    setRefreshing(true);
    setError("");
    try {
      const res = await api.refreshPremarketNews({ translate_top: 100, max_pages: 3, refresh_universe: false });
      setRefreshResult(res);
      await load();
      if (res.error) setError(res.error);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshing(false);
    }
  };

  const mark = async (item: PremarketNewsItem, decision: "important" | "not_important" | "skip") => {
    try {
      await api.markPremarketNewsFeedback(item.news_id, { decision });
      setItems((prev) => prev.filter((x) => x.news_id !== item.news_id));
      setSelected((prev) => (prev?.news_id === item.news_id ? null : prev));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const counts = useMemo(() => {
    const pos = items.filter((x) => x.sentiment === "positive").length;
    const neg = items.filter((x) => x.sentiment === "negative").length;
    const sector = items.filter((x) => x.sector_effect).length;
    return { pos, neg, sector };
  }, [items]);

  const boardRows = useMemo(
    () =>
      [...items].sort(
        (a, b) =>
          Number(b.adjusted_importance_score || b.importance_score || 0) -
          Number(a.adjusted_importance_score || a.importance_score || 0),
      ),
    [items],
  );

  return (
    <div className="min-h-screen bg-background">
      <div className="border-b bg-card/70">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-4">
          <div>
            <div className="flex items-center gap-2">
              <Newspaper className="h-5 w-5 text-primary" />
              <h1 className="text-xl font-semibold">盘前新闻清单</h1>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              默认展示近 7 天新闻；历史未读仍保留，可切换查看。页面读取不自动抓取，点击更新才刷新。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Link
              to={continuePath}
              className="inline-flex h-9 items-center rounded-md border bg-background px-3 text-sm font-medium hover:bg-muted"
            >
              继续勾选
            </Link>
            <select
              value={recentDays}
              onChange={(e) => setRecentDays(Number(e.target.value))}
              className="h-9 rounded-md border bg-background px-3 text-sm"
              aria-label="新闻时间范围"
            >
              <option value={7}>近 7 天</option>
              <option value={0}>全部历史</option>
            </select>
            <select
              value={reviewed}
              onChange={(e) => setReviewed(e.target.value)}
              className="h-9 rounded-md border bg-background px-3 text-sm"
            >
              <option value="unreviewed">只看未读</option>
              <option value="all">全部</option>
              <option value="important">已标重要</option>
              <option value="not_important">已标不重要</option>
            </select>
            <select
              value={minScore}
              onChange={(e) => setMinScore(Number(e.target.value))}
              className="h-9 rounded-md border bg-background px-3 text-sm"
            >
              <option value={0}>全部分数</option>
              <option value={45}>45 分以上</option>
              <option value={60}>60 分以上</option>
              <option value={75}>75 分以上</option>
            </select>
            <button
              type="button"
              onClick={refresh}
              disabled={refreshing}
              className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-medium text-primary-foreground disabled:opacity-60"
            >
              <RefreshCcw className={cn("h-4 w-4", refreshing && "animate-spin")} />
              更新隔夜新闻
            </button>
          </div>
        </div>
      </div>

      <main className="mx-auto max-w-7xl space-y-4 px-4 py-4">
        {sourceWarning && (
          <div role="status" className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-300">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{sourceWarning}</span>
          </div>
        )}
        <section className="grid gap-3 md:grid-cols-4">
          <div className="rounded-lg border bg-card p-3">
            <div className="text-xs text-muted-foreground">当前队列</div>
            <div className="mt-1 text-2xl font-semibold">{loading ? "--" : items.length}</div>
          </div>
          <div className="rounded-lg border bg-card p-3">
            <div className="text-xs text-muted-foreground">正面 / 负面</div>
            <div className="mt-1 text-2xl font-semibold text-emerald-600">{counts.pos}<span className="text-muted-foreground"> / </span><span className="text-rose-600">{counts.neg}</span></div>
          </div>
          <div className="rounded-lg border bg-card p-3">
            <div className="text-xs text-muted-foreground">可能板块扩散</div>
            <div className="mt-1 text-2xl font-semibold">{counts.sector}</div>
          </div>
          <div className="rounded-lg border bg-card p-3">
            <div className="text-xs text-muted-foreground">股票池覆盖</div>
            <div className="mt-1 text-sm font-semibold">
              {refreshResult ? `${refreshResult.universe_symbol_count || 0} 个标的 · 命中 ${refreshResult.matched_item_count || 0}` : "读取已入库结果"}
            </div>
          </div>
        </section>

        {refreshResult?.source_audit && (
          <div className="rounded-lg border border-sky-500/30 bg-sky-500/10 p-3 text-sm text-sky-800 dark:text-sky-200">
            Google Finance 已评估为网页参考源；当前主抓取源为 Massive/Polygon News API。英文新闻会保留原文、出处和机翻中文。
          </div>
        )}

        {error && (
          <div className="flex items-start gap-2 rounded-lg border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-700 dark:text-rose-300">
            <AlertTriangle className="mt-0.5 h-4 w-4" />
            <span>{error}</span>
          </div>
        )}

        {loading ? (
          <div className="rounded-lg border bg-card p-8 text-center text-muted-foreground">加载新闻队列...</div>
        ) : boardRows.length ? (
          <div className="overflow-x-auto rounded-lg border bg-card shadow-sm">
            <div className="grid min-w-[1050px] grid-cols-[120px_minmax(240px,1fr)_90px_120px_120px_120px_150px] gap-3 border-b bg-muted/50 px-3 py-2 text-xs font-medium text-muted-foreground">
              <div>标的</div>
              <div>新闻要点 / 标签</div>
              <div>冲击分</div>
              <div>开盘影响</div>
              <div>行情</div>
              <div>发布时间</div>
              <div>赛道 / 来源</div>
            </div>
            <div className="divide-y">
              {boardRows.map((item) => {
                const score = Number(item.adjusted_importance_score || item.importance_score || 0);
                const title = item.title_cn || item.title_original;
                const bigGap = Math.abs(Number(item.estimated_gap_pct || 0)) >= 0.04 || score >= 75;
                const active = selected?.news_id === item.news_id;
                const priceMissing = item.current_price === null || item.current_price === undefined;
                const poolHint = item.source_pools?.length ? item.source_pools[0] : "";
                return (
                  <Fragment key={item.news_id}>
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => setSelected((prev) => (prev?.news_id === item.news_id ? null : item))}
                    onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSelected((prev) => (prev?.news_id === item.news_id ? null : item)); } }}
                    className={cn(
                      "grid min-w-[1050px] w-full cursor-pointer grid-cols-[120px_minmax(240px,1fr)_90px_120px_120px_120px_150px] items-center gap-3 px-3 py-3 text-left text-sm transition hover:bg-muted/60",
                      active && "bg-primary/5",
                    )}
                  >
                    <div className="min-w-0">
                      {item.symbol === "MARKET" ? (
                        <span className="font-semibold text-purple-600 dark:text-purple-300">大盘 · 全市场</span>
                      ) : (
                        <Link to={`/single-stock-overnight?symbol=${item.symbol}`} onClick={(e) => e.stopPropagation()} className="font-semibold text-primary hover:underline">{item.symbol}</Link>
                      )}
                      <div className="truncate text-xs text-muted-foreground">{item.company_name || item.symbol}</div>
                    </div>
                    <div className="min-w-0">
                      <div className="truncate font-medium text-foreground">{title}</div>
                      <div className="mt-1 flex flex-wrap gap-1.5">
                        {item.symbol === "MARKET" && <span className="rounded-full border border-purple-500/45 bg-purple-500/10 px-2 py-0.5 text-[11px] font-medium text-purple-700 dark:text-purple-300">全市场·大盘</span>}
                        <span className={cn("rounded-full border px-2 py-0.5 text-[11px]", toneClass(item.sentiment))}>
                          {item.sentiment === "unreviewed" ? "待研判" : item.sentiment === "positive" ? "正面" : item.sentiment === "negative" ? "负面" : "中性"}
                        </span>
                        {item.event_type_cn && <span className="rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground">{item.event_type_cn}</span>}
                        {item.sector_effect && <span className="rounded-full border border-sky-500/35 bg-sky-500/10 px-2 py-0.5 text-[11px] text-sky-700 dark:text-sky-300">板块扩散</span>}
                        {bigGap && <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-700 dark:text-amber-300">高冲击</span>}
                        {item.source_tier_cn && (
                          <span className={cn("rounded-full border px-2 py-0.5 text-[11px]", tierClass(item.source_tier_tone))} title="来源类型(启发式)：权威媒体/一般媒体/散户荐股/企业通稿——非内容真伪判断">{item.source_tier_cn}</span>
                        )}
                        {item.source_provenance && <span className="text-[11px] text-amber-600 dark:text-amber-300">聚源摘要 · 原文未核验</span>}
                        {relatedTickers(item).length > 0 && (
                          <span className="rounded-full border border-primary/30 bg-primary/5 px-2 py-0.5 text-[11px] text-primary" title="同一新闻涉及的其它标的">相关 {relatedTickers(item).join(" · ")}</span>
                        )}
                      </div>
                    </div>
                    <div className={cn("text-lg font-semibold", impactClass(score))}>{score.toFixed(1)}</div>
                    <div>
                      <div className={cn("font-semibold", Number(item.estimated_gap_pct || 0) >= 0 ? "text-emerald-600" : "text-rose-600")}>
                        {item.estimated_gap_pct == null ? "待复核" : pct(item.estimated_gap_pct)}
                      </div>
                      <div className="text-xs text-muted-foreground">{item.impact_basis === "atr_reference" ? "ATR参考" : "带宽"} ±{pct(item.impact_band_pct)}</div>
                    </div>
                    <div>
                      {item.symbol === "MARKET" ? (
                        <div className="text-xs text-muted-foreground">宏观 · 全市场</div>
                      ) : (
                        <>
                          <div className={cn("font-semibold", priceMissing && "text-amber-600")}>
                            {priceMissing ? "待补齐" : money(item.current_price)}
                          </div>
                          <div className={cn("text-xs", Number(item.change_pct || 0) >= 0 ? "text-emerald-600" : "text-rose-600")}>{pct(item.change_pct)}</div>
                        </>
                      )}
                    </div>
                    <div className="text-xs text-muted-foreground">{item.source_provenance?.reported_time || compactDate(item.published_utc)}</div>
                    <div className="min-w-0">
                      <div className="truncate font-medium">{item.sector_name || poolHint || "--"}</div>
                      <div className="truncate text-xs text-muted-foreground">{item.publisher || "Unknown"}</div>
                    </div>
                  </div>
                  {active && (
                    <div className="border-l-2 border-primary/40 bg-muted/10 px-3 py-3">
                      <NewsCard item={item} onFeedback={mark} />
                    </div>
                  )}
                  </Fragment>
                );
              })}
            </div>
          </div>
        ) : (
          <div className="rounded-lg border bg-card p-8 text-center text-muted-foreground">
            当前筛选条件下没有新闻。可以点击“更新隔夜新闻”抓取从前一日收盘到现在的新闻。
          </div>
        )}
      </main>
    </div>
  );
}
