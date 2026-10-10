import { useEffect, useRef, useState } from "react";
import { api, type NewsDigest, type StockNewsResearchData } from "@/lib/api";
import { cn } from "@/lib/utils";

const tone = (s?: string) => s === "positive" ? "text-emerald-700 dark:text-emerald-300" : s === "negative" ? "text-rose-700 dark:text-rose-300" : "text-muted-foreground";
const label = (s?: string) => s === "positive" ? "正面" : s === "negative" ? "负面" : s === "neutral" ? "中性" : "待研判";
const date = (value?: string | null) => {
  if (!value) return "--";
  const d = new Date(value);
  return Number.isFinite(d.getTime()) ? d.toLocaleDateString("zh-CN") : "日期待核验";
};
const safeHref = (value?: string | null) => {
  try { const url = new URL(value || ""); return ["http:", "https:"].includes(url.protocol) ? value || undefined : undefined; }
  catch { return undefined; }
};

export function StockNewsResearch({ symbol, legacy }: { symbol: string; legacy?: NewsDigest }) {
  const [data, setData] = useState<StockNewsResearchData>();
  const [error, setError] = useState("");
  const sequence = useRef(0);
  const attempts = useRef(new Set<string>());
  useEffect(() => {
    const seq = ++sequence.current;
    attempts.current = new Set();
    setData(undefined); setError("");
    const controller = new AbortController();
    let alive = true, loading = false;
    const opened = Date.now();
    const load = async () => {
      if (loading || !alive) return;
      loading = true;
      try {
        const value = await api.getStockNewsResearch(symbol, controller.signal);
        if (!alive || seq !== sequence.current) return;
        if (value.symbol !== symbol || !Array.isArray(value.items)) throw new Error("Invalid news response");
        setData(value); setError("");
        if (value.items.length && !["queued", "running", "completed", "insufficient_content", "failed"].includes(value.analysis.status) && !attempts.current.has(value.fingerprint)) {
          attempts.current.add(value.fingerprint);
          const state = await api.summarizeStockNews(symbol, controller.signal);
          if (alive && seq === sequence.current) {
            if (state.status === "busy") setError("后台任务繁忙，先展示来源摘要；可稍后重试总结。");
            else setData(await api.getStockNewsResearch(symbol, controller.signal));
          }
        }
      } catch { if (alive && seq === sequence.current) setError("新闻读取暂不可用，已显示的内容保留。"); }
      finally { loading = false; }
    };
    void load();
    // A bounded read-only poll also discovers news from the existing page worker.
    const timer = window.setInterval(() => {
      if (Date.now() - opened < 180000) void load();
      else window.clearInterval(timer);
    }, 5000);
    return () => { alive = false; controller.abort(); sequence.current++; window.clearInterval(timer); };
  }, [symbol]);

  const current = data?.symbol === symbol ? data : undefined;
  const analysis = current?.analysis;
  const items = legacy?.available
    ? (legacy.items || []).slice(0, 8).map(i => ({ title: i.title, sentiment: i.sentiment, url: i.url, publisher: i.publisher, published: i.published_utc, received: false }))
    : (current?.items || []).slice(0, 8).map(i => ({ title: i.title_cn || i.title_original, sentiment: "unreviewed", url: i.url, publisher: i.publisher, published: i.published_utc, received: i.time_basis === "received_at" }));
  const hasNews = legacy?.available || !!current?.count;
  const trump = legacy?.trump;
  const impact = legacy?.impact;
  const basis = analysis?.summary_basis === "title_only" ? "基于标题" : analysis?.summary_basis === "mixed" ? "基于标题与来源摘要" : "基于来源摘要";
  return <section className="min-w-0 rounded border bg-card p-4">
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <h2 className="font-semibold">舆情 · 近期新闻情绪</h2>
      {legacy?.available && <span className={cn("rounded px-1.5 py-0.5 text-[11px]", (legacy.neg ?? 0) > (legacy.pos ?? 0) ? "bg-amber-500/15 text-amber-700 dark:text-amber-300" : "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300")}>
        {legacy.label}（近10日 {legacy.n} 条：<span className="text-emerald-600 dark:text-emerald-400">正{legacy.pos}</span>/<span className="text-rose-600 dark:text-rose-400">负{legacy.neg}</span>/中{legacy.neu}）
      </span>}
    </div>
    {!!current?.count && <p data-testid="recent-news-summary" className="mt-2 break-words text-xs leading-5">
      <strong>近期新闻总结：</strong>{analysis?.summary_cn || (["queued", "running"].includes(analysis?.status || "") ? "后台整理中，先展示新闻列表。" : error || analysis?.status === "failed" ? "暂未生成，新闻列表保留。" : "正在整理最近新闻。")}
      {analysis?.summary_cn && <span className="ml-1 text-[10px] text-muted-foreground">（{basis} · 最新{current.count}条）</span>}
    </p>}
    {!hasNews && <p className="mt-1 text-xs text-muted-foreground">{legacy?.reason === "not_cached" || !current ? "新闻正在后台更新。" : "暂无已收录的近期新闻。"}</p>}
    {trump?.mentioned && <div className="mt-2 rounded-md border-2 border-orange-500/60 bg-orange-500/10 p-2.5">
      <div className="flex flex-wrap items-center gap-2 text-sm font-medium text-orange-700 dark:text-orange-300">
        <span className="rounded bg-orange-500 px-1.5 py-0.5 text-[11px] text-white">TRUMP 提及</span>
        近10日有 {trump.count} 条提及 · 情绪{trump.net_score > 0.1 ? "偏正" : trump.net_score < -0.1 ? "偏负" : "中性"}（净 {trump.net_score >= 0 ? "+" : ""}{trump.net_score.toFixed(2)}）
      </div>
      <ul className="mt-1.5 space-y-1">{trump.items?.map((it, i) => <li key={i} className="text-xs">
        <a href={safeHref(it.url)} target="_blank" rel="noreferrer" className="text-primary hover:underline">{it.title}</a>
        <span className={cn("ml-1", tone(it.sentiment))}>· {label(it.sentiment)}</span>
        {it.published_utc && <span className="ml-1 text-muted-foreground">· {date(it.published_utc)}</span>}
      </li>)}</ul>
      <p className="mt-1 text-[10px] text-orange-700/80 dark:text-orange-300/80">政治人物提及可能引发双向波动；本系统仅展示，未验证为可交易信号。</p>
    </div>}
    {impact?.available && <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
      <span className="font-medium">预计短期影响：</span>
      <span className={cn("font-mono font-semibold", (impact.point_pct ?? 0) >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400")}>
        {(impact.point_pct ?? 0) >= 0 ? "+" : ""}{((impact.point_pct ?? 0) * 100).toFixed(2)}%（{impact.direction}）
      </span>
      <span className="text-muted-foreground">波动带 ±{((impact.band_pct ?? 0) * 100).toFixed(1)}%</span>
      <span className="text-muted-foreground/80">{impact.basis}</span>
    </div>}
    {impact && <p className="mt-0.5 text-[10px] text-muted-foreground/70">{impact.note}</p>}
    <div data-testid="recent-news-headlines" className="mt-3 space-y-1.5">{items.map((it, i) => <div key={i} className="flex flex-wrap items-baseline gap-x-2 text-xs">
      <span className={cn("font-medium", tone(it.sentiment))}>{label(it.sentiment)}</span>
      <a href={safeHref(it.url)} target="_blank" rel="noreferrer" className="min-w-0 break-words text-foreground hover:text-primary hover:underline">{it.title}</a>
      {it.publisher && <span className="text-muted-foreground">· {it.publisher}</span>}
      {it.published && <span className="text-muted-foreground/70">· {it.received ? "收录 " : ""}{date(it.published)}</span>}
    </div>)}</div>
    {hasNews && <p className="mt-2 text-[10px] text-muted-foreground/70">情绪标签沿用原新闻源；一句话总结使用最近30天内最新10条标题或来源摘要，不改变胜率或排序。</p>}
  </section>;
}
