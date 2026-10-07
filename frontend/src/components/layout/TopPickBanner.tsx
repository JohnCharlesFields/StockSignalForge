import { useEffect, useState } from "react";
import { ChevronDown, ChevronUp, Crown, ShieldQuestion, TrendingUp } from "lucide-react";
import { cn } from "@/lib/utils";
import { api, type ConfidenceBadge, type PriorityBoardResponse, type PriorityPick } from "@/lib/api";

// Module-level cache so navigating between pages does not refetch every time.
let cached: { at: number; data: PriorityBoardResponse } | null = null;
const TTL_MS = 5 * 60 * 1000;

function pct(v: number | undefined | null, d = 1): string {
  if (v === undefined || v === null || !Number.isFinite(v)) return "--";
  return `${(v * 100).toFixed(d)}%`;
}
function signedPct(v: number | undefined | null, d = 1): string {
  if (v === undefined || v === null || !Number.isFinite(v)) return "--";
  return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(d)}%`;
}

const BADGE: Record<ConfidenceBadge, { label: string; cls: string }> = {
  validated: { label: "已验证", cls: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30" },
  low_edge: { label: "低把握·未验证", cls: "bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30" },
  experimental: { label: "实验·未校准", cls: "bg-slate-500/15 text-slate-600 dark:text-slate-300 border-slate-500/30" },
};

function ConfidencePill({ badge }: { badge: ConfidenceBadge }) {
  const b = BADGE[badge] ?? BADGE.experimental;
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium", b.cls)}>
      <ShieldQuestion className="h-3 w-3" aria-hidden="true" />
      {b.label}
    </span>
  );
}

export function TopPickBanner() {
  const [board, setBoard] = useState<PriorityBoardResponse | null>(cached?.data ?? null);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("qa-toppick") === "collapsed");
  const [error, setError] = useState(false);

  useEffect(() => {
    if (cached && Date.now() - cached.at < TTL_MS) {
      setBoard(cached.data);
      return;
    }
    let alive = true;
    api
      .getPriorityBoard(5)
      .then((data) => {
        cached = { at: Date.now(), data };
        if (alive) setBoard(data);
      })
      .catch(() => alive && setError(true));
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    localStorage.setItem("qa-toppick", collapsed ? "collapsed" : "expanded");
  }, [collapsed]);

  // Hide entirely when there is nothing to show (no snapshot yet / fetch failed).
  if (error || !board || !board.picks?.length) return null;

  const top: PriorityPick = board.picks[0];
  const rest = board.picks.slice(1, 5);

  return (
    <div className="border-b bg-gradient-to-r from-amber-500/[0.06] via-background to-background">
      <div className="mx-auto flex max-w-[1600px] items-center gap-3 px-4 py-2 text-sm">
        <span className="inline-flex shrink-0 items-center gap-1.5 text-xs font-semibold text-amber-600 dark:text-amber-400">
          <Crown className="h-4 w-4" aria-hidden="true" />
          今日优先标的
        </span>

        {!collapsed && (
          <>
            <a
              href={top.detail_url}
              className="inline-flex shrink-0 items-center gap-2 rounded-md px-2 py-1 hover:bg-muted"
              title={top.reason}
            >
              <span className="text-base font-bold tracking-tight text-primary">{top.symbol}</span>
              {top.current_price > 0 && <span className="font-mono text-xs text-muted-foreground">${top.current_price.toFixed(2)}</span>}
            </a>

            <ConfidencePill badge={top.confidence_badge} />

            <span className="inline-flex items-center gap-1 whitespace-nowrap text-xs text-muted-foreground">
              <TrendingUp className="h-3.5 w-3.5" aria-hidden="true" />
              相对大盘{" "}
              <span className={cn("font-mono font-medium", top.relative_strength_20d >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400")}>
                {signedPct(top.relative_strength_20d)}
              </span>
            </span>
            <span className="whitespace-nowrap text-xs text-muted-foreground">
              校准胜率 <span className="font-mono font-medium text-foreground">{pct(top.calibrated_probability, 0)}</span>
            </span>

            <span className="hidden truncate text-xs text-muted-foreground lg:inline" title={top.reason}>
              · {top.reason}
            </span>

            <div className="ml-auto hidden items-center gap-1.5 md:flex">
              {rest.map((p) => (
                <a
                  key={p.symbol}
                  href={p.detail_url}
                  className="inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs hover:bg-muted"
                  title={p.reason}
                >
                  <span className="font-semibold">{p.symbol}</span>
                  <span className={cn("font-mono text-[10px]", p.relative_strength_20d >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400")}>
                    {signedPct(p.relative_strength_20d, 0)}
                  </span>
                </a>
              ))}
            </div>
          </>
        )}

        {collapsed && (
          <span className="text-xs text-muted-foreground">
            {top.symbol} · 校准胜率 {pct(top.calibrated_probability, 0)} · <ConfidencePillInline badge={top.confidence_badge} />
          </span>
        )}

        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className={cn("rounded p-1 text-muted-foreground hover:text-foreground", collapsed ? "" : "ml-2 shrink-0")}
          title={collapsed ? "展开优先标的" : "收起"}
          aria-label={collapsed ? "展开优先标的" : "收起优先标的"}
        >
          {collapsed ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}
        </button>
      </div>
    </div>
  );
}

function ConfidencePillInline({ badge }: { badge: ConfidenceBadge }) {
  const b = BADGE[badge] ?? BADGE.experimental;
  return <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium", b.cls)}>{b.label}</span>;
}
