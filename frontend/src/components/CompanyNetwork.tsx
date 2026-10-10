import { ExternalLink } from "lucide-react";
import { Link } from "react-router-dom";

export interface NetworkCompany {
  name?: string;
  display_name?: string;
  symbol?: string;
  market?: "US" | "CN" | "HK";
  listing_verified?: boolean;
  website?: string | null;
  relationship_status?: string;
  detail?: string;
  source_title?: string;
  source_url?: string | null;
  evidence_date?: string;
  evidence_note?: string;
  source_quote?: string;
  market_cap_usd?: number;
  market_cap_date?: string;
  match_basis?: string[];
  peer_type?: string;
}

export interface CompanyNetwork {
  status?: string;
  refresh_status?: string;
  as_of?: string;
  stale?: boolean;
  relationships_retained_as_of?: string;
  relationships?: Record<string, NetworkCompany[]>;
  upstream_note?: string;
  industry_peers?: NetworkCompany[];
  industry_candidate_count?: number;
  market_cap_checked_count?: number;
  coverage_note?: string;
}

export function legacyCompanyRows(items?: string[]): NetworkCompany[] {
  return (items || []).filter((name) => name && !name.includes("推断") && !/(运营商|供应商|设备商|制造商|行业客户)$/.test(name))
    .map((name) => ({ name, display_name: name, relationship_status: "ai_unverified", listing_verified: false }));
}

function CompanyName({ company }: { company: NetworkCompany }) {
  const label = company.display_name || company.name || "公司名称待核验";
  const content = <>{label}{company.symbol && <span className="ml-1.5 text-[11px] font-normal opacity-75">{company.symbol}</span>}</>;
  if (company.listing_verified && company.market === "US" && company.symbol) {
    return <Link className="font-medium text-primary hover:underline break-words" to={`/single-stock-overnight?symbol=${encodeURIComponent(company.symbol)}`}>{content}</Link>;
  }
  if (company.website) {
    return <a className="font-medium text-primary hover:underline break-words" href={company.website} target="_blank" rel="noopener noreferrer" title="公司官网（外部资料）">{content}<ExternalLink className="ml-1 inline h-3 w-3" /></a>;
  }
  return <span className="font-medium break-words">{content}</span>;
}

export function CompanyRelations({ items, emptyNote, status }: { items?: NetworkCompany[]; emptyNote?: string; status?: string }) {
  if (!items?.length) {
    const text = ["queued", "running", "loading"].includes(status || "") ? "公司关系资料待补齐，后台更新中。"
      : status === "not_updated" ? "公司关系资料尚未更新，可点击更新公司资料。"
      : ["partial", "unavailable", "failed"].includes(status || "") ? "本次资料获取未完成，可重试公司资料；不代表没有此类关系。"
      : emptyNote || "当前可用资料未提供具体公司；不代表该关系不存在。";
    return <p className="text-xs leading-5 text-muted-foreground">{text}</p>;
  }
  return <div className="flex flex-wrap items-start gap-2">
    {items.map((company, i) => <div key={`${company.market}-${company.symbol || company.name}-${i}`} className="max-w-full rounded border px-2.5 py-1.5 text-xs space-y-1">
      <CompanyName company={company} />
      <div className="text-[10px] text-muted-foreground">
        {company.market === "CN" ? "A股 · 外部资料" : company.market === "HK" ? "港股 · 外部资料" : company.listing_verified ? "美股身份已核验" : "上市身份未核验"}
        {" · "}{company.relationship_status === "disclosed" ? "披露关系" : company.relationship_status === "source_supported" ? "来源佐证·待人工复核" : "关系待核验"}
      </div>
      {company.detail && <p className="max-w-64 leading-4 text-muted-foreground">{company.detail}</p>}
      {company.evidence_note && <p className="max-w-64 text-[10px] leading-4 text-muted-foreground">{company.evidence_note}</p>}
      {company.source_url && <a className="block text-[10px] text-primary hover:underline" href={company.source_url} target="_blank" rel="noopener noreferrer">{company.source_title || "披露来源"} · {company.evidence_date || "日期未知"}</a>}
    </div>)}
  </div>;
}

export function IndustryCompanies({ network, status }: { network?: CompanyNetwork; status?: string }) {
  const rows = network?.industry_peers || [];
  return <div className="space-y-2">
    {network?.refresh_status === "running" && <p className="text-xs text-muted-foreground">行业公司资料后台更新中</p>}
    {rows.length ? <div className="divide-y divide-border/60">
      {rows.map((row) => <div key={row.symbol} className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 py-1.5">
        <div className="min-w-0 flex-1 basis-48"><CompanyName company={row} /><p className="mt-0.5 text-[11px] text-muted-foreground">{row.peer_type === "curated_track_peer" ? "同细分赛道（目录匹配）" : "行业参考，非直接竞对判定"} · {row.match_basis?.join(" / ")}</p></div>
        <div className="shrink-0 text-right text-xs"><strong className="tabular-nums">${((row.market_cap_usd || 0) / 1e9).toFixed(2)}B</strong><p className="mt-0.5 text-[10px] text-muted-foreground">{row.market_cap_date}</p></div>
      </div>)}
    </div> : <p className="text-xs leading-5 text-muted-foreground">{["queued", "running", "loading"].includes(status || network?.status || "") ? "同行分类与市值正在后台核验。" : (status || network?.status) === "not_updated" ? "同行参考尚未更新，可点击更新公司资料。" : ["partial", "failed", "unavailable"].includes(status || network?.status || "") ? "本次同行资料核验未完成，可重试公司资料。" : "当前归档范围内暂无同日市值已核验的可比公司；不代表没有同行。"}</p>}
    {network?.coverage_note && <p className="text-[11px] leading-4 text-muted-foreground">{network.coverage_note} · 市值查询 {network.market_cap_checked_count ?? 0} / 行业候选 {network.industry_candidate_count ?? 0} · {network.as_of}{network.stale ? " · 非最近交易日" : ""}</p>}
  </div>;
}
