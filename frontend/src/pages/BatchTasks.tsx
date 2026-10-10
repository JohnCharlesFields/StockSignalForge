import { useCallback, useEffect, useRef, useState } from "react";
import { CalendarClock, CheckCircle2, Copy, Loader2, Play, RefreshCw, Smartphone } from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import {
  api,
  type CalibRebuildStatus,
  type DailyAutoStatus,
  type FeishuMobileUrl,
  type PriorityBoardResponse,
  type SignalCalibrationStatus,
} from "@/lib/api";

function fmtTime(iso?: string | null): string {
  if (!iso) return "--";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}

function statusTone(status?: string): string {
  if (status === "running" || status === "queued") return "bg-sky-500/15 text-sky-700 dark:text-sky-300 border-sky-500/30";
  if (status === "completed") return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30";
  if (status === "disabled") return "bg-slate-500/15 text-slate-600 dark:text-slate-300 border-slate-500/30";
  return "bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30";
}

function evidenceStatus(status?: string): string {
  return ({ running: "更新中", pending: "待补充", completed: "已更新", partial: "部分更新", busy: "已在刷新",
    timeout: "限时结束", unavailable: "暂不可用", skipped: "已跳过" } as Record<string, string>)[status ?? ""] ?? "尚未更新";
}

function batchStatus(status?: string): string {
  return ({ running: "运行中", queued: "排队中", completed: "已完成", partial: "部分完成",
    waiting_data: "等待行情发布", skipped: "暂无当日行情", failed: "执行故障", disabled: "已停用",
    idle: "尚未运行" } as Record<string, string>)[status ?? ""] ?? status ?? "--";
}

function priceSyncReason(reason: string): string {
  return ({
    history_anchor_missing_or_future: "缺少可核验历史锚点",
    history_gap_exceeds_10_sessions: "历史缺口超过同步范围",
    invalid_history_index: "历史日期索引异常",
    non_session_anchor: "历史锚点不是交易日",
    non_consecutive_anchors: "历史锚点不连续",
    requested_session_or_overlap_missing: "目标日或重叠日行情未返回",
    overlap_price_basis_mismatch: "价格口径不一致",
    overlap_volume_unit_mismatch: "成交量口径不一致",
    previous_close_basis_mismatch: "昨收口径不一致",
    large_gap_requires_corporate_action_review: "价格大幅跳变待核验",
    Timeout: "数据源请求超时",
    ReadTimeout: "数据源读取超时",
    HTTPError: "数据源请求被拒绝",
  } as Record<string, string>)[reason] ?? (reason.startsWith("cache_planning_") ? "单股缓存读取异常" : "数据暂不可用或未通过校验");
}

export function BatchTasks() {
  const [auto, setAuto] = useState<DailyAutoStatus | null>(null);
  const [calib, setCalib] = useState<SignalCalibrationStatus | null>(null);
  const [board, setBoard] = useState<PriorityBoardResponse | null>(null);
  const [running, setRunning] = useState(false);
  const [collecting, setCollecting] = useState(false);
  const [rebuild, setRebuild] = useState<CalibRebuildStatus | null>(null);
  const [feishu, setFeishu] = useState<FeishuMobileUrl | null>(null);
  const [dataStatus, setDataStatus] = useState<Awaited<ReturnType<typeof api.getMarketDataStatus>> | null>(null);
  const pollRef = useRef<number | null>(null);
  const rebuildPollRef = useRef<number | null>(null);

  const loadAll = useCallback(() => {
    api.getDailyAutoStatus().then(setAuto).catch(() => {});
    api.getSignalCalibrationStatus().then(setCalib).catch(() => {});
    api.getPriorityBoard(1).then(setBoard).catch(() => {});
    api.getCalibRebuildStatus().then(setRebuild).catch(() => {});
    api.getFeishuMobileUrl().then(setFeishu).catch(() => {});
    api.getMarketDataStatus().then(setDataStatus).catch(() => {});
  }, []);

  const copyText = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success("已复制到剪贴板");
    } catch {
      toast.error("复制失败，请手动选择复制");
    }
  };

  const [feishuRefreshing, setFeishuRefreshing] = useState(false);
  const refreshFeishu = async () => {
    setFeishuRefreshing(true);
    try {
      const r = await api.getFeishuMobileUrl();
      setFeishu(r);
      toast.success(r.available ? "已刷新隧道地址" : "隧道未运行，未获取到地址");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "刷新失败");
    } finally {
      setFeishuRefreshing(false);
    }
  };

  const rebuildCalib = async () => {
    try {
      const res = await api.rebuildCalibration();
      setRebuild(res);
      toast.success("已开始重建校准（约 1 分钟）");
      if (rebuildPollRef.current) window.clearInterval(rebuildPollRef.current);
      rebuildPollRef.current = window.setInterval(() => {
        api.getCalibRebuildStatus().then((s) => {
          setRebuild(s);
          if (s.status !== "running") {
            if (rebuildPollRef.current) window.clearInterval(rebuildPollRef.current);
            rebuildPollRef.current = null;
            api.getSignalCalibrationStatus().then(setCalib).catch(() => {});
          }
        });
      }, 5000);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "重建失败");
    }
  };

  useEffect(() => {
    loadAll();
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [loadAll]);

  // Poll the daily scan while it is running.
  useEffect(() => {
    const live = auto?.status === "running" || auto?.status === "queued";
    if (live && !pollRef.current) {
      pollRef.current = window.setInterval(() => api.getDailyAutoStatus().then(setAuto).catch(() => {}), 4000);
    } else if (!live && pollRef.current) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
      api.getPriorityBoard(1).then(setBoard).catch(() => {});
    }
  }, [auto?.status]);

  const runNow = async (force: boolean) => {
    setRunning(true);
    try {
      const res = await api.runDailyAuto(force);
      setAuto(res);
      if (res.blocked_by_active_worker) {
        toast.warning(res.message);
        return;
      }
      toast.success(res.status === "queued" || res.status === "running"
        ? (force ? "已触发重跑" : "已开始运行，复用同日已完成池") : "今日已完成，无需重复运行");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "触发失败");
    } finally {
      setRunning(false);
    }
  };

  const finalizeNow = async () => {
    setRunning(true);
    try {
      const res = await api.finalizeDailyAuto();
      setAuto(res);
      if (res.blocked_by_active_worker) {
        toast.warning(res.message);
        return;
      }
      toast.success("已开始收尾恢复，不重新运行股票池扫描");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "恢复失败");
    } finally {
      setRunning(false);
    }
  };

  const collectNow = async () => {
    setCollecting(true);
    try {
      await api.collectOos();
      toast.success("已触发一次样本外采集");
      loadAll();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "采集失败");
    } finally {
      setCollecting(false);
    }
  };

  const today = auto?.today;
  const lastDate = auto?.last_record?.date;
  const doneToday = lastDate === today && auto?.last_record?.status === "completed";
  const snapIsToday = Boolean(board?.data_as_of && board.data_as_of === auto?.market?.most_recent_session);
  const busy = running || auto?.status === "running" || auto?.status === "queued";
  const poolResults = Object.values(auto?.last_record?.results ?? {});

  return (
    <div className="h-full overflow-y-auto bg-background">
      <main className="mx-auto max-w-4xl px-6 py-6 space-y-5">
        <section className="flex items-end justify-between border-b pb-4">
          <div>
            <div className="mb-1 inline-flex items-center gap-2 text-xs font-medium text-primary">
              <CalendarClock className="h-3.5 w-3.5" aria-hidden="true" />
              任务中心
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">定时批处理</h1>
            <p className="mt-1 text-sm text-muted-foreground">查看每日扫描是否跑出来；没跑可手动触发。</p>
          </div>
          <button onClick={loadAll} className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-muted">
            <RefreshCw className="h-4 w-4" /> 刷新
          </button>
        </section>

        {dataStatus && <section className="border-b pb-4 space-y-2">
          <h2 className="text-base font-semibold">行情数据口径与配置状态</h2>
          <div className="text-sm">日线优先级：{dataStatus.ohlcv_priority.join(" → ")}</div>
          <div className="flex flex-wrap gap-2 text-xs">
            {Object.entries(dataStatus.provider_configuration).map(([name, status]) => <span key={name}
              className={cn("border rounded px-2 py-1", status === "configured_unprobed" ? "border-amber-500/30 text-amber-700 dark:text-amber-300" : "text-muted-foreground") }>
              {name} · {status === "configured_unprobed" ? "已配置，授权待核验" : "未配置"}
            </span>)}
          </div>
          <p className="text-xs text-muted-foreground">{dataStatus.price_basis_note}</p>
        </section>}

        {/* Feishu mobile public URL (Cloudflare Quick Tunnel) */}
        <section className="rounded-lg border bg-card p-4">
          <div className="flex flex-wrap items-center gap-2">
            <Smartphone className="h-4 w-4 text-primary" />
            <h2 className="text-base font-semibold">飞书移动端公网地址</h2>
            {feishu?.available ? (
              <span className="inline-flex items-center gap-1 rounded-md border border-emerald-500/30 bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:text-emerald-300">
                <CheckCircle2 className="h-3 w-3" /> 隧道运行中
              </span>
            ) : (
              <span className="inline-flex items-center rounded-md border border-amber-500/30 bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-700 dark:text-amber-300">
                隧道未运行
              </span>
            )}
            <button
              type="button"
              onClick={refreshFeishu}
              disabled={feishuRefreshing}
              className="ml-auto inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-60"
            >
              {feishuRefreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              刷新地址
            </button>
          </div>

          {feishu?.available && feishu.mobile_url ? (
            <div className="mt-3 space-y-3">
              <div>
                <div className="text-xs text-muted-foreground">移动端主页地址（填到飞书后台，带 /m）</div>
                <div className="mt-1 flex items-center gap-2">
                  <code className="min-w-0 flex-1 truncate rounded-md border bg-muted/40 px-3 py-2 font-mono text-sm">{feishu.mobile_url}</code>
                  <button
                    type="button"
                    onClick={() => copyText(feishu.mobile_url!)}
                    className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
                  >
                    <Copy className="h-4 w-4" /> 复制
                  </button>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>
                  根地址：
                  <button type="button" onClick={() => copyText(feishu.url!)} className="ml-1 font-mono text-foreground underline-offset-2 hover:underline" title="点击复制">
                    {feishu.url}
                  </button>
                </span>
                <span>更新时间：{fmtTime(feishu.updated_at)}</span>
              </div>
              <p className="text-xs leading-relaxed text-muted-foreground">
                填写位置：飞书开放平台 → 自建应用 → 网页应用 → 移动端主页。
                <span className="text-amber-600 dark:text-amber-400"> 临时地址，重启隧道后会变化，变更后需重新填写。</span>
              </p>
            </div>
          ) : (
            <p className="mt-3 text-sm text-muted-foreground">
              当前没有运行中的隧道。在仓库根目录执行 <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">.\scripts\start-feishu-mobile.ps1</code> 启动后，刷新本页即可看到地址。
            </p>
          )}
        </section>

        {/* Daily scan (produces the priority-board candidate snapshot) */}
        <section className="rounded-lg border bg-card p-4">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-base font-semibold">每日三层扫描</h2>
            <span className={cn("inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium", statusTone(auto?.status))}>
              {auto?.status === "running" || auto?.status === "queued" ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
              {batchStatus(auto?.status)}
            </span>
            {doneToday && (
              <span className="inline-flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
                <CheckCircle2 className="h-3.5 w-3.5" /> 今日已完成
              </span>
            )}
            <div className="ml-auto flex gap-2">
              <button
                onClick={() => runNow(false)}
                disabled={busy}
                className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
              >
                {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                {["failed", "partial", "waiting_data"].includes(auto?.last_record?.status ?? "") ? "接续未完成池" : "立即运行"}
              </button>
              <button
                onClick={() => runNow(true)}
                disabled={busy}
                className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-60"
                title="即使今天已完成也强制重跑"
              >
                强制重跑
              </button>
              <button onClick={finalizeNow} disabled={busy}
                className="inline-flex items-center gap-2 whitespace-nowrap rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-60">
                <RefreshCw className="h-4 w-4" /> 仅恢复榜单
              </button>
            </div>
          </div>

          {(auto?.status === "running" || auto?.status === "queued") && (
            <div className="mt-3">
              <div className="mb-1 text-xs text-muted-foreground">{auto?.message || auto?.current_universe}</div>
              <div className="h-2 overflow-hidden rounded-full bg-muted">
                <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${Math.max(3, (auto?.progress ?? 0) * 100)}%` }} />
              </div>
            </div>
          )}

          <dl className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
            <Field label="计划时间" value={`${auto?.schedule_time ?? "--"} (${auto?.timezone ?? ""})`} />
            <Field label="启用" value={auto?.enabled ? "是" : "否"} />
            <Field label="今天" value={today ?? "--"} />
            <Field label="上次运行日期" value={lastDate ?? "尚无记录"} />
            <Field label="上次状态" value={batchStatus(auto?.last_record?.status)} />
            <Field label="上次完成时间" value={fmtTime(auto?.last_record?.finished_at)} />
          </dl>
          <div className="mt-3 border-l-2 border-sky-500 pl-3 text-xs">
            <p className="font-medium">每日新闻 · {evidenceStatus(auto?.daily_news?.status)}
              {auto?.daily_news?.written != null ? ` · 已保存 ${auto.daily_news.written} 条` : ""}</p>
            <p className="mt-1 text-muted-foreground">{auto?.daily_news?.error || "与跑批独立更新，资讯不可用时保留已有新闻，不阻塞榜单。"}</p>
            {auto?.daily_news?.finished_at && <p className="mt-1">更新时间 {fmtTime(auto.daily_news.finished_at)}</p>}
            {auto?.daily_evidence && <p className="mt-1">后台补充 · 事件 {evidenceStatus(auto.daily_evidence.event_status)} · AI复核 {evidenceStatus(auto.daily_evidence.llm_status)}</p>}
          </div>
          {(auto?.last_record?.data_warnings ?? []).map((warning) => <p key={warning} className="mt-2 text-xs text-amber-700 dark:text-amber-300">{warning}</p>)}
          {auto?.last_record?.cached_price_coverage && (
            <p className="mt-2 text-xs text-muted-foreground">当日行情覆盖 {auto.last_record.cached_price_coverage.current ?? 0}/{auto.last_record.cached_price_coverage.total ?? 0} 只；无当日行情的池跳过，未更新部分保留历史结果。</p>
          )}
          {auto?.last_record?.gildata_price_sync && (
            <div className="mt-2 text-xs text-muted-foreground">
              <p>聚源日线同步：{evidenceStatus(auto.last_record.gildata_price_sync.status)} · 写入 {auto.last_record.gildata_price_sync.symbols_written ?? 0} 只 / {auto.last_record.gildata_price_sync.records_added ?? 0} 条 · 暂未补齐 {auto.last_record.gildata_price_sync.rejected_count ?? 0} 只 · 待处理 {auto.last_record.gildata_price_sync.pending_count ?? 0} 只 · 冷却中 {auto.last_record.gildata_price_sync.cooldown_count ?? 0} 只。既有历史价格不覆盖。</p>
              {auto.last_record.gildata_price_sync.error && <p className="mt-1">同步暂未完成（{auto.last_record.gildata_price_sync.error}），已补齐的标的继续分析。</p>}
              {Object.keys(auto.last_record.gildata_price_sync.rejection_reasons ?? {}).length > 0 && <p className="mt-1">未补齐原因：{Object.entries(auto.last_record.gildata_price_sync.rejection_reasons ?? {}).map(([reason, count]) => `${priceSyncReason(reason)} ${count} 只`).join("；")}</p>}
            </div>
          )}
          {auto?.last_record?.price_repair?.status === "waiting_data" && auto.last_record.price_repair.available_end && (
            <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">Databento 日线发布截至 {fmtTime(auto.last_record.price_repair.available_end)}，目标完整交易日尚未发布，不发起收费下载。</p>
          )}
          {auto?.last_record?.error && !busy && (
            <div role="alert" className="mt-3 border-l-2 border-amber-500 pl-3 text-sm break-words">
              <p className="font-medium text-amber-700 dark:text-amber-300">
                {auto.last_record.status === "waiting_data" ? "等待完整交易日行情，上一份榜单保留" : auto.last_record.phase === "price_preflight" ? "行情检查未通过，未启动耗时扫描" : "本次未完整完成，上一份榜单保留"}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">{auto.last_record.error}</p>
              <p className="mt-1 text-xs">完成 {auto.last_record.completed_universe_count ?? 0} 池 · 故障 {auto.last_record.failed_universe_count ?? 0} 池 · 暂缺行情 {auto.last_record.skipped_universe_count ?? 0} 池。
                行情恢复后可接续未完成池；已有完整池结果时可仅恢复榜单。</p>
            </div>
          )}
          {auto?.last_record?.status === "partial" && !auto.last_record.error && !busy && (
            <div role="status" className="mt-3 border-l-2 border-amber-500 pl-3 text-sm break-words">
              <p className="font-medium text-amber-700 dark:text-amber-300">部分完成，已更新有效候选榜单</p>
              <p className="mt-1 text-xs">已发布 {auto.last_record.snapshot_row_count ?? 0} 只有效候选；
                排除 {auto.last_record.excluded_stale_count ?? 0} 只旧行情报告候选。
                完成 {auto.last_record.completed_universe_count ?? 0} 池，未完成 {auto.last_record.failed_universe_count ?? 0} 池。</p>
              <p className="mt-1 text-xs text-muted-foreground">缺失部分未计入完整结果；可接续未完成池，不必强制重跑全部池。</p>
            </div>
          )}
          {poolResults.length > 0 && (
            <details className="mt-3 text-xs" open>
              <summary className="cursor-pointer font-medium">各池结果 · 本次复用 {auto?.last_record?.resumed_universe_count ?? 0} 池</summary>
              <div className="mt-2 space-y-2">
                {poolResults.map((pool) => <div key={pool.universe} className="grid grid-cols-[5rem_5rem_1fr] gap-2 border-b pb-1">
                  <span>{pool.universe}</span><span>{batchStatus(pool.status)}</span>
                  <span className="break-words text-muted-foreground">{pool.message || "--"}</span>
                </div>)}
              </div>
            </details>
          )}
          {auto?.last_record?.finalize_timed_out && (
            <p className="mt-2 rounded border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-300">
              上次收尾超时，未确认为完成。上一份成功榜单保留，请查看失败原因后恢复。
            </p>
          )}
        </section>

        {/* Market sync gate (trading-calendar) */}
        {auto?.market && (
          <section className="rounded-lg border bg-card p-4">
            <h2 className="text-base font-semibold">行情同步闸门（交易日历）</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              休市日（周末/假日）不同步行情、只同步资讯。
            </p>
            <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
              <Field label="最近交易日" value={auto.market.most_recent_session ?? "--"} />
              <Field label="今天开市" value={auto.market.today_is_trading_day ? "是" : "否"} tone={auto.market.today_is_trading_day ? "ok" : "warn"} />
              <Field label="已同步到" value={auto.market.last_synced_session ?? "尚未"} />
              <Field label="待同步行情" value={auto.market.price_sync_pending ? "是" : "否"} tone={auto.market.price_sync_pending ? "warn" : "ok"} />
            </dl>
          </section>
        )}

        {/* Phase timings (where the daily batch spends its time) */}
        <PhaseTimings timings={auto?.last_record?.phase_timings} />

        {/* Snapshot the priority board currently reads */}
        <section className="rounded-lg border bg-card p-4">
          <h2 className="text-base font-semibold">当前榜单快照</h2>
          <p className="mt-1 text-xs text-muted-foreground">回调买入榜的候选池来自这份快照；扫描跑完后会换成当天的。</p>
          <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
            <Field label="快照 ID" value={board?.snapshot_id ?? "--"} />
            <Field label="行情交易日" value={board?.data_as_of ?? "--"} />
            <Field label="行情是否最新" value={snapIsToday ? "是" : "否（保留历史榜单）"} tone={snapIsToday ? "ok" : "warn"} />
            <Field label="榜单生成时间" value={fmtTime(board?.generated_at)} />
          </dl>
        </section>

        {/* OOS collection + calibration */}
        <section className="grid gap-4 sm:grid-cols-2">
          <div className="rounded-lg border bg-card p-4">
            <div className="flex items-center justify-between">
              <h2 className="text-base font-semibold">样本外采集 (OOS)</h2>
              <button
                onClick={collectNow}
                disabled={collecting}
                className="inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-60"
              >
                {collecting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                采集一次
              </button>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">每 6 小时自动记录+回填；下面是已累积的事件数。</p>
            <dl className="mt-3 grid grid-cols-3 gap-2 text-sm">
              <Field label="累计" value={String(calib?.events?.total ?? "--")} />
              <Field label="已回填" value={String(calib?.events?.resolved ?? "--")} />
              <Field label="待回填" value={String(calib?.events?.pending ?? "--")} />
            </dl>
          </div>

          <div className="rounded-lg border bg-card p-4">
            <div className="flex items-center justify-between">
              <h2 className="text-base font-semibold">校准曲线</h2>
              <button
                onClick={rebuildCalib}
                disabled={rebuild?.status === "running"}
                className="inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-60"
                title="重新用 spx 3 年数据建一版校准（约 1 分钟）"
              >
                {rebuild?.status === "running" ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                重建校准
              </button>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {rebuild?.status === "running"
                ? (rebuild.message || "重建中（spx 3y，约 1 分钟）...")
                : rebuild?.status === "completed"
                  ? `上次重建完成：${fmtTime(rebuild.finished_at)}`
                  : rebuild?.status === "error"
                    ? `重建失败：${rebuild.message}`
                    : "不自动重建；点上面按钮即可手动重建一版（spx 3y）。"}
            </p>
            <div className="mt-3 space-y-1.5 text-sm">
              {(calib?.active_curves ?? []).slice(0, 4).map((c) => (
                <div key={`${c.signal_type}-${c.horizon_days}`} className="flex items-center justify-between text-xs">
                  <span className="font-mono">{c.signal_type}/{c.horizon_days}d</span>
                  <span className="text-muted-foreground">{fmtTime(c.generated_at)}</span>
                  <span className={cn("rounded px-1.5 py-0.5", c.global?.edge?.validated ? "bg-emerald-500/15 text-emerald-600" : "bg-amber-500/15 text-amber-600")}>
                    {c.global?.edge?.validated ? "已验证" : "未验证"}
                  </span>
                </div>
              ))}
              {(!calib?.active_curves || calib.active_curves.length === 0) && (
                <p className="text-xs text-muted-foreground">暂无 active 曲线。</p>
              )}
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}

const PHASE_LABELS: Record<string, string> = {
  universe_loop_total: "三层扫描（全部赛道池）",
  grouped_daily_ingest: "行情同步（Massive grouped-daily）",
  snapshot_build: "构建首页快照（含隔夜 alpha）",
  snapshot_save: "保存快照",
  board_prewarm: "预热回调买入榜",
  track_leader_prewarm: "预热赛道龙头对比",
  predictions_log: "落账今日预测",
  predictions_resolve: "结算到期预测",
};

function PhaseTimings({ timings }: { timings?: Record<string, number | Record<string, number> | undefined> }) {
  if (!timings || Object.keys(timings).length === 0) return null;
  const total = typeof timings.total === "number" ? timings.total : 0;
  const perUniverse = (timings.per_universe as Record<string, number> | undefined) || {};
  // Top-level phases worth ranking (exclude container/rollup keys).
  const skip = new Set(["total", "per_universe", "finalize_total"]);
  const entries = Object.entries(timings)
    .filter(([k, v]) => typeof v === "number" && !skip.has(k))
    .map(([k, v]) => [k, v as number] as [string, number])
    .sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map((e) => e[1]));
  return (
    <section className="rounded-lg border bg-card p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">跑批耗时分解</h2>
        <span className="text-xs text-muted-foreground">总耗时 {total ? `${total}s（${(total / 60).toFixed(1)}分）` : "--"}</span>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">按阶段耗时降序；最长的就是优化重点。每阶段也会写进容器日志（搜 <code>[daily-timing]</code>）。</p>
      <div className="mt-3 space-y-1.5">
        {entries.map(([k, v]) => (
          <div key={k} className="flex items-center gap-2 text-xs">
            <div className="w-44 shrink-0 truncate text-muted-foreground" title={PHASE_LABELS[k] || k}>{PHASE_LABELS[k] || k}</div>
            <div className="h-3 flex-1 overflow-hidden rounded bg-muted">
              <div className={cn("h-full rounded", v === max ? "bg-amber-500" : "bg-primary/70")} style={{ width: `${Math.max(2, (v / max) * 100)}%` }} />
            </div>
            <div className="w-16 shrink-0 text-right font-mono">{v}s</div>
          </div>
        ))}
      </div>
      {Object.keys(perUniverse).length > 0 && (
        <details className="mt-3 text-xs">
          <summary className="cursor-pointer text-muted-foreground">按赛道池细分（三层扫描）</summary>
          <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
            {Object.entries(perUniverse).sort((a, b) => b[1] - a[1]).map(([u, s]) => (
              <div key={u} className="flex justify-between font-mono"><span className="text-muted-foreground">{u}</span><span>{s}s</span></div>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}

function Field({ label, value, tone }: { label: string; value: string; tone?: "ok" | "warn" }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={cn("mt-0.5 font-medium", tone === "ok" && "text-emerald-600 dark:text-emerald-400", tone === "warn" && "text-amber-600 dark:text-amber-400")}>
        {value}
      </dd>
    </div>
  );
}
