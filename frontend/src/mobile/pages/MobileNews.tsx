import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Check, ChevronLeft, ChevronRight, ExternalLink, FileText, Globe, RefreshCw, X } from "lucide-react";
import { toast } from "sonner";
import { api, type PremarketNewsItem } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Chip, EmptyState, ErrorState, LoadingState, StatTile, moveClass, signedPct } from "../components/ui";

// Short tickers that are also common uppercase words/abbreviations in headlines —
// skip them so we don't highlight the English word instead of the stock.
const TICKER_STOPWORDS = new Set([
  "AI", "ON", "IT", "ALL", "US", "USA", "OR", "BE", "GO", "SO", "AM", "PM", "AN", "AS", "AT", "BY", "DO", "IF",
  "IN", "IS", "OF", "UP", "WE", "ARE", "FOR", "NEW", "NOW", "ONE", "TWO", "OUT", "CEO", "CFO", "IPO", "ETF",
  "AND", "GDP", "EPS", "EV", "PC", "TV", "OK", "Q1", "Q2", "Q3", "Q4",
]);

/** Highlight ticker symbols inside free text (case-sensitive, word-boundary). */
function highlightTickers(text: string, tickers: string[]): ReactNode {
  if (!text) return null;
  const list = Array.from(
    new Set(tickers.map((t) => (t || "").trim().toUpperCase()).filter((t) => t.length >= 2 && !TICKER_STOPWORDS.has(t))),
  );
  if (!list.length) return text;
  const esc = list.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp(`\\b(${esc.join("|")})\\b`, "g");
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(
      <mark key={`h${i++}`} className="rounded bg-amber-300/50 px-0.5 font-semibold text-foreground dark:bg-amber-500/30">
        {m[0]}
      </mark>,
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const MARKET = "MARKET";
const POS_KEY = "premarket_swipe_news_id"; // shared with the desktop swipe page so position carries over
const SWIPE_THRESHOLD = 75;

const SENTIMENT_CN: Record<string, { label: string; tone: string; chip: "good" | "bad" | "neutral" }> = {
  positive: { label: "利好", tone: "text-emerald-600 dark:text-emerald-400", chip: "good" },
  negative: { label: "利空", tone: "text-rose-600 dark:text-rose-400", chip: "bad" },
  neutral: { label: "中性", tone: "text-muted-foreground", chip: "neutral" },
};

function tierTone(tone?: string): "strong" | "good" | "warn" | "neutral" {
  if (tone === "good") return "good";
  if (tone === "warn" || tone === "caution") return "warn";
  return "neutral";
}

function fmtTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Full-bleed news card — shows as much content as fits, plus a 要点 summary. */
function CardContent({ item, onOpenReader, labeled }: { item: PremarketNewsItem; onOpenReader: () => void; labeled?: "important" | "not_important" | null }) {
  const isMarket = String(item.symbol || "").toUpperCase() === MARKET;
  const title = item.title_cn || item.title_original;
  const desc = item.description_cn || item.description_original;
  const reason = item.sentiment_reasoning;
  const related = (item.related_symbols || []).filter((s) => s && s.toUpperCase() !== MARKET).slice(0, 8);
  const sent = SENTIMENT_CN[item.sentiment || "neutral"] ?? SENTIMENT_CN.neutral;
  const score = Math.round(Number(item.adjusted_importance_score || item.importance_score || 0));

  return (
    <div className={cn("flex h-full flex-col gap-2.5 rounded-2xl border bg-card p-4 shadow-sm", isMarket && "border-indigo-500/40 bg-indigo-500/5")}>
      {/* sentiment-colored stock / market tag row */}
      <div className="flex flex-wrap items-center gap-1.5">
        {isMarket ? (
          <Chip tone={sent.chip} title="影响全市场的宏观新闻">
            <span className="inline-flex items-center gap-1">
              <Globe className="h-3 w-3" /> 全市场·{sent.label}
            </span>
          </Chip>
        ) : (
          <Link to={`/m/stock?symbol=${encodeURIComponent(item.symbol)}`} onPointerDown={(e) => e.stopPropagation()}>
            <Chip tone={sent.chip}>
              <span className="font-bold">{item.symbol}</span>
            </Chip>
          </Link>
        )}
        {item.company_name && !isMarket && <span className="m-break truncate text-xs text-muted-foreground">{item.company_name}</span>}
        {item.source_tier_cn && <Chip tone={tierTone(item.source_tier_tone)}>{item.source_tier_cn}</Chip>}
        {item.event_type_cn && <Chip tone="neutral">{item.event_type_cn}</Chip>}
        {labeled === "important" && <Chip tone="strong">✓ 已标有用</Chip>}
        {labeled === "not_important" && <Chip tone="bad">✗ 已标没用</Chip>}
        {!labeled && item.preference?.label && (
          <Chip tone={item.preference.label.includes("勾选") ? "good" : "warn"}>{item.preference.label}</Chip>
        )}
      </div>

      <div className="m-break line-clamp-3 text-[17px] font-semibold leading-snug">{title}</div>

      <div className="grid grid-cols-3 gap-2">
        <StatTile label="冲击分" value={score || "--"} />
        <StatTile label="预计跳空" value={signedPct(item.estimated_gap_pct)} tone={moveClass(item.estimated_gap_pct)} />
        <StatTile label="情绪" value={sent.label} tone={sent.tone} />
      </div>

      {related.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {related.map((s) => (
            <Link key={s} to={`/m/stock?symbol=${encodeURIComponent(s)}`} onPointerDown={(e) => e.stopPropagation()}>
              <Chip tone={sent.chip}>{s}</Chip>
            </Link>
          ))}
        </div>
      )}

      {reason && (
        <div className={cn("rounded-lg border-l-2 bg-muted/40 px-2.5 py-1.5", sent.chip === "good" ? "border-emerald-500" : sent.chip === "bad" ? "border-rose-500" : "border-slate-400")}>
          <span className="text-[11px] font-semibold text-muted-foreground">要点</span>
          <p className="m-break mt-0.5 line-clamp-4 text-xs leading-relaxed">{reason}</p>
        </div>
      )}

      {/* full-text region fills the rest; shows as much as space allows */}
      {desc && (
        <div className="min-h-0 flex-1 overflow-hidden">
          <p className="m-break text-sm leading-relaxed text-muted-foreground">{desc}</p>
        </div>
      )}

      <div className="mt-auto flex items-center justify-between gap-2 border-t pt-2 text-[11px] text-muted-foreground">
        <span className="m-break min-w-0 truncate">{item.publisher || item.source || "—"} · {fmtTime(item.published_utc)}</span>
        <button
          type="button"
          onPointerDown={(e) => e.stopPropagation()}
          onClick={onOpenReader}
          className="inline-flex shrink-0 items-center gap-1 rounded-md bg-primary/10 px-2 py-1 font-medium text-primary active:bg-primary/20"
        >
          <FileText className="h-3.5 w-3.5" /> 原文
        </button>
      </div>
    </div>
  );
}

/** Full-screen in-app reader: shows the captured original text + source + date,
 *  with tickers highlighted. No external navigation. */
function ArticleReader({ item, onClose }: { item: PremarketNewsItem; onClose: () => void }) {
  const [zh, setZh] = useState(false);
  // Make the hardware/browser Back button (and gesture) close the reader instead
  // of navigating the router away from the news page.
  useEffect(() => {
    window.history.pushState({ __reader: true }, "");
    const onPop = () => onClose();
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const close = () => window.history.back(); // → popstate → onClose
  const isMarket = String(item.symbol || "").toUpperCase() === MARKET;
  const tickers = [...(item.related_symbols || []), ...(isMarket ? [] : [item.symbol])].filter(Boolean) as string[];
  const sent = SENTIMENT_CN[item.sentiment || "neutral"] ?? SENTIMENT_CN.neutral;
  const hasZh = !!(item.title_cn || item.description_cn);
  const title = zh ? item.title_cn || item.title_original : item.title_original;
  const body = zh ? item.description_cn || item.description_original : item.description_original;

  return (
    <div className="m-safe-top m-safe-bottom fixed inset-0 z-50 flex flex-col bg-background">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b bg-card px-2">
        <button onClick={close} aria-label="返回" className="inline-flex h-9 shrink-0 items-center gap-0.5 rounded-lg pl-1 pr-2 text-sm font-medium text-primary active:bg-muted">
          <ChevronLeft className="h-5 w-5" /> 返回
        </button>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold">{item.publisher || item.source || "原文"}</div>
          <div className="text-[11px] text-muted-foreground">
            {fmtTime(item.published_utc)}
            {item.source_tier_cn ? ` · ${item.source_tier_cn}` : ""}
          </div>
        </div>
        {hasZh && (
          <button onClick={() => setZh((v) => !v)} className="shrink-0 rounded-md border px-2.5 py-1 text-xs text-muted-foreground active:bg-muted">
            {zh ? "原文" : "译文"}
          </button>
        )}
      </header>

      <div className="m-scroll flex-1 overflow-y-auto px-4 py-3">
        {tickers.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {tickers.map((s) => (
              <Link key={s} to={`/m/stock?symbol=${encodeURIComponent(s)}`}>
                <Chip tone={sent.chip}>{s}</Chip>
              </Link>
            ))}
          </div>
        )}
        <h1 className="m-break text-lg font-bold leading-snug">{highlightTickers(title || "", tickers)}</h1>
        <div className="m-break mt-3 whitespace-pre-wrap text-[15px] leading-relaxed">
          {body ? highlightTickers(body, tickers) : <span className="text-muted-foreground">（数据源未提供正文）</span>}
        </div>
        <p className="mt-4 rounded-lg bg-muted/40 p-2.5 text-[11px] leading-relaxed text-muted-foreground">
          以上为数据源（Polygon/Massive）抓取的原文摘要；发行方完整正文多受付费墙 / 反爬限制，无法稳定抓取全文。
        </p>
        {item.article_url && (
          <a
            href={item.article_url}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-2 inline-flex items-center gap-1 text-xs text-muted-foreground underline-offset-2 hover:underline"
          >
            在浏览器打开发行方链接 <ExternalLink className="h-3.5 w-3.5" />
          </a>
        )}
      </div>
    </div>
  );
}

export function MobileNews() {
  const [items, setItems] = useState<PremarketNewsItem[]>([]);
  const [reader, setReader] = useState<PremarketNewsItem | null>(null);
  const [myLabels, setMyLabels] = useState<Record<string, "important" | "not_important">>({});
  const [active, setActive] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  // drag state
  const [drag, setDrag] = useState({ x: 0, y: 0 });
  const [dragActive, setDragActive] = useState(false);
  const [hover, setHover] = useState<null | "important" | "not_important">(null);
  const [anim, setAnim] = useState(false);
  const pressed = useRef(false);
  const animating = useRef(false);
  const startX = useRef(0);
  const startY = useRef(0);
  const impRef = useRef<HTMLButtonElement>(null);
  const notRef = useRef<HTMLButtonElement>(null);

  const current = items[active];

  const load = async (keepActive = true) => {
    setError("");
    try {
      // "all" keeps already-labelled news in the feed (marks are training labels,
      // not a filter). Ordering is personalised by those labels server-side.
      const queue = await api.getPremarketNews({ reviewed: "all", limit: 120, min_score: 0 });
      const list = queue.items || [];
      setItems(list);
      setActive((prev) => {
        if (!list.length) return 0;
        const savedId = typeof localStorage !== "undefined" ? localStorage.getItem(POS_KEY) : null;
        if (savedId) {
          const i = list.findIndex((x) => x.news_id === savedId);
          if (i >= 0) return i;
        }
        if (!keepActive) return 0;
        const oldId = items[prev]?.news_id;
        const i = oldId ? list.findIndex((x) => x.news_id === oldId) : 0;
        return i >= 0 ? i : 0;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "无法加载盘前新闻");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(false);
    const timer = window.setInterval(() => {
      if (!document.hidden && !pressed.current && !animating.current) void load(true);
    }, 60_000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (current?.news_id && typeof localStorage !== "undefined") localStorage.setItem(POS_KEY, current.news_id);
  }, [current?.news_id]);

  const manualRefresh = async () => {
    setRefreshing(true);
    setError("");
    try {
      await api.refreshPremarketNews({ translate_top: 100, max_pages: 3, refresh_universe: false });
      await load(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "刷新失败");
    } finally {
      setRefreshing(false);
    }
  };

  const mark = async (decision: "important" | "not_important") => {
    if (!current) return;
    const id = current.news_id;
    // Label-only: keep the card (it becomes training data, not filtered out).
    setAnim(false);
    setDrag({ x: 0, y: 0 });
    setMyLabels((m) => ({ ...m, [id]: decision }));
    setActive((p) => Math.min(items.length - 1, p + 1)); // advance to next, don't remove
    toast.success(decision === "important" ? "已标记有用 ✓ 同类新闻会被加权" : "已标记没用 ✗ 同类新闻会被降权");
    try {
      await api.markPremarketNewsFeedback(id, { decision });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "提交失败");
    }
  };

  // dir: +1 = previous (fly right), -1 = next (fly left)
  const flyAndSwap = (dir: 1 | -1) => {
    animating.current = true;
    setAnim(true);
    setDrag({ x: dir > 0 ? 600 : -600, y: 0 });
    window.setTimeout(() => {
      setActive((a) => Math.max(0, Math.min(items.length - 1, a + (dir > 0 ? -1 : 1))));
      setAnim(false);
      setDrag({ x: 0, y: 0 });
      animating.current = false;
    }, 200);
  };

  const hitTest = (cx: number, cy: number): "important" | "not_important" | null => {
    // Inflate the target so dropping near the circle counts (circles are small).
    const pad = 30;
    const within = (r: DOMRect | undefined) =>
      !!r && cx >= r.left - pad && cx <= r.right + pad && cy >= r.top - pad && cy <= r.bottom + pad;
    if (within(impRef.current?.getBoundingClientRect())) return "important";
    if (within(notRef.current?.getBoundingClientRect())) return "not_important";
    return null;
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (animating.current) return;
    pressed.current = true;
    startX.current = e.clientX;
    startY.current = e.clientY;
    setAnim(false);
    setDragActive(true);
    setDrag({ x: 0, y: 0 });
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* ignore */ }
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!pressed.current || animating.current) return;
    const x = e.clientX - startX.current;
    const y = e.clientY - startY.current;
    setDrag({ x, y });
    setHover(hitTest(e.clientX, e.clientY));
  };
  const onPointerUp = (e: React.PointerEvent) => {
    if (!pressed.current) return;
    pressed.current = false;
    const x = e.clientX - startX.current;
    const y = e.clientY - startY.current;
    const target = hitTest(e.clientX, e.clientY);
    setDragActive(false);
    setHover(null);
    if (target === "important") { setDrag({ x: 0, y: 0 }); void mark("important"); return; }
    if (target === "not_important") { setDrag({ x: 0, y: 0 }); void mark("not_important"); return; }
    if (x > SWIPE_THRESHOLD && Math.abs(x) > Math.abs(y) && active > 0) { flyAndSwap(1); return; }
    if (x < -SWIPE_THRESHOLD && Math.abs(x) > Math.abs(y) && active < items.length - 1) { flyAndSwap(-1); return; }
    setAnim(true);
    setDrag({ x: 0, y: 0 });
  };

  const urgent = useMemo(() => items.filter((x) => Number(x.adjusted_importance_score || x.importance_score || 0) >= 75).length, [items]);
  const labeledCount = useMemo(
    () => items.filter((x) => myLabels[x.news_id] || x.feedback?.decision === "important" || x.feedback?.decision === "not_important").length,
    [items, myLabels],
  );

  if (loading && items.length === 0) return <LoadingState />;
  if (error && items.length === 0) return <ErrorState message={error} onRetry={() => load(false)} />;

  const btnBase = "flex items-center justify-center gap-2 font-semibold text-white transition-all duration-200 origin-bottom";

  return (
    <div className="flex h-full flex-col">
      {/* toolbar */}
      <div className="flex items-center justify-between gap-2 px-3 pt-2.5">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className="font-mono">{current ? `${Math.min(active + 1, items.length)} / ${items.length}` : "0 / 0"}</span>
          <span>已标 {labeledCount}</span>
          {urgent > 0 && <Chip tone="warn">{urgent} 条高冲击</Chip>}
        </div>
        <button
          type="button"
          onClick={manualRefresh}
          disabled={refreshing}
          aria-label="刷新"
          className="grid h-9 w-9 place-items-center rounded-lg border text-muted-foreground active:bg-muted disabled:opacity-60"
        >
          <RefreshCw className={cn("h-4 w-4", refreshing && "animate-spin")} />
        </button>
      </div>

      {/* swipe / drag stage */}
      <div className="relative min-h-0 flex-1 px-3 py-2.5">
        {current ? (
          <>
            {drag.x > 40 && active > 0 && !hover && (
              <div className="pointer-events-none absolute left-5 top-1/2 z-10 -translate-y-1/2 rounded-full bg-foreground/80 px-2 py-1 text-xs font-medium text-background">上一页</div>
            )}
            {drag.x < -40 && active < items.length - 1 && !hover && (
              <div className="pointer-events-none absolute right-5 top-1/2 z-10 -translate-y-1/2 rounded-full bg-foreground/80 px-2 py-1 text-xs font-medium text-background">下一页</div>
            )}
            <div
              className="h-full select-none"
              style={{
                touchAction: "none",
                transform: `translate(${drag.x}px, ${drag.y}px) rotate(${drag.x * 0.025}deg)`,
                transition: anim ? "transform 0.2s ease-out" : "none",
                opacity: 1 - Math.min(Math.hypot(drag.x, drag.y) / 700, 0.35),
              }}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerCancel={onPointerUp}
            >
              <CardContent
                item={current}
                onOpenReader={() => setReader(current)}
                labeled={
                  myLabels[current.news_id] ??
                  (current.feedback?.decision === "important" || current.feedback?.decision === "not_important"
                    ? current.feedback.decision
                    : null)
                }
              />
            </div>
          </>
        ) : (
          <EmptyState label="当前没有待勾选新闻" hint="左右滑动翻页 · 拖到下方按钮即可标记 · 右上角刷新" />
        )}
      </div>

      {/* decision bar — while dragging the card, the buttons morph into big round
          ✓ / ✗ drop targets; releasing the card over one marks it. */}
      {current && (
        <div className={cn("flex items-center gap-3 px-3 pt-1 transition-all duration-200", dragActive ? "min-h-[112px] justify-around pb-3" : "min-h-[64px] pb-2")}>
          <button
            ref={impRef}
            type="button"
            onClick={() => void mark("important")}
            className={cn(
              btnBase,
              "bg-emerald-600 active:bg-emerald-700",
              dragActive ? "h-20 w-20 rounded-full shadow-xl" : "h-14 flex-1 rounded-2xl text-base",
              hover === "important" && "h-24 w-24 shadow-2xl ring-4 ring-emerald-300/70",
            )}
          >
            {dragActive ? (
              <Check className={cn("h-9 w-9", hover === "important" && "h-12 w-12")} />
            ) : (
              <span className="inline-flex items-center gap-2"><Check className="h-6 w-6" /> 重要</span>
            )}
          </button>
          <button
            ref={notRef}
            type="button"
            onClick={() => void mark("not_important")}
            className={cn(
              btnBase,
              "bg-rose-600 active:bg-rose-700",
              dragActive ? "h-20 w-20 rounded-full shadow-xl" : "h-14 flex-1 rounded-2xl text-base",
              hover === "not_important" && "h-24 w-24 shadow-2xl ring-4 ring-rose-300/70",
            )}
          >
            {dragActive ? (
              <X className={cn("h-9 w-9", hover === "not_important" && "h-12 w-12")} />
            ) : (
              <span className="inline-flex items-center gap-2"><X className="h-6 w-6" /> 不重要</span>
            )}
          </button>
        </div>
      )}

      {/* prev/next fallback (non-touch / accessibility) */}
      {current && items.length > 1 && (
        <div className="flex items-center justify-between px-3 pb-2 text-xs text-muted-foreground">
          <button type="button" onClick={() => active > 0 && flyAndSwap(1)} disabled={active <= 0} className="inline-flex h-8 items-center gap-1 rounded-md px-2 disabled:opacity-30">
            <ChevronLeft className="h-4 w-4" /> 上一页
          </button>
          <span className="text-[11px]">← 滑动翻页 · 拖到按钮标记 →</span>
          <button type="button" onClick={() => active < items.length - 1 && flyAndSwap(-1)} disabled={active >= items.length - 1} className="inline-flex h-8 items-center gap-1 rounded-md px-2 disabled:opacity-30">
            下一页 <ChevronRight className="h-4 w-4" />
          </button>
        </div>
      )}

      {reader && <ArticleReader item={reader} onClose={() => setReader(null)} />}
    </div>
  );
}
