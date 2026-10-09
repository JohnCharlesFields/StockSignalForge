export interface GildataCompany {
  name?: string;
  factset_industry?: string | null;
  sic_industry?: string | null;
  fetched_at?: string;
  vendor_updated_at?: string | null;
}

export interface GildataEstimate {
  kind: string;
  report_period: string;
  window_days: number;
  mean: number;
  count?: number | null;
  up_count?: number | null;
  down_count?: number | null;
  relative_dispersion?: number | null;
  mean_change_pct?: number | null;
  previous_as_of?: string | null;
}

export interface GildataEvidence {
  company?: GildataCompany | null;
  forecast?: { as_of?: string; fetched_at?: string; estimates?: GildataEstimate[] } | null;
  news?: { title: string; publisher?: string; provenance?: { reported_time?: string } }[];
}

function percent(v: number | null | undefined) {
  return v == null ? "--" : `${(v * 100).toFixed(1)}%`;
}

export function GildataEvidencePanel({ evidence, marketDate }: { evidence?: GildataEvidence; marketDate?: string | null }) {
  if (!evidence) return null;
  const company = evidence.company;
  const forecast = evidence.forecast;
  const estimates = forecast?.estimates || [];
  const shown = ["eps", "revenue"].flatMap((kind) => estimates.filter((r) => r.kind === kind).slice(0, 2));
  const old = forecast?.as_of && marketDate && forecast.as_of !== marketDate;
  return <div className="space-y-3 border-t py-3 text-sm">
    <div className="flex flex-wrap items-center gap-2">
      <h3 className="font-semibold">聚源研究证据</h3>
      <span className="text-xs text-muted-foreground">不计入胜率或排序权重</span>
    </div>
    {company && <div className="space-y-1">
      {company.factset_industry && <p>FactSet 行业：{company.factset_industry}</p>}
      {company.sic_industry && <p>SIC 行业：{company.sic_industry}</p>}
      <p className="text-xs text-muted-foreground">采集 {company.fetched_at?.slice(0, 10) || "--"} · 资料更新时间未提供 · 不替代细分同行配对</p>
    </div>}
    {shown.length > 0 && <div className="space-y-2">
      <p className={old ? "text-amber-600 dark:text-amber-300" : "text-muted-foreground"}>一致预期 · 数据 {forecast?.as_of} {old ? "· 非最近交易日" : ""}</p>
      {shown.map((r) => <div key={`${r.kind}:${r.report_period}:${r.window_days}`} className="space-y-1 border-l-2 border-border pl-3">
        <p className="font-medium">{r.kind === "eps" ? "EPS" : "营收"}预期 · 报告期截至 {r.report_period}</p>
        <p>{r.kind === "eps" ? `$${r.mean.toFixed(2)} / 股` : `$${(r.mean / 1e9).toFixed(2)}B`} · {r.count ?? "--"} 份预测 · 近 {r.window_days} 日统计</p>
        <p>上修 {r.up_count ?? "--"} / 下修 {r.down_count ?? "--"} · 预期分歧 {percent(r.relative_dispersion)}</p>
        <p className="text-xs text-muted-foreground">{r.previous_as_of ? `较归档 ${r.previous_as_of} 变化 ${percent(r.mean_change_pct)}` : "尚无可比前值，等待每日归档积累"}</p>
      </div>)}
      <p className="text-xs text-muted-foreground">分歧为标准差 / |均值|；修正次数不是上涨概率。报告期预期不是下次财报披露日期。</p>
    </div>}
  </div>;
}
