import { useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, BrainCircuit, Download, Loader2, Play, RefreshCw, Signal, Target } from "lucide-react";
import { toast } from "sonner";
import { api, type EventCalibrationInfo, type EventDrivenScanResponse, type EventDrivenSignal, type ResearchUniverseOption } from "@/lib/api";
import { UniverseOptions } from "@/components/research/UniverseOptions";

function asNumber(value: string | number | undefined | null): number | null {
  if (value === undefined || value === null || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

function pct(value: string | number | undefined | null, digits = 1): string {
  const n = asNumber(value);
  return n === null ? "--" : `${(n * 100).toFixed(digits)}%`;
}

function num(value: string | number | undefined | null, digits = 2): string {
  const n = asNumber(value);
  return n === null ? "--" : n.toFixed(digits);
}

function money(value: string | number | undefined | null): string {
  const n = asNumber(value);
  return n === null || n <= 0 ? "--" : `$${n.toFixed(2)}`;
}

function signalText(row: EventDrivenSignal): string {
  if (row.signal_cn && ["强启动观察", "观察", "回避", "中性"].includes(row.signal_cn)) return row.signal_cn;
  if (row.signal === "strong_watch") return "强启动观察";
  if (row.signal === "watch") return "观察";
  if (row.signal === "avoid") return "回避";
  return "中性";
}

function signalTone(signal: string): string {
  if (signal === "strong_watch") return "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
  if (signal === "watch") return "bg-sky-500/10 text-sky-700 dark:text-sky-300";
  if (signal === "avoid") return "bg-rose-500/10 text-rose-700 dark:text-rose-300";
  return "bg-muted text-muted-foreground";
}

function calibrationLabel(item: EventCalibrationInfo): string {
  const ic = item.primary_ic === null || item.primary_ic === undefined ? "--" : item.primary_ic.toFixed(3);
  const horizon = item.primary_horizon || "5d";
  return `${item.run_id} | ${item.event_count} events | ${horizon} IC ${ic}`;
}

export function EventRadar() {
  const [universe, setUniverse] = useState("spx");
  const [universes, setUniverses] = useState<ResearchUniverseOption[]>([]);
  const [limit, setLimit] = useState(50);
  const [top, setTop] = useState(20);
  const [newsPerTicker, setNewsPerTicker] = useState(3);
  const [tickersText, setTickersText] = useState("");
  const [autoCalibration, setAutoCalibration] = useState(true);
  const [selectedCalibration, setSelectedCalibration] = useState("");
  const [calibrations, setCalibrations] = useState<EventCalibrationInfo[]>([]);
  const [result, setResult] = useState<EventDrivenScanResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [calibrationsLoading, setCalibrationsLoading] = useState(false);

  const selectedCalibrationInfo = useMemo(
    () => calibrations.find((item) => item.run_id === selectedCalibration),
    [calibrations, selectedCalibration],
  );

  const loadCalibrations = async () => {
    setCalibrationsLoading(true);
    try {
      const res = await api.listEventCalibrations({ usable_only: true, min_events: 20 });
      setCalibrations(res.calibrations || []);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "无法加载校准模型";
      toast.error(msg);
    } finally {
      setCalibrationsLoading(false);
    }
  };

  useEffect(() => {
    api.listResearchUniverses()
      .then((res) => setUniverses(res.universes || []))
      .catch((err) => toast.error(err instanceof Error ? err.message : "无法加载股票池"));
    loadCalibrations();
  }, []);

  const runScan = async () => {
    const tickers = tickersText
      .split(/[,;\s]+/)
      .map((x) => x.trim().toUpperCase())
      .filter(Boolean);
    setLoading(true);
    setResult(null);
    try {
      const res = await api.scanEventDrivenSp500({
        universe,
        tickers: tickers.length ? tickers : undefined,
        limit: tickers.length ? 0 : limit,
        top,
        news_per_ticker: newsPerTicker,
        workers: 12,
        sleep_seconds: 0,
        include_prices: true,
        auto_calibration: autoCalibration && !selectedCalibration,
        calibration_run_id: selectedCalibration || undefined,
        min_calibration_events: 20,
        timeout_seconds: 900,
      });
      setResult(res);
      toast.success(`事件扫描完成：${res.signal_count} 个候选`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "事件扫描失败";
      toast.error(msg);
    } finally {
      setLoading(false);
    }
  };

  const activeCalibration = result?.calibration || selectedCalibrationInfo || calibrations[0];
  const reportUrl = result ? `${result.report_pdf_url}${result.report_pdf_url.includes("?") ? "&" : "?"}download=1` : "";

  return (
    <div className="h-full overflow-y-auto bg-background">
      <main className="mx-auto max-w-7xl px-6 py-6 space-y-6">
        <section className="flex flex-col gap-4 border-b pb-5 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="mb-2 inline-flex items-center gap-2 text-xs font-medium text-primary">
              <Signal className="h-3.5 w-3.5" aria-hidden="true" />
              事件驱动选股
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">统一股票池短期启动信号雷达</h1>
            <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
              批量采集最新美股新闻，结合历史事件影响校准、价格趋势与成交量确认，输出短线观察候选。
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={loadCalibrations}
              disabled={calibrationsLoading}
              className="inline-flex items-center gap-2 whitespace-nowrap rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-60"
            >
              {calibrationsLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              刷新校准
            </button>
            <button
              type="button"
              onClick={runScan}
              disabled={loading}
              className="inline-flex items-center gap-2 whitespace-nowrap rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
            >
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              运行扫描
            </button>
          </div>
        </section>

        <section className="grid gap-4 lg:grid-cols-[1.2fr_0.8fr]">
          <div className="rounded-lg border bg-card p-4">
            <h2 className="mb-4 flex items-center gap-2 text-base font-semibold">
              <Target className="h-4 w-4 text-primary" aria-hidden="true" />
              扫描参数
            </h2>
            <div className="grid gap-4 md:grid-cols-5">
              <label className="space-y-1 text-sm">
                <span className="text-muted-foreground">股票池</span>
                <select className="w-full rounded-md border bg-background px-3 py-2" value={universe} onChange={(e) => setUniverse(e.target.value)} disabled={!!tickersText.trim()}>
                  <UniverseOptions universes={universes} />
                </select>
              </label>
              <label className="space-y-1 text-sm">
                <span className="text-muted-foreground">扫描数量上限</span>
                <input className="w-full rounded-md border bg-background px-3 py-2" type="number" min={1} max={1000} value={limit} onChange={(e) => setLimit(Number(e.target.value) || 50)} disabled={!!tickersText.trim()} />
              </label>
              <label className="space-y-1 text-sm">
                <span className="text-muted-foreground">Top 候选</span>
                <input className="w-full rounded-md border bg-background px-3 py-2" type="number" min={1} max={100} value={top} onChange={(e) => setTop(Number(e.target.value) || 20)} />
              </label>
              <label className="space-y-1 text-sm">
                <span className="text-muted-foreground">每票新闻数</span>
                <input className="w-full rounded-md border bg-background px-3 py-2" type="number" min={1} max={20} value={newsPerTicker} onChange={(e) => setNewsPerTicker(Number(e.target.value) || 3)} />
              </label>
              <label className="flex items-end gap-2 text-sm">
                <input className="mb-3 h-4 w-4" type="checkbox" checked={autoCalibration} onChange={(e) => setAutoCalibration(e.target.checked)} disabled={!!selectedCalibration} />
                <span className="pb-2 text-muted-foreground">自动使用最佳校准</span>
              </label>
            </div>
            <label className="mt-4 block space-y-1 text-sm">
              <span className="text-muted-foreground">指定标的，可留空使用所选股票池</span>
              <textarea
                className="min-h-20 w-full rounded-md border bg-background px-3 py-2"
                value={tickersText}
                onChange={(e) => setTickersText(e.target.value)}
                placeholder="例如：AAPL, NVDA, MSFT；留空则扫描所选股票池前 N 只"
              />
            </label>
          </div>

          <div className="rounded-lg border bg-card p-4">
            <h2 className="mb-4 flex items-center gap-2 text-base font-semibold">
              <BrainCircuit className="h-4 w-4 text-primary" aria-hidden="true" />
              历史影响校准
            </h2>
            <select
              className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              value={selectedCalibration}
              onChange={(e) => {
                setSelectedCalibration(e.target.value);
                if (e.target.value) setAutoCalibration(false);
              }}
            >
              <option value="">自动选择最佳可用模型</option>
              {calibrations.map((item) => (
                <option key={item.run_id} value={item.run_id}>{calibrationLabel(item)}</option>
              ))}
            </select>
            <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
              <Metric label="样本事件" value={activeCalibration ? String(activeCalibration.event_count) : "--"} />
              <Metric label="主窗口" value={activeCalibration?.primary_horizon || "--"} />
              <Metric label="IC" value={activeCalibration?.primary_ic === undefined || activeCalibration?.primary_ic === null ? "--" : activeCalibration.primary_ic.toFixed(3)} />
              <Metric label="Beta" value={activeCalibration?.primary_beta === undefined || activeCalibration?.primary_beta === null ? "--" : activeCalibration.primary_beta.toFixed(3)} />
            </div>
            <p className="mt-3 text-xs text-muted-foreground">
              自动模式会优先选择样本数达标且历史 IC 绝对值更强的校准结果；输出是研究参考，不代表确定性预测。
            </p>
          </div>
        </section>

        {result && (
          <section className="grid gap-4 md:grid-cols-5">
            <Metric label="Run ID" value={result.run_id} wide />
            <Metric label="采集事件" value={String(result.event_count)} />
            <Metric label="候选数量" value={String(result.signal_count)} />
            <Metric label="耗时" value={`${result.elapsed_seconds.toFixed(1)}s`} />
            <a
              className="flex items-center justify-center gap-2 rounded-lg border bg-card p-4 text-sm font-medium hover:bg-muted"
              href={reportUrl}
              target="_blank"
              rel="noreferrer"
            >
              <Download className="h-4 w-4" aria-hidden="true" />
              下载报告
            </a>
          </section>
        )}

        <section className="rounded-lg border bg-card">
          <div className="flex items-center justify-between border-b px-4 py-3">
            <h2 className="flex items-center gap-2 text-base font-semibold">
              <Activity className="h-4 w-4 text-primary" aria-hidden="true" />
              短期启动信号排名
            </h2>
            {loading && <span className="inline-flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />正在采集新闻和行情</span>}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1100px] text-sm">
              <thead className="bg-muted/40 text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-3 text-left font-medium">排名</th>
                  <th className="px-4 py-3 text-left font-medium">标的</th>
                  <th className="px-4 py-3 text-left font-medium">信号</th>
                  <th className="px-4 py-3 text-right font-medium">启动分</th>
                  <th className="px-4 py-3 text-right font-medium">预期异常收益</th>
                  <th className="px-4 py-3 text-right font-medium">现价</th>
                  <th className="px-4 py-3 text-right font-medium">5日</th>
                  <th className="px-4 py-3 text-right font-medium">20日</th>
                  <th className="px-4 py-3 text-right font-medium">量比</th>
                  <th className="px-4 py-3 text-left font-medium">核心原因</th>
                  <th className="px-4 py-3 text-left font-medium">顶部事件</th>
                </tr>
              </thead>
              <tbody>
                {!result && !loading && (
                  <tr>
                    <td colSpan={11} className="px-4 py-12 text-center text-muted-foreground">
                      点击“运行扫描”开始采集最新资讯并生成候选排名。
                    </td>
                  </tr>
                )}
                {result?.signals.map((row, index) => (
                  <tr key={`${row.symbol}-${index}`} className="border-t">
                    <td className="px-4 py-3 text-muted-foreground">{index + 1}</td>
                    <td className="px-4 py-3 font-semibold">{row.symbol}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex rounded-md px-2 py-1 text-xs font-medium ${signalTone(row.signal)}`}>
                        {signalText(row)}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right font-medium">{num(row.launch_score, 3)}</td>
                    <td className="px-4 py-3 text-right">{pct(row.expected_abret, 2)}</td>
                    <td className="px-4 py-3 text-right">{money(row.last_price)}</td>
                    <td className="px-4 py-3 text-right">{pct(row.ret_5d)}</td>
                    <td className="px-4 py-3 text-right">{pct(row.ret_20d)}</td>
                    <td className="px-4 py-3 text-right">{num(row.volume_ratio, 2)}</td>
                    <td className="max-w-[220px] px-4 py-3 text-muted-foreground">{row.reason || "--"}</td>
                    <td className="max-w-[360px] px-4 py-3">
                      <div className="max-h-10 overflow-hidden">{row.top_event || "--"}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4 text-sm text-amber-900 dark:text-amber-100">
          <div className="mb-1 flex items-center gap-2 font-medium">
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            研究口径提示
          </div>
          <p>
            事件驱动模型衡量的是新闻冲击与后续异常收益之间的统计关系。新闻覆盖、发布时间、样本数量和市场环境都会影响结论，结果应作为研究报告和候选池筛选，不应直接等同于下单信号。
          </p>
        </section>
      </main>
    </div>
  );
}

function Metric({ label, value, wide = false }: { label: string; value: string; wide?: boolean }) {
  return (
    <div className={`rounded-lg border bg-card p-4 ${wide ? "md:col-span-2" : ""}`}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 truncate text-lg font-semibold">{value}</div>
    </div>
  );
}
