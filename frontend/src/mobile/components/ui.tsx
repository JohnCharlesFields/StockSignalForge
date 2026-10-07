import type { ReactNode } from "react";
import { AlertTriangle, Inbox, Loader2, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";

// ---- formatting helpers (self-contained, mirror the desktop pages) ----

export function pct(v: number | null | undefined, d = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "--";
  return `${(v * 100).toFixed(d)}%`;
}

export function signedPct(v: number | null | undefined, d = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "--";
  return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(d)}%`;
}

export function money(v: number | null | undefined, d = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "--";
  return `$${v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d })}`;
}

export function advShort(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "--";
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(0)}M`;
  return `$${(v / 1e3).toFixed(0)}K`;
}

/** US convention: green = up, red = down. */
export function moveClass(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "text-muted-foreground";
  if (v > 0) return "text-emerald-600 dark:text-emerald-400";
  if (v < 0) return "text-rose-600 dark:text-rose-400";
  return "text-muted-foreground";
}

// ---- layout primitives ----

export function MCard({ children, className, onClick }: { children: ReactNode; className?: string; onClick?: () => void }) {
  return (
    <div
      className={cn("rounded-xl border bg-card p-3.5 shadow-sm", onClick && "active:bg-muted/40", className)}
      onClick={onClick}
    >
      {children}
    </div>
  );
}

export function StatTile({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="rounded-lg border bg-muted/30 px-2.5 py-2">
      <div className="text-[11px] leading-tight text-muted-foreground">{label}</div>
      <div className={cn("mt-0.5 font-mono text-sm font-semibold tabular-nums", tone)}>{value}</div>
    </div>
  );
}

export function Chip({ children, tone = "neutral", title }: { children: ReactNode; tone?: "strong" | "good" | "warn" | "bad" | "neutral"; title?: string }) {
  const cls: Record<string, string> = {
    strong: "border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
    good: "border-green-500/30 bg-green-500/10 text-green-700 dark:text-green-300",
    warn: "border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-300",
    bad: "border-rose-500/40 bg-rose-500/15 text-rose-700 dark:text-rose-300",
    neutral: "border-slate-500/25 bg-slate-500/10 text-slate-600 dark:text-slate-300",
  };
  return (
    <span title={title} className={cn("inline-flex max-w-full items-center rounded-md border px-1.5 py-0.5 text-[11px] font-medium leading-tight", cls[tone])}>
      <span className="truncate">{children}</span>
    </span>
  );
}

// ---- async state views ----

export function LoadingState({ label = "加载中…" }: { label?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
      <Loader2 className="h-6 w-6 animate-spin" />
      {label}
    </div>
  );
}

export function EmptyState({ label = "暂无数据", hint }: { label?: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-16 text-center text-sm text-muted-foreground">
      <Inbox className="h-7 w-7 opacity-60" />
      <div>{label}</div>
      {hint && <div className="text-xs text-muted-foreground/70">{hint}</div>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-16 text-center">
      <AlertTriangle className="h-7 w-7 text-amber-500" />
      <div className="text-sm text-muted-foreground">{message || "加载失败"}</div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex min-h-[44px] items-center gap-2 rounded-lg border border-primary/40 bg-primary/10 px-4 text-sm font-medium text-primary active:bg-primary/20"
        >
          <RefreshCw className="h-4 w-4" /> 重试
        </button>
      )}
    </div>
  );
}
