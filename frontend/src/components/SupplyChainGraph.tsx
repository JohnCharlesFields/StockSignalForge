import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { Building2, CheckCircle2, ExternalLink, GitBranch, List, Loader2, RefreshCw, Search, X } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { NetworkCompany } from "./CompanyNetwork";

export interface GraphMetrics {
  market_cap_usd?: number | null; market_cap_date?: string; market_cap_source?: string;
  pe?: number | null; pe_date?: string; pe_basis?: string; pe_source?: string;
  close?: number | null; price_date?: string; price_source?: string;
  return_3m_pct?: number | null; return_start_date?: string; return_end_date?: string;
  return_status?: string; price_basis?: string; stale?: boolean;
}
type Role = "target" | "upstream" | "downstream" | "competitor" | "industry";
export interface SupplyNode extends NetworkCompany { id: string; role: Role; name: string; metrics: GraphMetrics }
interface Edge { source: string; target: string; role: Role; directed: boolean; verified: boolean }
export interface SupplyGraph {
  symbol: string; as_of: string; relationship_as_of?: string; nodes: SupplyNode[]; edges: Edge[];
  needs_metrics: string[]; scope_note: string; return_note: string;
  refresh: { status?: string; finished_timestamp?: number; requested?: number; source_status?: string };
}
const roles: Record<Role, string> = { target: "当前公司", upstream: "上游供应商", downstream: "下游客户", competitor: "竞争对手", industry: "同业参考（非供应链）" };
const colors: Record<Role, string> = { target: "#f97316", upstream: "#0d9488", downstream: "#2563eb", competitor: "#8b5cf6", industry: "#64748b" };
const money = (value?: number | null) => value == null ? "--" : `$${value.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
const cap = (value?: number | null) => value == null ? "--" : value >= 1e12 ? `$${(value / 1e12).toFixed(2)}T` : value >= 1e9 ? `$${(value / 1e9).toFixed(2)}B` : `$${(value / 1e6).toFixed(1)}M`;
const pe = (value?: number | null) => value == null ? "--" : value <= 0 ? "不适用" : `${value.toFixed(1)}x`;
const pct = (value?: number | null) => value == null ? "--" : `${value >= 0 ? "+" : ""}${value.toFixed(1)}%`;
const returnNotes: Record<string, string> = { history_missing: "暂无历史日线", history_insufficient: "三个月历史行情不足或不连续", price_jump_needs_review: "价格出现大幅跳变，需核验拆股或数据异常", raw_price_change: "未复权价格变动，不含股息" };

function Diagram({ graph, nodes, selected, onSelect }: { graph: SupplyGraph; nodes: SupplyNode[]; selected?: string; onSelect: (id: string) => void }) {
  const figure = useRef<HTMLDivElement>(null);
  const elements = useRef<Record<string, HTMLButtonElement | null>>({});
  const marker = useId().replace(/:/g, "");
  const [lines, setLines] = useState<{ edge: Edge; path: string }[]>([]);
  useLayoutEffect(() => {
    let raf = 0;
    const draw = () => {
      const area = figure.current?.getBoundingClientRect();
      if (!area) return;
      const out: { edge: Edge; path: string }[] = [];
      for (const edge of graph.edges) {
        const from = elements.current[edge.source]?.getBoundingClientRect();
        const to = elements.current[edge.target]?.getBoundingClientRect();
        if (!from || !to) continue;
        if (edge.role === "competitor") {
          const x1 = from.left + from.width / 2 - area.left, y1 = from.bottom - area.top;
          const x2 = to.left + to.width / 2 - area.left, y2 = to.top - area.top;
          out.push({ edge, path: `M${x1},${y1} C${x1},${y1 + 30} ${x2},${y2 - 25} ${x2},${y2}` });
        } else {
          const x1 = from.right - area.left, y1 = from.top + from.height / 2 - area.top;
          const x2 = to.left - area.left, y2 = to.top + to.height / 2 - area.top;
          const mid = (x1 + x2) / 2;
          out.push({ edge, path: `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}` });
        }
      }
      setLines(out);
    };
    const schedule = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(draw); };
    const observer = new ResizeObserver(schedule);
    if (figure.current) observer.observe(figure.current);
    Object.values(elements.current).forEach(element => { if (element) observer.observe(element); });
    schedule();
    return () => { cancelAnimationFrame(raf); observer.disconnect(); };
  }, [graph.edges, nodes]);

  const card = (node: SupplyNode) => <button key={node.id} ref={element => { elements.current[node.id] = element; }}
    type="button" aria-label={`选择 ${node.name} ${node.symbol || ""}`} onClick={() => onSelect(node.id)}
    className={cn("relative z-10 w-full min-w-0 rounded border bg-card p-2 text-left shadow-sm transition-colors hover:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary", selected === node.id && "border-primary ring-1 ring-primary")}
    style={{ borderTop: `3px solid ${colors[node.role]}` }}>
    <div className="line-clamp-2 min-h-9 break-words text-xs font-semibold" title={node.name}>{node.name}</div>
    <div className="mt-1 flex flex-wrap items-center gap-1 text-[10px] text-muted-foreground"><span className="font-mono font-semibold text-foreground">{node.symbol || "未匹配证券"}</span>{node.market && <span>{node.market === "US" ? "美股" : node.market === "CN" ? "A股" : "港股"}</span>}</div>
    <dl className="mt-2 space-y-1 text-[11px] tabular-nums">
      <div className="flex flex-wrap justify-between gap-x-1"><dt className="text-muted-foreground">市值</dt><dd>{cap(node.metrics.market_cap_usd)}</dd></div>
      <div className="flex flex-wrap justify-between gap-x-1"><dt className="text-muted-foreground">PE</dt><dd>{pe(node.metrics.pe)}</dd></div>
      <div className="flex flex-wrap justify-between gap-x-1"><dt className="text-muted-foreground">近3月</dt><dd className={node.metrics.return_3m_pct == null ? "" : node.metrics.return_3m_pct >= 0 ? "text-emerald-700 dark:text-emerald-300" : "text-rose-700 dark:text-rose-300"}>{pct(node.metrics.return_3m_pct)}</dd></div>
    </dl>
    {node.role !== "target" && <p className="mt-2 text-[10px] text-muted-foreground">{node.role === "industry" ? "行业分类参考" : node.relationship_status === "disclosed" ? "已披露关系" : node.relationship_status === "source_supported" ? "来源佐证·待人工复核" : "关系待核验"}</p>}
  </button>;
  const group = (role: Role, main = false) => {
    const items = nodes.filter(node => node.role === role);
    return <section key={role} className={cn("relative z-10 min-w-0", !main && "mt-8 border-t pt-4")}>
      <h3 className="mb-4 text-xs font-semibold" style={{ color: colors[role] }}>{roles[role]} {items.length ? `· ${items.length}` : ""}</h3>
      <div className={main ? "space-y-4" : "grid grid-cols-2 gap-4 xl:grid-cols-3"}>{items.map(card)}</div>
      {!items.length && <p className="rounded border border-dashed bg-background p-3 text-[11px] leading-5 text-muted-foreground">{role === "target" ? "--" : "当前资料未提供公司，不代表该关系不存在。"}</p>}
    </section>;
  };
  return <div ref={figure} className="relative min-w-0 p-4">
    <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true">
      <defs>{(["upstream", "downstream"] as Role[]).map(role => <marker key={role} id={`${marker}-${role}`} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill={colors[role]} /></marker>)}</defs>
      {lines.map(({ edge, path }) => <path key={`${edge.source}-${edge.target}`} d={path} fill="none" stroke={colors[edge.role]} strokeWidth="1.6"
        strokeDasharray={edge.role === "competitor" ? "3 5" : edge.verified ? undefined : "6 4"}
        opacity={selected && selected !== edge.source && selected !== edge.target ? 0.2 : 0.65}
        markerEnd={edge.directed ? `url(#${marker}-${edge.role})` : undefined} />)}
    </svg>
    <div className="grid grid-cols-3 gap-5 sm:gap-10">
      {group("upstream", true)}
      <div className="flex min-w-0 flex-col justify-center py-10">{group("target", true)}</div>
      {group("downstream", true)}
    </div>
    {group("competitor")}
    {group("industry")}
  </div>;
}

export function SupplyChainGraph({ symbol, onClose, onRefreshCompany }: { symbol: string; onClose: () => void; onRefreshCompany: () => void }) {
  const [data, setData] = useState<SupplyGraph>();
  const [selected, setSelected] = useState<string>();
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<"graph" | "list">("graph");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [mobileDetails, setMobileDetails] = useState(false);
  const modal = useRef<HTMLDivElement>(null);
  const refresh = async () => {
    setBusy(true); setError("");
    try {
      const state = await api.refreshSupplyChain(symbol);
      if (state.status === "busy") setError("公司更新任务繁忙，请稍后重试；已有图谱保留。");
      setData(await api.getSupplyChain(symbol));
    } catch (e) { setError(e instanceof Error ? e.message : "指标补齐失败"); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    let alive = true, polling = false, timer = 0;
    const controller = new AbortController();
    const opened = Date.now();
    api.getSupplyChain(symbol, controller.signal).then(async value => {
      if (!alive) return;
      setData(value); setSelected(value.nodes.find(n => n.role === "target")?.id);
      if (value.needs_metrics.length && !["running", "queued"].includes(value.refresh.status || "")) {
        await api.refreshSupplyChain(symbol, controller.signal);
        if (alive) setData(await api.getSupplyChain(symbol, controller.signal));
      }
    }).catch(e => { if (alive) setError(e instanceof Error ? e.message : "图谱读取失败"); });
    timer = window.setInterval(async () => {
      if (polling || !alive || Date.now() - opened > 180000) return;
      polling = true;
      try { const value = await api.getSupplyChain(symbol, controller.signal); if (alive) setData(value); }
      catch { /* Preserve readable graph during transient errors. */ }
      finally { polling = false; }
    }, 5000);
    return () => { alive = false; controller.abort(); window.clearInterval(timer); };
  }, [symbol]);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    modal.current?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key !== "Tab" || !modal.current) return;
      const buttons = [...modal.current.querySelectorAll<HTMLElement>('button:not([disabled]),a[href],input')].filter(element => element.getClientRects().length > 0);
      if (!buttons.length) return;
      const first = buttons[0], last = buttons[buttons.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === modal.current)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", key);
    return () => { document.body.style.overflow = overflow; document.removeEventListener("keydown", key); previous?.focus(); };
  }, [onClose]);
  const nodes = useMemo(() => (data?.nodes || []).filter(n => n.role === "target" || `${n.name} ${n.symbol || ""}`.toLowerCase().includes(query.toLowerCase())), [data, query]);
  const node = data?.nodes.find(n => n.id === selected);
  const m = node?.metrics || {};
  const updating = busy || ["running", "queued"].includes(data?.refresh.status || "");
  return createPortal(<div className="fixed inset-0 z-[80] bg-background/80 p-2 backdrop-blur-sm sm:p-4">
    <div ref={modal} role="dialog" aria-modal="true" aria-label={`${symbol} 供应链图谱`} tabIndex={-1} className="flex h-full min-w-0 flex-col overflow-hidden rounded-md border bg-background shadow-xl outline-none">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3">
        <div><h2 className="flex items-center gap-2 text-lg font-semibold"><GitBranch className="h-5 w-5 text-primary" />{symbol} · 供应链图谱</h2><p className="mt-1 text-xs text-muted-foreground">关系资料 {data?.relationship_as_of || "日期待核验"} · 指标参考交易日 {data?.as_of || "--"}</p>{["partial", "failed", "interrupted"].includes(data?.refresh.status || "") && <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">部分指标暂不可用，保留已有资料与原始日期。</p>}</div>
        <div className="flex items-center gap-2">
          <button type="button" disabled={updating} onClick={refresh} className="inline-flex min-h-9 items-center gap-1.5 rounded border px-3 text-xs disabled:opacity-50"><RefreshCw className={cn("h-4 w-4", updating && "animate-spin")} />{updating ? "补齐中" : "补齐指标"}</button>
          <button type="button" onClick={onClose} aria-label="关闭供应链图谱" title="关闭" className="flex h-9 w-9 items-center justify-center rounded hover:bg-muted"><X className="h-5 w-5" /></button>
        </div>
      </header>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-2">
        <div className="flex items-center gap-1 rounded border p-0.5">{(["graph", "list"] as const).map(view => <button key={view} type="button" aria-pressed={tab === view} onClick={() => setTab(view)} className={cn("inline-flex min-h-8 items-center gap-1.5 rounded px-3 text-xs", tab === view ? "bg-primary text-primary-foreground" : "hover:bg-muted")}>{view === "graph" ? <GitBranch className="h-3.5 w-3.5" /> : <List className="h-3.5 w-3.5" />}{view === "graph" ? "图谱" : "公司清单"}</button>)}</div>
        <label className="flex min-w-0 items-center gap-2"><Search className="h-4 w-4 text-muted-foreground" /><input aria-label="搜索图谱公司" placeholder="公司名称 / 代码" value={query} onChange={e => setQuery(e.target.value)} className="h-8 w-44 max-w-full rounded border bg-background px-2 text-xs" /></label>
        <div className="flex flex-wrap gap-3 text-[11px] text-muted-foreground"><span>实线：已披露供应关系</span><span>虚线：供应关系待核验</span><span>点线：竞争关系</span></div>
      </div>
      {error && <p role="alert" className="border-b bg-amber-500/10 px-4 py-2 text-xs text-amber-700 dark:text-amber-300">{error}</p>}
      <div className="grid min-h-0 flex-1 grid-cols-1 overflow-y-auto lg:grid-cols-[minmax(0,1fr)_300px] lg:overflow-hidden">
        <main className="min-w-0 lg:overflow-y-auto">
          {!data && <div className="flex min-h-64 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-5 w-5 animate-spin" />读取已归档供应链</div>}
          {data && tab === "graph" && <Diagram graph={data} nodes={nodes} selected={selected} onSelect={id => { setSelected(id); setMobileDetails(true); }} />}
          {data && tab === "list" && <div className="p-4"><div className="space-y-3">{nodes.map(n => <button key={n.id} type="button" onClick={() => { setSelected(n.id); setMobileDetails(true); }} className={cn("grid w-full min-w-0 grid-cols-[minmax(0,1fr)_80px] gap-2 rounded border px-3 py-2 text-left text-xs sm:grid-cols-[minmax(0,1fr)_90px_75px_75px]", selected === n.id && "border-primary")}>
            <div className="min-w-0 break-words"><strong>{n.name}</strong><span className="ml-2 font-mono text-muted-foreground">{n.symbol || "--"}</span><p className="mt-1 text-muted-foreground">{roles[n.role]}</p></div>
            <div>市值<p className="mt-1 font-medium">{cap(n.metrics.market_cap_usd)}</p></div><div>PE<p className="mt-1 font-medium">{pe(n.metrics.pe)}</p></div><div>近3月<p className="mt-1 font-medium">{pct(n.metrics.return_3m_pct)}</p></div>
          </button>)}</div></div>}
        </main>
        <aside className={cn("fixed inset-x-2 bottom-2 z-[90] max-h-[65vh] min-w-0 overflow-y-auto rounded border bg-background p-4 shadow-xl lg:static lg:z-auto lg:max-h-none lg:rounded-none lg:border-0 lg:border-l lg:bg-muted/20 lg:shadow-none", !mobileDetails && "hidden lg:block")}>
          <button type="button" aria-label="收起公司详情" title="收起公司详情" onClick={() => setMobileDetails(false)} className="float-right flex h-8 w-8 items-center justify-center rounded hover:bg-muted lg:hidden"><X className="h-4 w-4" /></button>
          {node && <><div className="flex items-center gap-2 text-xs text-muted-foreground"><Building2 className="h-4 w-4" />{roles[node.role]}</div><h3 className="mt-2 break-words text-base font-semibold">{node.name}</h3><p className="mt-1 font-mono text-xs text-muted-foreground">{node.symbol || "证券身份未匹配"}</p>
            {node.role !== "target" && <p className="mt-3 text-xs text-muted-foreground">{node.role === "industry" ? "仅行业分类参考，不推定供应或竞争关系" : node.relationship_status === "disclosed" ? "已披露关系" : "关系待核验"}{node.listing_verified ? " · 证券身份已核验" : " · 证券身份待核验"}</p>}
            <dl className="mt-4 divide-y text-xs">
              {[["日收盘参考", money(m.close), m.price_date], ["市值（美元）", cap(m.market_cap_usd), m.market_cap_date], ["PE", pe(m.pe), m.pe_date], ["近3月价格变动", pct(m.return_3m_pct), m.return_start_date && `${m.return_start_date} 至 ${m.return_end_date}`]].map(([label, value, date]) => <div key={label} className="py-3"><dt className="text-muted-foreground">{label}</dt><dd className="mt-1 text-base font-semibold tabular-nums">{value}</dd><p className="mt-1 text-[10px] text-muted-foreground">{date || "日期未核验"}</p></div>)}
            </dl>
            {m.pe != null && <p className="mt-1 text-[11px] text-muted-foreground">{m.pe_basis}{m.pe <= 0 ? ` · 原始值 ${m.pe}` : ""}</p>}
            <div className="mt-3 space-y-1 break-words text-[10px] text-muted-foreground"><p>行情来源：{m.price_source || "未提供"}</p><p>市值来源：{m.market_cap_source || "未提供"}</p><p>PE来源：{m.pe_source || "未提供"}</p></div>
            <p className="mt-3 text-[11px] text-muted-foreground">{returnNotes[m.return_status || ""] || (node.market !== "US" ? "非美股指标本版暂未核验，不套用美股数据。" : "尚无可用行情指标。")}</p>
            {m.stale && <p className="mt-2 text-[11px] text-amber-700 dark:text-amber-300">部分指标不是参考交易日数据，保留各自原始日期。</p>}
            {node.detail && <p className="mt-4 text-xs leading-5">{node.detail}</p>}
            {node.evidence_note && <p className="mt-2 text-[11px] text-muted-foreground">{node.evidence_note}</p>}
            {node.source_url && <a href={node.source_url} target="_blank" rel="noopener noreferrer" className="mt-3 inline-flex items-center gap-1 text-xs text-primary"><CheckCircle2 className="h-3.5 w-3.5" />{node.source_title || "关系来源"} · {node.evidence_date || "日期未知"}</a>}
            <div className="mt-4 flex flex-wrap gap-2">{node.market === "US" && (node.listing_verified || node.role === "target") && node.symbol ? <Link onClick={onClose} to={`/single-stock-overnight?symbol=${encodeURIComponent(node.symbol)}`} className="rounded bg-primary px-3 py-2 text-xs text-primary-foreground">个股研判</Link> : node.website ? <a href={node.website} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 rounded border px-3 py-2 text-xs">公司外部资料<ExternalLink className="h-3 w-3" /></a> : null}</div>
          </>}
          {data && data.nodes.length === 1 && <button type="button" onClick={onRefreshCompany} className="mt-4 inline-flex items-center gap-1 rounded border px-3 py-2 text-xs"><RefreshCw className="h-3.5 w-3.5" />更新公司关系资料</button>}
        </aside>
      </div>
      <footer className="shrink-0 space-y-1 border-t px-4 py-2 text-[10px] leading-4 text-muted-foreground"><p>{data?.scope_note || "供应链关系与证券身份分别核验。"}</p><p>{data?.return_note} · 本版只作研究资料，不改变胜率或排序。</p></footer>
    </div>
  </div>, document.body);
}
