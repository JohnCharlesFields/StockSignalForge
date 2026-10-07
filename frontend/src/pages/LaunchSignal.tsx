import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity, AlertTriangle, BrainCircuit, Loader2,
  Rocket, Settings2, Target, TrendingUp, Zap,
} from "lucide-react";
import { toast } from "sonner";
import {
  api,
  type LaunchCalibrationResult,
  type LaunchModelInfo,
  type LaunchPeerGroup,
  type LaunchScanResponse,
  type ResearchUniverseOption,
} from "@/lib/api";
import { UniverseOptions } from "@/components/research/UniverseOptions";

function pct(v: number | undefined | null, d = 1): string {
  if (v === undefined || v === null || !Number.isFinite(v)) return "--";
  return `${(v * 100).toFixed(d)}%`;
}
function num(v: number | undefined | null, d = 2): string {
  if (v === undefined || v === null || !Number.isFinite(v)) return "--";
  return v.toFixed(d);
}
function money(v: number | undefined | null): string {
  if (v === undefined || v === null || v <= 0) return "--";
  return `$${v.toFixed(2)}`;
}

function signalTone(sig: string): string {
  if (sig === "strong_buy") return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border border-emerald-500/30";
  if (sig === "buy") return "bg-sky-500/15 text-sky-700 dark:text-sky-300 border border-sky-500/30";
  if (sig === "watch") return "bg-amber-500/15 text-amber-700 dark:text-amber-300 border border-amber-500/30";
  return "bg-muted text-muted-foreground";
}

function scoreBarWidth(score: number): string {
  return `${Math.min(100, Math.max(5, score * 100))}%`;
}

export function LaunchSignal() {
  const [universe, setUniverse] = useState("spx");
  const [universes, setUniverses] = useState<ResearchUniverseOption[]>([]);
  const [symbolsText, setSymbolsText] = useState("");
  const [useObservationPool, setUseObservationPool] = useState(true);
  const [top, setTop] = useState(30);
  const [threshold, setThreshold] = useState(0.5);
  const [scanMode, setScanMode] = useState<"technical" | "peer_earnings" | "combined">("combined");
  const [peerGroup, setPeerGroup] = useState("all");
  const [peerGroups, setPeerGroups] = useState<LaunchPeerGroup[]>([]);
  const [result, setResult] = useState<LaunchScanResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [progressPhase, setProgressPhase] = useState("");
  const [progressPct, setProgressPct] = useState(0);

  const [calibrating, setCalibrating] = useState(false);
  const [calibPhase, setCalibPhase] = useState("");
  const [calibPct, setCalibPct] = useState(0);
  const [calibResult, setCalibResult] = useState<LaunchCalibrationResult | null>(null);

  const [models, setModels] = useState<LaunchModelInfo[]>([]);
  const [selectedModel, setSelectedModel] = useState("");
  const [showSettings, setShowSettings] = useState(false);

  const sseRef = useRef<EventSource | null>(null);
  const isPeerRelayPool = universe === "speculative_peer_earnings";

  const loadModels = async () => {
    try {
      const res = await api.listLaunchSignalModels();
      setModels(res.models || []);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "无法加载模型列表";
      toast.error(msg);
    }
  };

  useEffect(() => {
    loadModels();
    api.listResearchUniverses()
      .then((res) => setUniverses(res.universes || []))
      .catch((err) => toast.error(err instanceof Error ? err.message : "无法加载股票池"));
    api.listLaunchSignalPeerGroups()
      .then((res) => setPeerGroups(res.groups || []))
      .catch((err) => toast.error(err instanceof Error ? err.message : "无法加载同行赛道"));
    return () => { if (sseRef.current) sseRef.current.close(); };
  }, []);

  useEffect(() => {
    setScanMode(isPeerRelayPool ? "peer_earnings" : "technical");
    if (isPeerRelayPool) {
      setPeerGroup("all");
      setUseObservationPool(false);
    }
  }, [isPeerRelayPool]);

  const runScan = async () => {
    const symbols = symbolsText
      .split(/[,;\s]+/)
      .map((x) => x.trim().toUpperCase())
      .filter(Boolean);

    setLoading(true);
    setResult(null);
    setProgressPhase("准备中");
    setProgressPct(0);

    try {
      const { job_id } = await api.scanLaunchSignalAsync({
        universe,
        symbols: symbols.length ? symbols : undefined,
        model_path: selectedModel || undefined,
        threshold: threshold !== 0.5 ? threshold : undefined,
        top,
        use_stock_observation_pool: !isPeerRelayPool && useObservationPool,
        scan_mode: scanMode,
        peer_group: peerGroup,
      });

      const es = new EventSource(api.launchSignalScanStreamUrl(job_id));
      sseRef.current = es;

      es.addEventListener("progress", (e) => {
        try {
          const data = JSON.parse(e.data);
          setProgressPhase(data.phase || "");
          setProgressPct(data.pct || 0);
        } catch { /* ignore */ }
      });

      es.addEventListener("result", (e) => {
        try {
          const data = JSON.parse(e.data) as LaunchScanResponse;
          setResult(data);
          toast.success(`扫描完成：${data.n_signals} 个启动信号`);
        } catch { /* ignore */ }
      });

      es.addEventListener("error", (e) => {
        try {
          const data = JSON.parse((e as MessageEvent).data);
          toast.error(data.error || "扫描出错");
        } catch {
          toast.error("扫描连接中断");
        }
        setLoading(false);
        es.close();
      });

      es.addEventListener("done", () => {
        setLoading(false);
        es.close();
      });

      es.onerror = () => {
        setLoading(false);
        es.close();
      };
    } catch (err) {
      const msg = err instanceof Error ? err.message : "扫描启动失败";
      toast.error(msg);
      setLoading(false);
    }
  };

  const runCalibration = async () => {
    const symbols = symbolsText
      .split(/[,;\s]+/)
      .map((x) => x.trim().toUpperCase())
      .filter(Boolean);

    setCalibrating(true);
    setCalibResult(null);
    setCalibPhase("准备校准");
    setCalibPct(0);

    try {
      const { job_id } = await api.calibrateLaunchSignalAsync({
        universe,
        symbols: symbols.length ? symbols.slice(0, 40) : undefined,
        days: 500,
        fwd_horizon: 10,
        launch_threshold: 0.08,
        contamination: 0.12,
      });

      const es = new EventSource(api.launchSignalCalibrateStreamUrl(job_id));

      es.addEventListener("progress", (e) => {
        try {
          const data = JSON.parse(e.data);
          setCalibPhase(data.phase || "");
          setCalibPct(data.pct || 0);
        } catch { /* ignore */ }
      });

      es.addEventListener("result", (e) => {
        try {
          const data = JSON.parse(e.data) as LaunchCalibrationResult;
          setCalibResult(data);
          toast.success(`校准完成：精确率 ${(data.precision * 100).toFixed(1)}%`);
          loadModels();
        } catch { /* ignore */ }
      });

      es.addEventListener("error", (e) => {
        try {
          const data = JSON.parse((e as MessageEvent).data);
          toast.error(data.error || "校准出错");
        } catch {
          toast.error("校准连接中断");
        }
        setCalibrating(false);
        es.close();
      });

      es.addEventListener("done", () => {
        setCalibrating(false);
        es.close();
      });

      es.onerror = () => {
        setCalibrating(false);
        es.close();
      };
    } catch (err) {
      const msg = err instanceof Error ? err.message : "校准启动失败";
      toast.error(msg);
      setCalibrating(false);
    }
  };

  // The legacy ML calibration panel only consumes calibResult; the scan's
  // top-level `calibration` is now the R1 shadow summary (different shape).
  const activeCalibration = calibResult;

  // R1 calibration proved the raw launch_score is INVERTED vs net-of-cost
  // excess returns, so we rank by the honest calibrated probability (which
  // corrects the inversion). Raw launch_score is shown but never the sort key.
  const sortedByScore = useMemo(() => {
    if (!result?.signals) return [];
    const prob = (s: (typeof result.signals)[number]) =>
      s.calibrated_probability ?? s.calibration?.launch?.p_calibrated ?? 0.5;
    return [...result.signals].sort((a, b) => prob(b) - prob(a) || b.launch_score - a.launch_score);
  }, [result]);

  return (
    <div className="h-full overflow-y-auto bg-background">
      <main className="mx-auto max-w-7xl px-6 py-6 space-y-6">
        <section className="flex flex-col gap-4 border-b pb-5 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="mb-2 inline-flex items-center gap-2 text-xs font-medium text-primary">
              <Rocket className="h-3.5 w-3.5" aria-hidden="true" />
              技术启动信号
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">量化启动信号扫描</h1>
            <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
              第二层：组合量价启动与同行财报接力。专项模式会寻找已先发且超预期的同行，并评估尚未披露财报公司的相对滞涨空间。
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <select
              aria-label="扫描模式"
              className="rounded-md border bg-background px-3 py-2 text-sm"
              value={scanMode}
              disabled
            >
              <option value="technical">普通扫描：量价启动</option>
              <option value="peer_earnings">专项扫描：同行财报接力</option>
            </select>
            {scanMode !== "technical" && (
              <select
                aria-label="同行赛道"
                className="rounded-md border bg-background px-3 py-2 text-sm"
                value={peerGroup}
                onChange={(e) => setPeerGroup(e.target.value)}
              >
                <option value="all">全部热门同行赛道</option>
                {peerGroups.map((item) => (
                  <option key={item.id} value={item.id}>{item.label} · {item.symbols.length} 家</option>
                ))}
              </select>
            )}
            <select
              aria-label="股票池"
              className="rounded-md border bg-background px-3 py-2 text-sm"
              value={universe}
              onChange={(e) => setUniverse(e.target.value)}
              disabled={!!symbolsText.trim()}
            >
              <UniverseOptions universes={universes} />
            </select>
            <button
              type="button"
              onClick={() => setShowSettings(!showSettings)}
              className="inline-flex items-center gap-2 whitespace-nowrap rounded-md border px-3 py-2 text-sm hover:bg-muted"
            >
              <Settings2 className="h-4 w-4" />
              {showSettings ? "隐藏设置" : "高级设置"}
            </button>
            <button
              type="button"
              onClick={runCalibration}
              disabled
              title="历史校准器尚未恢复"
              className="inline-flex items-center gap-2 whitespace-nowrap rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-60"
            >
              <BrainCircuit className="h-4 w-4" />
              校准器待恢复
            </button>
            <button
              type="button"
              onClick={runScan}
              disabled={loading}
              className="inline-flex items-center gap-2 whitespace-nowrap rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
            >
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Zap className="h-4 w-4" />}
              {loading ? "扫描中..." : "运行扫描"}
            </button>
          </div>
        </section>

        {showSettings && (
          <section className="grid gap-4 lg:grid-cols-2">
            <div className="rounded-lg border bg-card p-4">
              <h2 className="mb-4 flex items-center gap-2 text-base font-semibold">
                <Target className="h-4 w-4 text-primary" aria-hidden="true" />
                扫描参数
              </h2>
              <div className="grid gap-4 md:grid-cols-3">
                <label className="space-y-1 text-sm">
                  <span className="text-muted-foreground">Top 候选</span>
                  <input
                    className="w-full rounded-md border bg-background px-3 py-2"
                    type="number" min={1} max={200}
                    value={top}
                    onChange={(e) => setTop(Number(e.target.value) || 30)}
                  />
                </label>
                <label className="space-y-1 text-sm">
                  <span className="text-muted-foreground">信号阈值</span>
                  <input
                    className="w-full rounded-md border bg-background px-3 py-2"
                    type="number" min={0.1} max={0.95} step={0.05}
                    value={threshold}
                    onChange={(e) => setThreshold(Number(e.target.value) || 0.5)}
                  />
                </label>
                <label className="space-y-1 text-sm">
                  <span className="text-muted-foreground">指定模型</span>
                  <select
                    className="w-full rounded-md border bg-background px-3 py-2"
                    value={selectedModel}
                    onChange={(e) => setSelectedModel(e.target.value)}
                  >
                    <option value="">自动选择最新模型</option>
                    {models.map((m) => (
                      <option key={m.path} value={m.path}>
                        {m.name} | P={pct(m.precision, 1)} F1={pct(m.f1, 1)}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <label className="mt-4 block space-y-1 text-sm">
                <span className="text-muted-foreground">指定标的（留空使用所选股票池）</span>
                <textarea
                  className="min-h-16 w-full rounded-md border bg-background px-3 py-2"
                  value={symbolsText}
                  onChange={(e) => setSymbolsText(e.target.value)}
                  placeholder="例如：AAPL, NVDA, MSFT, TSLA；留空使用默认池"
                />
              </label>
              {scanMode === "technical" && (
                <label className="mt-4 flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={useObservationPool} onChange={(e) => setUseObservationPool(e.target.checked)} disabled={!!symbolsText.trim()} />
                  <span className="text-muted-foreground">优先扫描实股机会观察池；若暂无报告则使用所选完整股票池</span>
                </label>
              )}
            </div>

            <div className="rounded-lg border bg-card p-4">
              <h2 className="mb-4 flex items-center gap-2 text-base font-semibold">
                <BrainCircuit className="h-4 w-4 text-primary" aria-hidden="true" />
                模型校准
              </h2>
              {calibrating ? (
                <div className="space-y-3">
                  <div className="flex items-center gap-2 text-sm">
                    <Loader2 className="h-4 w-4 animate-spin text-primary" />
                    <span>{calibPhase}</span>
                    <span className="text-muted-foreground">{(calibPct * 100).toFixed(0)}%</span>
                  </div>
                  <div className="h-2 rounded-full bg-muted overflow-hidden">
                    <div
                      className="h-full bg-primary transition-all duration-300 rounded-full"
                      style={{ width: `${calibPct * 100}%` }}
                    />
                  </div>
                </div>
              ) : activeCalibration ? (
                <div className="space-y-3">
                  <div className="grid grid-cols-3 gap-3 text-sm">
                    <MetricCard label="精确率" value={pct(activeCalibration.precision, 1)} highlight={activeCalibration.precision >= 0.6} />
                    <MetricCard label="召回率" value={pct(activeCalibration.recall, 1)} />
                    <MetricCard label="F1" value={pct(activeCalibration.f1, 1)} />
                    <MetricCard label="阈值" value={num(activeCalibration.threshold, 3)} />
                    <MetricCard label="训练样本" value={activeCalibration.n_train_samples.toLocaleString()} />
                    <MetricCard label="正样本" value={activeCalibration.n_positive.toLocaleString()} />
                  </div>
                  {activeCalibration.validation_results && typeof activeCalibration.validation_results === "object" && "avg_precision" in activeCalibration.validation_results && (
                    <div className="rounded-md bg-muted/50 p-3 text-xs space-y-1">
                      <div className="font-medium text-muted-foreground">Walk-Forward 验证（{(activeCalibration.validation_results as Record<string, unknown>).n_folds as number} 折）</div>
                      <div>平均精确率: {pct((activeCalibration.validation_results as Record<string, unknown>).avg_precision as number, 1)} | 平均 F1: {pct((activeCalibration.validation_results as Record<string, unknown>).avg_f1 as number, 1)}</div>
                    </div>
                  )}
                  {activeCalibration.feature_importances && (
                    <div className="text-xs">
                      <div className="mb-1 font-medium text-muted-foreground">Top 特征重要性</div>
                      <div className="flex flex-wrap gap-1">
                        {Object.entries(activeCalibration.feature_importances).slice(0, 8).map(([k, v]) => (
                          <span key={k} className="inline-flex items-center gap-1 rounded-md bg-muted px-2 py-0.5">
                            <span>{k}</span>
                            <span className="text-primary font-mono">{(v as number).toFixed(3)}</span>
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">历史校准器源码尚待恢复。当前扫描使用可审计量价规则，不展示未经验证的训练指标。</p>
              )}
            </div>
          </section>
        )}

        {loading && (
          <section className="rounded-lg border bg-card p-4">
            <div className="flex items-center gap-3">
              <Loader2 className="h-5 w-5 animate-spin text-primary" />
              <div className="flex-1">
                <div className="text-sm font-medium">{progressPhase || "正在扫描..."}</div>
                <div className="mt-2 h-2 rounded-full bg-muted overflow-hidden">
                  <div
                    className="h-full bg-primary transition-all duration-300 rounded-full"
                    style={{ width: `${Math.max(5, progressPct * 100)}%` }}
                  />
                </div>
              </div>
              <span className="text-sm text-muted-foreground">{(progressPct * 100).toFixed(0)}%</span>
            </div>
          </section>
        )}

        {result && (
          <section className="grid gap-4 md:grid-cols-5">
            <MetricCard label="Run ID" value={result.run_id} wide />
            <MetricCard label="扫描标的" value={String(result.n_scanned)} />
            <MetricCard label="启动信号" value={String(result.n_signals)} highlight={result.n_signals > 0} />
            <MetricCard label="耗时" value={`${result.elapsed_seconds.toFixed(1)}s`} />
            <MetricCard label="信号阈值" value={num(result.threshold, 3)} />
          </section>
        )}

        <section className="rounded-lg border bg-card">
          <div className="flex items-center justify-between border-b px-4 py-3">
            <div>
              <h2 className="flex items-center gap-2 text-base font-semibold">
                <Activity className="h-4 w-4 text-primary" aria-hidden="true" />
                启动信号排名
              </h2>
              <p className="mt-0.5 text-xs text-muted-foreground">
                按<span className="font-medium text-foreground">校准胜率</span>排序（历史校准显示原始启动分越高、跑赢自身基线的概率反而越低，故启动分仅作研究展示、不用于排序）。
              </p>
            </div>
            {loading && (
              <span className="inline-flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />正在计算特征和信号
              </span>
            )}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1820px] text-sm">
              <thead className="bg-muted/40 text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-3 text-left font-medium">#</th>
                  <th className="px-4 py-3 text-left font-medium">标的</th>
                  <th className="px-4 py-3 text-left font-medium">信号</th>
                  <th className="px-4 py-3 text-right font-medium">校准胜率</th>
                  <th className="px-4 py-3 text-right font-medium">启动分<span className="ml-1 font-normal text-muted-foreground/70">(研究·非胜率)</span></th>
                  <th className="px-4 py-3 text-left font-medium">日隧道协商</th>
                  <th className="px-4 py-3 text-right font-medium">机会分</th>
                  <th className="px-4 py-3 text-right font-medium">现价</th>
                  <th className="px-4 py-3 text-right font-medium">5日</th>
                  <th className="px-4 py-3 text-right font-medium">10日</th>
                  <th className="px-4 py-3 text-right font-medium">20日</th>
                  <th className="px-4 py-3 text-right font-medium">量比</th>
                  <th className="px-4 py-3 text-right font-medium">RSI</th>
                  <th className="px-4 py-3 text-left font-medium">接力赛道</th>
                  <th className="px-4 py-3 text-left font-medium">先发财报</th>
                  <th className="px-4 py-3 text-left font-medium">滞涨空间 / 概率</th>
                  <th className="px-4 py-3 text-left font-medium">信号原因</th>
                </tr>
              </thead>
              <tbody>
                {!result && !loading && (
                  <tr>
                    <td colSpan={17} className="px-4 py-12 text-center text-muted-foreground">
                      点击"运行扫描"开始量化技术分析，识别启动信号。
                    </td>
                  </tr>
                )}
                {sortedByScore.map((row, index) => (
                  <tr key={`${row.symbol}-${index}`} className="border-t hover:bg-muted/30 transition-colors">
                    <td className="px-4 py-3 text-muted-foreground">{index + 1}</td>
                    <td className="px-4 py-3 font-semibold">
                      <a
                        className="text-primary hover:underline"
                        href={`/single-stock-overnight?symbol=${encodeURIComponent(row.symbol)}`}
                      >
                        {row.symbol}
                      </a>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex rounded-md px-2 py-1 text-xs font-medium ${signalTone(row.signal)}`}>
                        {row.signal_cn}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right">
                      {(() => {
                        const p = row.calibrated_probability ?? row.calibration?.launch?.p_calibrated;
                        const validated = row.calibration?.launch?.source === "calibrated";
                        return (
                          <div className="flex items-center justify-end gap-1.5">
                            <span className="font-mono font-semibold">{p == null ? "--" : pct(p, 0)}</span>
                            {!validated && (
                              <span className="rounded bg-amber-500/15 px-1 py-0.5 text-[9px] text-amber-700 dark:text-amber-300" title="无 active 校准曲线，回退经验 sigmoid">
                                未验证
                              </span>
                            )}
                          </div>
                        );
                      })()}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <div className="w-16 h-1.5 rounded-full bg-muted overflow-hidden">
                          <div
                            className="h-full rounded-full bg-muted-foreground/50"
                            style={{ width: scoreBarWidth(row.launch_score) }}
                          />
                        </div>
                        <span className="font-mono w-12 text-right text-muted-foreground">{num(row.launch_score, 3)}</span>
                      </div>
                    </td>
                    <td className="max-w-[220px] px-4 py-3 text-xs">
                      {row.daily_tunnel ? (
                        <div className="space-y-1">
                          <div className="font-semibold text-foreground">
                            {num(row.daily_tunnel.score, 1)} 分 · {row.daily_tunnel.label}
                          </div>
                          <div className="text-muted-foreground">{row.daily_tunnel.current_zone}</div>
                          <div className="text-muted-foreground">
                            拐点 {pct(row.daily_tunnel.turning_point_score)} · 当前上行 {row.daily_tunnel.current_up_cycle_days ?? 0} 日 · 最长上行 {row.daily_tunnel.longest_up_cycle_days ?? 0} 日
                          </div>
                          <div className="text-muted-foreground">
                            守 MA20 {pct(row.daily_tunnel.above_ma20_rate)} · 上行力量 {pct(row.daily_tunnel.up_cycle_power)}
                          </div>
                        </div>
                      ) : "--"}
                    </td>
                    <td className="px-4 py-3 text-right font-mono">{row.opportunity_score == null ? "--" : num(row.opportunity_score, 1)}</td>
                    <td className="px-4 py-3 text-right font-mono">{money(row.last_price)}</td>
                    <td className={`px-4 py-3 text-right font-mono ${row.ret_5d >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>
                      {pct(row.ret_5d)}
                    </td>
                    <td className={`px-4 py-3 text-right font-mono ${row.ret_10d >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>
                      {pct(row.ret_10d)}
                    </td>
                    <td className={`px-4 py-3 text-right font-mono ${row.ret_20d >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>
                      {pct(row.ret_20d)}
                    </td>
                    <td className="px-4 py-3 text-right font-mono">{num(row.vol_ratio, 2)}</td>
                    <td className="px-4 py-3 text-right font-mono">
                      <span className={row.rsi_14 > 70 ? "text-rose-500" : row.rsi_14 < 30 ? "text-emerald-500" : ""}>
                        {num(row.rsi_14, 1)}
                      </span>
                    </td>
                    <td className="max-w-[180px] px-4 py-3 text-xs">
                      {row.peer_earnings ? (
                        <div>
                          <div>{row.peer_earnings.group_label}</div>
                          <div className="text-muted-foreground">
                            {row.peer_earnings.sublane_relation_display || row.peer_earnings.sublane_relation || "--"}
                          </div>
                        </div>
                      ) : "--"}
                    </td>
                    <td className="max-w-[190px] px-4 py-3 text-xs">
                      {row.peer_earnings
                        ? `${row.peer_earnings.leader_symbol} · ${row.peer_earnings.leader_report_date}`
                        : "--"}
                    </td>
                    <td className="max-w-[190px] px-4 py-3 text-xs">
                      {row.peer_earnings
                        ? `${pct(row.peer_earnings.relative_lag_gap)} / ${pct(row.peer_earnings.research_probability)}${row.peer_earnings.target_report_date_requires_manual_check ? " · 披露日待人工确认" : ""}`
                        : "--"}
                    </td>
                    <td className="max-w-[280px] px-4 py-3 text-muted-foreground text-xs">{row.reason || "--"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {result?.signals && result.signals.length > 0 && (
          <section className="grid gap-4 lg:grid-cols-2">
            <div className="rounded-lg border bg-card p-4">
              <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                <TrendingUp className="h-4 w-4 text-primary" />
                信号分布
              </h3>
              <div className="space-y-2">
                {["strong_buy", "buy", "watch"].map((sig) => {
                  const count = result.signals.filter((s) => s.signal === sig).length;
                  const label = sig === "strong_buy" ? "强烈启动" : sig === "buy" ? "启动信号" : "观察启动";
                  const color = sig === "strong_buy" ? "bg-emerald-500" : sig === "buy" ? "bg-sky-500" : "bg-amber-500";
                  return (
                    <div key={sig} className="flex items-center gap-3 text-sm">
                      <div className={`w-2 h-2 rounded-full ${color}`} />
                      <span className="w-20 text-muted-foreground">{label}</span>
                      <div className="flex-1 h-4 rounded-full bg-muted overflow-hidden">
                        <div
                          className={`h-full rounded-full ${color} opacity-60`}
                          style={{ width: `${(count / result.n_scanned) * 100}%` }}
                        />
                      </div>
                      <span className="font-mono w-8 text-right">{count}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          </section>
        )}

        <section className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4 text-sm text-amber-900 dark:text-amber-100">
          <div className="mb-1 flex items-center gap-2 font-medium">
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            研究口径提示
          </div>
          <p>
            当前启动信号是可解释的研究候选筛选，不是历史胜率承诺。同行财报接力使用 EPS surprise、营收同比和量价异动；yfinance 不提供真实净买入，因此资金关注仅使用成交额放大代理。若下一次披露日为空，候选仍会保留并提示人工确认。请结合公告原文、市场环境和流动性复核后使用，不构成投资建议。
          </p>
        </section>
      </main>
    </div>
  );
}

function MetricCard({ label, value, wide = false, highlight = false }: { label: string; value: string; wide?: boolean; highlight?: boolean }) {
  return (
    <div className={`rounded-lg border bg-card p-4 ${wide ? "md:col-span-2" : ""}`}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`mt-1 truncate text-lg font-semibold ${highlight ? "text-primary" : ""}`}>{value}</div>
    </div>
  );
}
