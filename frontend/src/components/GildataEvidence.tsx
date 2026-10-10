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

export function GildataIndustryInfo({ company }: { company?: GildataCompany | null }) {
  if (!company) return null;
  return <div className="space-y-1 text-xs leading-5 text-muted-foreground">
    {company.factset_industry && <p><span className="mr-2 font-medium text-foreground">FactSet</span>{company.factset_industry}</p>}
    {company.sic_industry && <p><span className="mr-2 font-medium text-foreground">SIC</span>{company.sic_industry}</p>}
    <p className="text-[11px]">聚源 · 采集 {company.fetched_at?.slice(0, 10) || "--"} · {company.vendor_updated_at ? `资料更新 ${company.vendor_updated_at.slice(0, 10)}` : "资料更新时间未提供"}</p>
  </div>;
}

export function GildataConsensusTable({ forecast, marketDate }: { forecast?: GildataEvidence["forecast"]; marketDate?: string | null }) {
  const estimates = forecast?.estimates || [];
  const shown = ["eps", "revenue"].flatMap(kind => estimates.filter(r => r.kind === kind).slice(0, 2));
  const periods = [...new Set(shown.map(r => `${r.report_period}:${r.window_days}`))].sort();
  if (!periods.length) return null;
  const old = Boolean(forecast?.as_of && marketDate && forecast.as_of !== marketDate);
  const cell = (r?: GildataEstimate) => r ? <div className="min-w-0 space-y-1">
    <span className="block text-[11px] text-muted-foreground sm:hidden">{r.kind === "eps" ? "EPS / 股" : "营收"}</span>
    <div className="text-base font-semibold tabular-nums text-foreground">{r.kind === "eps" ? `$${r.mean.toFixed(2)}` : `$${(r.mean / 1e9).toFixed(2)}B`}</div>
    <p className="text-xs text-muted-foreground">{r.count ?? "--"} 份预测 · 分歧 {percent(r.relative_dispersion)}</p>
    <p className="text-xs text-muted-foreground">上修 <span className="tabular-nums text-foreground">{r.up_count ?? "--"}</span> / 下修 <span className="tabular-nums text-foreground">{r.down_count ?? "--"}</span></p>
    {r.previous_as_of && <p className="text-[11px] text-muted-foreground">较 {r.previous_as_of}：{percent(r.mean_change_pct)}</p>}
  </div> : <p className="text-xs text-muted-foreground">暂无预期</p>;
  return <div className="min-w-0 space-y-2">
    <p className={`text-xs ${old ? "text-amber-600 dark:text-amber-300" : "text-muted-foreground"}`}>聚源 · 数据 {forecast?.as_of || "日期未知"}{old ? " · 非最近交易日" : ""}</p>
    <div className="hidden grid-cols-[minmax(6.5rem,0.7fr)_minmax(0,1fr)_minmax(0,1fr)] gap-3 border-b pb-2 text-[11px] font-medium text-muted-foreground sm:grid">
      <span>报告期截至</span><span>EPS / 股</span><span>营收</span>
    </div>
    <div className="divide-y divide-border/60">
      {periods.map(key => {
        const group = shown.filter(r => `${r.report_period}:${r.window_days}` === key);
        const first = group[0];
        return <div key={key} className="grid grid-cols-2 gap-3 py-3 sm:grid-cols-[minmax(6.5rem,0.7fr)_minmax(0,1fr)_minmax(0,1fr)]">
          <div className="col-span-2 min-w-0 text-xs sm:col-span-1"><span className="mr-1 text-muted-foreground sm:hidden">报告期截至</span><strong className="tabular-nums">{first.report_period}</strong><p className="mt-1 text-[11px] text-muted-foreground">近 {first.window_days} 日统计</p></div>
          {cell(group.find(r => r.kind === "eps"))}{cell(group.find(r => r.kind === "revenue"))}
        </div>;
      })}
    </div>
    <details className="text-[11px] leading-5 text-muted-foreground">
      <summary className="w-fit cursor-pointer hover:text-foreground">预期口径与历史对比</summary>
      <p className="mt-1">分歧 = 标准差 / |均值|；上修、下修为次数，不是涨跌概率。报告期不是下次财报披露日期，不计入胜率或排序权重。</p>
      {shown.some(r => !r.previous_as_of) && <p>尚无可比前值的项目暂不计算预期变化，等待每日归档积累。</p>}
    </details>
  </div>;
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
