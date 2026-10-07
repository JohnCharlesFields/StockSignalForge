import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ChevronRight, Droplets, RefreshCw, ShieldQuestion, X } from "lucide-react";
import { api, type ConfidenceBadge, type PriorityBoardResponse, type PriorityPick } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Chip, ErrorState, LoadingState, MCard, StatTile, advShort, moveClass, pct, signedPct } from "../components/ui";

const BADGE: Record<ConfidenceBadge, { label: string; tone: "strong" | "warn" | "neutral" }> = {
  validated: { label: "已验证", tone: "strong" },
  low_edge: { label: "低把握·未验证", tone: "warn" },
  experimental: { label: "实验·未校准", tone: "neutral" },
};

function pickTags(p: PriorityPick): Array<{ key: string; label: string; tone: "strong" | "good" | "warn" | "bad" | "neutral" }> {
  const tags: Array<{ key: string; label: string; tone: "strong" | "good" | "warn" | "bad" | "neutral" }> = [];
  if (p.deep_oversold && p.deep_oversold.level !== "none") {
    tags.push({ key: "os", label: p.deep_oversold.level === "deep" ? "深超卖" : "超卖", tone: p.deep_oversold.level === "deep" ? "strong" : "good" });
  }
  const days = typeof p.earnings?.days_until === "number" ? p.earnings.days_until : null;
  if (p.earnings?.available && p.earnings.next_date && days !== null) {
    tags.push({ key: "er", label: `财报${days}天`, tone: days >= 0 && days <= 10 ? "warn" : "neutral" });
  }
  for (const item of p.playbook_enhancements?.labels ?? []) {
    if (item?.label) tags.push({ key: `pb:${item.id || item.label}`, label: item.label, tone: item.tone === "strong" ? "strong" : item.tone === "warn" ? "warn" : "good" });
  }
  if (p.news?.trump) tags.push({ key: "trump", label: "TRUMP", tone: "bad" });
  return tags.slice(0, 4);
}

export function MobileBoard() {
  const [board, setBoard] = useState<PriorityBoardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [liquidOnly, setLiquidOnly] = useState(() => (localStorage.getItem("priorityBoard.liquidOnly") ?? "1") === "1");
  const [oversoldOnly, setOversoldOnly] = useState(() => localStorage.getItem("priorityBoard.oversoldOnly") === "1");
  const [showWarn, setShowWarn] = useState(() => localStorage.getItem("priorityBoard.liveBannerDismissed") !== "1");

  const load = (force = false, liquid = liquidOnly, oversold = oversoldOnly) => {
    setLoading(true);
    setError("");
    api
      .getPriorityBoard(force ? 30 : 20, liquid, oversold, force)
      .then(setBoard)
      .catch((e) => setError(e instanceof Error ? e.message : "无法加载优先级看板"))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggleLiquid = () => {
    const next = !liquidOnly;
    setLiquidOnly(next);
    localStorage.setItem("priorityBoard.liquidOnly", next ? "1" : "0");
    load(false, next, oversoldOnly);
  };
  const toggleOversold = () => {
    const next = !oversoldOnly;
    setOversoldOnly(next);
    localStorage.setItem("priorityBoard.oversoldOnly", next ? "1" : "0");
    load(false, liquidOnly, next);
  };
  const dismissWarn = () => {
    setShowWarn(false);
    localStorage.setItem("priorityBoard.liveBannerDismissed", "1");
  };

  return (
    <div className="space-y-3 p-3 pb-6">
      {showWarn && (
        <div className="flex items-start gap-2 rounded-xl border-2 border-amber-500/50 bg-amber-500/10 p-3 text-xs leading-relaxed">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
          <div className="flex-1 text-amber-700 dark:text-amber-200">
            <b>实盘验证期 · 非已验证策略。</b>edge 很小（~1%/10交易日），真实前向对账尚未结算。胜率=跑赢自身基线的概率，非赚钱概率。小资金、按纪律。
          </div>
          <button type="button" onClick={dismissWarn} aria-label="不再提示" className="shrink-0 rounded p-1 text-amber-600 active:bg-amber-500/20">
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* filters */}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={toggleLiquid}
          className={cn(
            "inline-flex min-h-[40px] flex-1 items-center justify-center gap-1.5 rounded-lg border px-2 text-xs font-medium",
            liquidOnly ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "text-muted-foreground",
          )}
        >
          <Droplets className="h-4 w-4" /> 高流动性{liquidOnly ? "·开" : "·关"}
        </button>
        <button
          type="button"
          onClick={toggleOversold}
          className={cn(
            "inline-flex min-h-[40px] flex-1 items-center justify-center gap-1.5 rounded-lg border px-2 text-xs font-medium",
            oversoldOnly ? "border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300" : "text-muted-foreground",
          )}
        >
          仅深超卖{oversoldOnly ? "·开" : "·关"}
        </button>
        <button
          type="button"
          onClick={() => load(true)}
          disabled={loading}
          aria-label="刷新"
          className="grid h-10 w-10 shrink-0 place-items-center rounded-lg border text-muted-foreground active:bg-muted disabled:opacity-60"
        >
          <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
        </button>
      </div>

      {board && (
        <MCard className="flex flex-wrap items-center gap-2 text-xs">
          <Chip tone={BADGE[board.confidence.badge].tone}>
            <span className="inline-flex items-center gap-1">
              <ShieldQuestion className="h-3 w-3" />
              {BADGE[board.confidence.badge].label}
            </span>
          </Chip>
          <span className="text-muted-foreground">候选 {board.n_candidates} · SPY20日 {signedPct(board.benchmark_return_20d)}</span>
        </MCard>
      )}

      {board?.no_edge_today && (
        <div className="rounded-xl border border-warning/40 bg-warning/10 p-3 text-xs leading-relaxed text-warning">
          ⚠️ 今日无显著买点：最高校准胜率仅 {pct(board.top_calibrated_win_rate)}（&lt;50%）。上涨市里逆水行舟——建议观望。
        </div>
      )}

      {loading && !board && <LoadingState />}
      {error && !board && <ErrorState message={error} onRetry={() => load(true)} />}

      {board && board.picks.length === 0 && !loading && (
        <div className="rounded-xl border bg-card p-6 text-center text-sm text-muted-foreground">暂无候选（等待当日快照生成）。</div>
      )}

      {board?.picks.map((p, i) => (
        <Link key={p.symbol} to={`/m/stock?symbol=${encodeURIComponent(p.symbol)}`} className="block">
          <MCard className="space-y-2.5">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="flex items-baseline gap-2">
                  <span className="text-[11px] text-muted-foreground">#{i + 1}</span>
                  <span className="m-break text-lg font-bold">{p.symbol}</span>
                  {p.current_price > 0 && <span className="font-mono text-xs text-muted-foreground">${p.current_price.toFixed(2)}</span>}
                </div>
                <div className="m-break mt-0.5 text-xs text-muted-foreground">{p.track_cn || p.track || "--"}</div>
              </div>
              <div className="shrink-0 text-right">
                <div className={cn("font-mono text-xl font-bold tabular-nums", p.calibrated_probability >= 0.5 ? "text-success" : "text-warning")}>
                  {pct(p.calibrated_probability)}
                </div>
                <div className="text-[10px] text-muted-foreground">校准胜率</div>
              </div>
            </div>

            <div className="grid grid-cols-3 gap-2">
              <StatTile label="优先级" value={(p.ranking_score ?? p.priority_score).toFixed(3)} />
              <StatTile label="相对大盘" value={signedPct(p.relative_strength_20d)} tone={moveClass(p.relative_strength_20d)} />
              <StatTile label="流动性" value={p.liquidity?.tier && p.liquidity.tier !== "未知" ? p.liquidity.tier : advShort(p.liquidity?.adv)} />
            </div>

            {(() => {
              const tags = pickTags(p);
              if (!tags.length) return null;
              return (
                <div className="flex flex-wrap gap-1.5">
                  {tags.map((t) => (
                    <Chip key={t.key} tone={t.tone}>{t.label}</Chip>
                  ))}
                </div>
              );
            })()}

            <div className="flex items-center justify-end text-xs text-primary">
              查看研判 <ChevronRight className="h-3.5 w-3.5" />
            </div>
          </MCard>
        </Link>
      ))}

      {board?.method_note && <p className="px-1 text-[11px] leading-relaxed text-muted-foreground">{board.method_note}</p>}
    </div>
  );
}
