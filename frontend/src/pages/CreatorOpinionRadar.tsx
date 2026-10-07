import { useEffect, useMemo, useState } from "react";
import type React from "react";
import { ExternalLink, RefreshCw, Radio, TrendingDown, TrendingUp, Users } from "lucide-react";
import { api, type CreatorChannel, type CreatorOpinionFeedItem, type CreatorSectorSignal } from "@/lib/api";
import { cn } from "@/lib/utils";

function stanceTone(stance?: string) {
  const s = String(stance || "").toLowerCase();
  if (s.includes("bull") || s.includes("多") || s.includes("positive")) return "good";
  if (s.includes("bear") || s.includes("空") || s.includes("negative")) return "bad";
  if (s.includes("mixed")) return "warn";
  return "neutral";
}

function Badge({ children, tone = "neutral", title }: { children: React.ReactNode; tone?: string; title?: string }) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium",
        tone === "good" && "bg-emerald-500/15 text-emerald-300",
        tone === "bad" && "bg-rose-500/15 text-rose-300",
        tone === "warn" && "bg-amber-500/15 text-amber-200",
        tone === "neutral" && "bg-muted text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

function fmtDate(value?: string | null) {
  if (!value) return "--";
  try {
    return new Date(value).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return value;
  }
}

function compactNumber(value?: number | null) {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  return Math.round(value).toLocaleString("en-US");
}

export function CreatorOpinionRadar() {
  const [channels, setChannels] = useState<CreatorChannel[]>([]);
  const [items, setItems] = useState<CreatorOpinionFeedItem[]>([]);
  const [signals, setSignals] = useState<CreatorSectorSignal[]>([]);
  const [latestDate, setLatestDate] = useState<string | null>(null);
  const [previousDate, setPreviousDate] = useState<string | null>(null);
  const [latestCount, setLatestCount] = useState(0);
  const [previousCount, setPreviousCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [useLlm, setUseLlm] = useState(true);
  const [lastRun, setLastRun] = useState<string>("");

  const load = async () => {
    setLoading(true);
    try {
      const [c, f, s] = await Promise.all([
        api.listCreatorChannels(),
        api.getCreatorOpinionFeed(80),
        api.getCreatorSectorSignals(7, true),
      ]);
      setChannels(c.channels || []);
      setItems(f.items || []);
      setSignals(s.signals || []);
      setLatestDate(f.latest_date || null);
      setPreviousDate(f.previous_date || null);
      setLatestCount(f.latest_count || 0);
      setPreviousCount(f.previous_count || 0);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load().catch(() => setLoading(false));
  }, []);

  const refresh = async () => {
    setRunning(true);
    setLastRun("");
    try {
      const res = await api.refreshCreatorOpinions({ limit_per_channel: 1, use_llm: useLlm, force: false });
      setLastRun(`新增视频 ${res.new_videos ?? 0} 条 · 字幕 ${res.transcripts_ready ?? 0} 条 · 观点 ${res.opinions_ready ?? 0} 条`);
      await load();
    } catch (error) {
      setLastRun(error instanceof Error ? error.message : "刷新失败");
    } finally {
      setRunning(false);
    }
  };

  const stats = useMemo(() => {
    const ready = items.filter((x) => x.opinion?.available).length;
    const transcriptReady = items.filter((x) => x.transcript_status === "ready").length;
    const tickerMentions = new Set<string>();
    const sectorMentions = new Set<string>();
    items.forEach((item) => {
      item.opinion?.ticker_views?.forEach((v) => v.ticker && tickerMentions.add(v.ticker));
      item.opinion?.sector_views?.forEach((v) => v.sector_name && sectorMentions.add(v.sector_name));
    });
    return { ready, transcriptReady, tickerMentions: tickerMentions.size, sectorMentions: sectorMentions.size };
  }, [items]);

  return (
    <div className="min-h-screen space-y-4 bg-background p-4 text-foreground">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <Radio className="h-5 w-5 text-primary" />
            <h1 className="text-xl font-semibold">博主观点雷达</h1>
          </div>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            每日抓取已配置 YouTube 博主的新视频，提炼个股观点与赛道传导。当前只作为软证据与人工复核标签，不直接改变胜率。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground">
            <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />
            DeepSeek 提炼
          </label>
          <button
            type="button"
            onClick={refresh}
            disabled={running}
            className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-60"
          >
            <RefreshCw className={cn("h-4 w-4", running && "animate-spin")} />
            更新最新视频
          </button>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        <div className="rounded-md border bg-card p-3">
          <div className="text-xs text-muted-foreground">已配置博主</div>
          <div className="mt-1 text-2xl font-semibold">{channels.length}</div>
        </div>
        <div className="rounded-md border bg-card p-3">
          <div className="text-xs text-muted-foreground">最新日 / 前一日</div>
          <div className="mt-1 text-sm font-medium">{latestDate || "--"} / {previousDate || "--"}</div>
          <div className="mt-1 text-xs text-muted-foreground">新增 {latestCount} · 前日 {previousCount}</div>
        </div>
        <div className="rounded-md border bg-card p-3">
          <div className="text-xs text-muted-foreground">字幕 / 观点</div>
          <div className="mt-1 text-2xl font-semibold">{stats.transcriptReady} / {stats.ready}</div>
        </div>
        <div className="rounded-md border bg-card p-3">
          <div className="text-xs text-muted-foreground">提及标的 / 赛道</div>
          <div className="mt-1 text-2xl font-semibold">{stats.tickerMentions} / {stats.sectorMentions}</div>
        </div>
      </div>

      {lastRun && <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">{lastRun}</div>}

      <section className="rounded-md border bg-card">
        <div className="flex items-center justify-between border-b px-3 py-2">
          <div className="flex items-center gap-2 font-medium">
            <TrendingUp className="h-4 w-4 text-emerald-300" />
            赛道观点传导
          </div>
          <div className="text-xs text-muted-foreground">按近 7 日博主观点聚合</div>
        </div>
        <div className="grid gap-2 p-3 md:grid-cols-2 xl:grid-cols-3">
          {signals.length === 0 && <div className="text-sm text-muted-foreground">暂无赛道信号，先点击“更新最新视频”。</div>}
          {signals.map((s) => (
            <div key={`${s.as_of_date}-${s.sector_key}`} className="rounded-md border bg-background/60 p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="font-medium">{s.sector_name}</div>
                <Badge tone={stanceTone(s.stance)}>{s.stance || "mixed"}</Badge>
              </div>
              <div className="mt-2 flex flex-wrap gap-1">
                <Badge tone="neutral">提及 {s.mentions ?? 0}</Badge>
                <Badge tone="good">多 {s.bullish ?? 0}</Badge>
                <Badge tone="bad">空 {s.bearish ?? 0}</Badge>
                <Badge tone="neutral">分 {s.sentiment_score ?? "--"}</Badge>
              </div>
              <div className="mt-2 text-xs text-muted-foreground">博主：{(s.creators || []).join(", ") || "--"}</div>
              <div className="mt-1 text-xs text-muted-foreground">龙头/关联：{(s.leader_tickers || []).join(", ") || "--"}</div>
              {s.evidence?.[0] && <p className="mt-2 line-clamp-3 text-xs leading-5 text-muted-foreground">{s.evidence[0]}</p>}
            </div>
          ))}
        </div>
      </section>

      <section className="rounded-md border bg-card">
        <div className="flex items-center justify-between border-b px-3 py-2">
          <div className="flex items-center gap-2 font-medium">
            <Users className="h-4 w-4 text-primary" />
            最新视频与观点
          </div>
          {loading && <div className="text-xs text-muted-foreground">加载中...</div>}
        </div>
        <div className="divide-y">
          {items.length === 0 && <div className="p-4 text-sm text-muted-foreground">暂无视频记录。</div>}
          {items.map((item) => {
            const opinion = item.opinion || {};
            return (
              <article key={item.video_id} className="space-y-3 p-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold">{item.handle}</span>
                      <Badge tone={item.transcript_status === "ready" ? "good" : "warn"}>字幕 {item.transcript_status}</Badge>
                      <Badge tone={opinion.available ? "good" : "warn"}>观点 {item.opinion_status}</Badge>
                      {item.language && <Badge>{item.language}</Badge>}
                      {item.chars ? <Badge>{compactNumber(item.chars)} 字</Badge> : null}
                    </div>
                    <h2 className="mt-1 break-words text-base font-medium">{item.title}</h2>
                    <div className="mt-1 text-xs text-muted-foreground">{fmtDate(item.published_at)}</div>
                  </div>
                  <a href={item.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sm text-primary">
                    YouTube <ExternalLink className="h-3.5 w-3.5" />
                  </a>
                </div>

                {opinion.summary && <p className="rounded-md bg-muted/35 p-2 text-sm leading-6 text-muted-foreground">{opinion.summary}</p>}
                {opinion.reason && <p className="rounded-md bg-amber-500/10 p-2 text-sm text-amber-200">{opinion.reason}</p>}

                <div className="grid gap-3 lg:grid-cols-2">
                  <div>
                    <div className="mb-1 flex items-center gap-1 text-sm font-medium">
                      <TrendingUp className="h-4 w-4" />
                      个股观点
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {(opinion.ticker_views || []).length === 0 && <span className="text-xs text-muted-foreground">暂无明确标的</span>}
                      {(opinion.ticker_views || []).slice(0, 14).map((view, idx) => (
                        <Badge key={`${view.ticker}-${idx}`} tone={stanceTone(view.stance)} title={view.evidence || view.risk}>
                          {view.ticker || "--"} · {view.stance || "neutral"}
                        </Badge>
                      ))}
                    </div>
                  </div>
                  <div>
                    <div className="mb-1 flex items-center gap-1 text-sm font-medium">
                      <TrendingDown className="h-4 w-4" />
                      赛道传导
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {(opinion.sector_views || []).length === 0 && <span className="text-xs text-muted-foreground">暂无明确赛道</span>}
                      {(opinion.sector_views || []).slice(0, 10).map((view, idx) => (
                        <Badge key={`${view.sector_key}-${idx}`} tone={stanceTone(view.stance)} title={view.evidence || view.risk}>
                          {view.sector_name || view.sector_key || "--"} · {view.stance || "mixed"}
                        </Badge>
                      ))}
                    </div>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      </section>
    </div>
  );
}
