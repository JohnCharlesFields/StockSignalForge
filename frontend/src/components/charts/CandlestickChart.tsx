import { useEffect, useRef, useState, useMemo, useCallback } from "react";
import { cn } from "@/lib/utils";
import { ChevronDown } from "lucide-react";
import type { PriceBar, TradeMarker, IndicatorPoint } from "@/lib/api";
import { calcMA, calcBOLL, calcMACD, calcRSI, calcKDJ, calcEMA } from "@/lib/indicators";
import { getChartTheme } from "@/lib/chart-theme";
import { abbreviateNum } from "@/lib/formatters";
import { echarts, CHART_GROUP, connectCharts } from "@/lib/echarts";
import { useDarkMode } from "@/hooks/useDarkMode";

type Sub = "vol" | "macd" | "rsi" | "kdj" | "hv";
type Range = "1M" | "3M" | "6M" | "1Y" | "ALL";
type Overlay = "ma5" | "ma10" | "ma20" | "ma60" | "ema5" | "ema10" | "ema15" | "ema20" | "ema12" | "ema26" | "boll";

const OVERLAY_OPTIONS: { id: Overlay; label: string; group: string }[] = [
  { id: "ma5", label: "MA5", group: "MA" },
  { id: "ma10", label: "MA10", group: "MA" },
  { id: "ma20", label: "MA20", group: "MA" },
  { id: "ma60", label: "MA60", group: "MA" },
  { id: "ema5", label: "EMA5", group: "EMA" },
  { id: "ema10", label: "EMA10", group: "EMA" },
  { id: "ema15", label: "EMA15", group: "EMA" },
  { id: "ema20", label: "EMA20", group: "EMA" },
  { id: "ema12", label: "EMA12", group: "EMA" },
  { id: "ema26", label: "EMA26", group: "EMA" },
  { id: "boll", label: "BOLL", group: "Channel" },
];

// Annualized realized volatility (HV) series from closes, in percent.
function calcHVSeries(closes: number[], window = 20): (number | null)[] {
  const logret: number[] = [];
  for (let i = 1; i < closes.length; i++) logret.push(Math.log(closes[i] / closes[i - 1]));
  const out: (number | null)[] = closes.map(() => null);
  for (let i = window; i < closes.length; i++) {
    const slice = logret.slice(i - window, i);
    const m = slice.reduce((a, b) => a + b, 0) / slice.length;
    const v = slice.reduce((a, b) => a + (b - m) ** 2, 0) / slice.length;
    out[i] = Math.sqrt(v * 252) * 100;
  }
  return out;
}

const RANGE_BARS: Record<Range, number> = { "1M": 22, "3M": 63, "6M": 126, "1Y": 252, ALL: Infinity };
// 多空博弈区间 = volume-profile congestion of the CURRENTLY VISIBLE window. It
// follows the range buttons and the mouse-wheel zoom, so zooming in tightens the
// 70% value area to the local congestion and zooming out widens it. (Computed
// from the visible bars in the chart-options effect + the dataZoom handler.)

// 多空博弈区间 = volume-at-price congestion. Build a volume profile over the
// visible window: bin price, spread each bar's volume across its [low,high],
// split into up-day (多头) vs down-day (空头) volume. The value area (~70% of
// volume around the POC) is the battle zone; per-bucket colour = who dominated
// at that price, opacity = how much traded there (深浅 by 量价). Daily-direction
// is a proxy for buy/sell pressure (approximate; intraday would be finer).
interface ZoneBand { lo: number; hi: number; bull: boolean; opacity: number }
function computeBattleZone(bars: PriceBar[], K = 24, vaPct = 0.7): { bands: ZoneBand[]; poc: number } | null {
  if (!bars || bars.length < 8) return null;
  let lo = Infinity, hi = -Infinity;
  for (const b of bars) { if (b.low < lo) lo = b.low; if (b.high > hi) hi = b.high; }
  if (!(hi > lo)) return null;
  const size = (hi - lo) / K;
  const up = new Array(K).fill(0), dn = new Array(K).fill(0);
  for (const b of bars) {
    const v = b.volume || 0; if (v <= 0) continue;
    const bl = b.low, bh = b.high > b.low ? b.high : b.low + size * 0.001;
    const span = bh - bl;
    const isUp = b.close >= b.open;
    let k0 = Math.max(0, Math.min(K - 1, Math.floor((bl - lo) / size)));
    let k1 = Math.max(0, Math.min(K - 1, Math.floor((bh - lo) / size)));
    if (k1 < k0) k1 = k0;
    for (let k = k0; k <= k1; k++) {
      const cl = lo + k * size, ch = cl + size;
      const ov = Math.max(0, Math.min(bh, ch) - Math.max(bl, cl));
      const frac = span > 0 ? ov / span : 1 / (k1 - k0 + 1);
      const part = v * frac;
      if (isUp) up[k] += part; else dn[k] += part;
    }
  }
  const tot = up.map((u, i) => u + dn[i]);
  const total = tot.reduce((a, b) => a + b, 0);
  if (total <= 0) return null;
  let poc = 0; for (let i = 1; i < K; i++) if (tot[i] > tot[poc]) poc = i;
  let loI = poc, hiI = poc, acc = tot[poc];
  const target = total * vaPct;
  while (acc < target && (loI > 0 || hiI < K - 1)) {
    const below = loI > 0 ? tot[loI - 1] : -1;
    const above = hiI < K - 1 ? tot[hiI + 1] : -1;
    if (above >= below) { hiI++; acc += tot[hiI]; } else { loI--; acc += tot[loI]; }
  }
  let vmax = 0; for (let k = loI; k <= hiI; k++) if (tot[k] > vmax) vmax = tot[k];
  const bands: ZoneBand[] = [];
  for (let k = loI; k <= hiI; k++) {
    if (tot[k] <= 0) continue;
    const cl = lo + k * size;
    bands.push({ lo: cl, hi: cl + size, bull: up[k] >= dn[k], opacity: 0.05 + 0.36 * (tot[k] / (vmax || 1)) });
  }
  return { bands, poc: lo + (poc + 0.5) * size };
}
const OVERLAY_COLORS = ["#f59e0b", "#8b5cf6", "#3b82f6", "#ec4899", "#10b981", "#f97316", "#6366f1"];

interface Props {
  data: PriceBar[];
  markers?: TradeMarker[];
  indicators?: Record<string, IndicatorPoint[]>;
  height?: number;
  initialOverlays?: Overlay[];
  currentIv?: number;
  priceLines?: { price: number; label: string; color?: string; dashed?: boolean }[];
  initialRange?: Range;
}

export function CandlestickChart({ data, markers, indicators, height = 500, initialOverlays, currentIv, priceLines, initialRange }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ReturnType<typeof echarts.init> | null>(null);
  const [subs, setSubs] = useState<Set<Sub>>(new Set(["vol"]));
  const [range, setRange] = useState<Range>(initialRange ?? "ALL");
  const toggleSub = useCallback((id: Sub) => {
    setSubs((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);
  const [overlays, setOverlays] = useState<Set<Overlay>>(new Set(initialOverlays ?? ["ma5", "ma20"]));
  const [showLines, setShowLines] = useState(true);
  const [showZone, setShowZone] = useState(true);
  const [showMenu, setShowMenu] = useState(false);
  const { dark } = useDarkMode();

  const toggleOverlay = useCallback((id: Overlay) => {
    setOverlays(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  // Memoize base data arrays — only recompute when raw data changes
  const baseData = useMemo(() => {
    const dates = data.map(d => d.time);
    const closes = data.map(d => d.close);
    const highs = data.map(d => d.high);
    const lows = data.map(d => d.low);
    const opens = data.map(d => d.open);
    const candle = data.map(d => [d.open, d.close, d.low, d.high]);
    return { dates, closes, highs, lows, opens, candle };
  }, [data]);

  // Memoize indicator calculations — only recompute when data changes (not on overlay toggle)
  const indicatorCache = useMemo(() => ({
    ma5: calcMA(baseData.closes, 5),
    ma10: calcMA(baseData.closes, 10),
    ma20: calcMA(baseData.closes, 20),
    ma60: calcMA(baseData.closes, 60),
    ema5: calcEMA(baseData.closes, 5),
    ema10: calcEMA(baseData.closes, 10),
    ema15: calcEMA(baseData.closes, 15),
    ema20: calcEMA(baseData.closes, 20),
    ema12: calcEMA(baseData.closes, 12),
    ema26: calcEMA(baseData.closes, 26),
    boll: calcBOLL(baseData.closes, 20, 2),
    macd: calcMACD(baseData.closes),
    rsi: calcRSI(baseData.closes),
    kdj: calcKDJ(baseData.highs, baseData.lows, baseData.closes),
    hv: calcHVSeries(baseData.closes, 20),
  }), [baseData]);

  // Memoize backend indicator series with Map lookup (O(1) instead of O(n) find)
  const extraIndicators = useMemo(() => {
    if (!indicators) return [];
    return Object.entries(indicators).map(([name, points]) => {
      const lookup = new Map(points.map(p => [p.time, p.value]));
      return { name: name.toUpperCase(), values: baseData.dates.map(d => lookup.get(d) ?? null) };
    });
  }, [indicators, baseData.dates]);

  // Init chart instance — only on mount/unmount and dark mode change
  useEffect(() => {
    if (!containerRef.current || data.length === 0) return;
    const chart = echarts.init(containerRef.current);
    chart.group = CHART_GROUP;
    connectCharts();
    chartRef.current = chart;

    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(containerRef.current);
    return () => { ro.disconnect(); chart.dispose(); chartRef.current = null; };
  }, [data.length === 0, dark]); // only re-init when going empty↔non-empty or theme changes

  // Update chart options — setOption on existing instance, no dispose
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || data.length === 0) return;

    const t = getChartTheme();
    const { dates, closes, opens, candle } = baseData;

    // Overlay series
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const overlaySeries: any[] = [];
    const legendNames: string[] = ["K"];
    let colorIdx = 0;

    const overlayMap: Record<string, { name: string; data: (number | null)[] }> = {
      ma5: { name: "MA5", data: indicatorCache.ma5 },
      ma10: { name: "MA10", data: indicatorCache.ma10 },
      ma20: { name: "MA20", data: indicatorCache.ma20 },
      ma60: { name: "MA60", data: indicatorCache.ma60 },
      ema5: { name: "EMA5", data: indicatorCache.ema5 },
      ema10: { name: "EMA10", data: indicatorCache.ema10 },
      ema15: { name: "EMA15", data: indicatorCache.ema15 },
      ema20: { name: "EMA20", data: indicatorCache.ema20 },
      ema12: { name: "EMA12", data: indicatorCache.ema12 },
      ema26: { name: "EMA26", data: indicatorCache.ema26 },
    };

    for (const [key, { name, data: lineData }] of Object.entries(overlayMap)) {
      if (overlays.has(key as Overlay)) {
        overlaySeries.push({ name, type: "line", data: lineData, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: OVERLAY_COLORS[colorIdx], width: 1 } });
        legendNames.push(name);
        colorIdx++;
      }
    }

    if (overlays.has("boll")) {
      const boll = indicatorCache.boll;
      overlaySeries.push(
        { name: "BOLL+", type: "line", data: boll.upper, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: t.bollColor, width: 0.8, type: "dashed" } },
        { name: "BOLL", type: "line", data: boll.mid, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: t.bollColor, width: 1 } },
        { name: "BOLL-", type: "line", data: boll.lower, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: t.bollColor, width: 0.8, type: "dashed" } },
      );
      legendNames.push("BOLL");
    }

    // Trade markers. Live (unresolved) pullback_hv setups -- today / last ~10
    // sessions whose forward return isn't measurable yet -- are drawn amber with
    // a "B?" so they read as "current setup", distinct from backtested green Bs.
    const marks = (markers || []).map(m => ({
      coord: [m.time, m.price],
      value: m.live ? "B?" : (m.side === "BUY" ? "B" : "S"),
      name: [`${m.live ? "今日/近期设置(未到期)" : m.side} @ ${m.price}`, m.qty ? `Qty: ${m.qty}` : "", m.reason || ""].filter(Boolean).join("\n"),
      itemStyle: { color: m.live ? t.warningColor : (m.side === "BUY" ? t.upColor : t.downColor) },
      label: { color: "#fff", fontSize: 10, fontWeight: "bold" as const },
    }));

    // Volume
    const vol = data.map((d, i) => ({
      value: d.volume,
      itemStyle: { color: closes[i] >= opens[i] ? t.volumeUp : t.volumeDown },
    }));

    // === Sub-charts (multi-select, each its own stacked grid below the main) ===
    const SUB_ORDER: Sub[] = ["vol", "macd", "rsi", "kdj", "hv"];
    const SUB_LEGEND: Record<Sub, string[]> = { vol: ["Vol"], macd: ["DIF", "DEA", "MACD"], rsi: ["RSI"], kdj: ["%K", "%D", "%J"], hv: ["HV20"] };
    const activeSubs = SUB_ORDER.filter((s) => subs.has(s));

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const buildSub = (id: Sub, gi: number): { series: any[]; yAxis: any } => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const baseY: any = { scale: true, gridIndex: gi, splitLine: { lineStyle: { color: t.gridColor } }, axisLabel: { color: t.textColor, fontSize: 10 } };
      if (id === "vol") return { series: [{ name: "Vol", type: "bar", data: vol, xAxisIndex: gi, yAxisIndex: gi }], yAxis: { ...baseY, axisLabel: { ...baseY.axisLabel, formatter: (v: number) => abbreviateNum(v) } } };
      if (id === "macd") {
        const m = indicatorCache.macd;
        return { series: [
          { name: "DIF", type: "line", data: m.dif, xAxisIndex: gi, yAxisIndex: gi, symbol: "none", lineStyle: { width: 1, color: t.infoColor } },
          { name: "DEA", type: "line", data: m.signal, xAxisIndex: gi, yAxisIndex: gi, symbol: "none", lineStyle: { width: 1, color: t.warningColor } },
          { name: "MACD", type: "bar", data: m.histogram.map((v) => ({ value: v ?? 0, itemStyle: { color: (v ?? 0) >= 0 ? t.upColor : t.downColor } })), xAxisIndex: gi, yAxisIndex: gi },
        ], yAxis: baseY };
      }
      if (id === "rsi") return { series: [{ name: "RSI", type: "line", data: indicatorCache.rsi, xAxisIndex: gi, yAxisIndex: gi, symbol: "none", lineStyle: { width: 1.5, color: t.infoColor } }], yAxis: { ...baseY, min: 0, max: 100 } };
      if (id === "hv") return { series: [{
        name: "HV20", type: "line", data: indicatorCache.hv, xAxisIndex: gi, yAxisIndex: gi, symbol: "none", lineStyle: { width: 1.5, color: t.infoColor },
        markLine: currentIv != null && currentIv > 0 ? { symbol: "none", silent: true, data: [{ yAxis: Number((currentIv * 100).toFixed(1)) }], lineStyle: { color: t.warningColor, type: "dashed", width: 1 }, label: { formatter: `IV ${(currentIv * 100).toFixed(0)}%`, color: t.warningColor, fontSize: 10, position: "insideEndTop" as const } } : undefined,
      }], yAxis: { ...baseY, axisLabel: { ...baseY.axisLabel, formatter: (v: number) => `${v.toFixed(0)}%` } } };
      const kdj = indicatorCache.kdj;
      return { series: [
        { name: "%K", type: "line", data: kdj.k, xAxisIndex: gi, yAxisIndex: gi, symbol: "none", lineStyle: { width: 1, color: t.infoColor } },
        { name: "%D", type: "line", data: kdj.d, xAxisIndex: gi, yAxisIndex: gi, symbol: "none", lineStyle: { width: 1, color: t.warningColor } },
        { name: "%J", type: "line", data: kdj.j, xAxisIndex: gi, yAxisIndex: gi, symbol: "none", lineStyle: { width: 1, color: "#a855f7" } },
      ], yAxis: baseY };
    };

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const allSubSeries: any[] = [];
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const subYAxes: any[] = [];
    activeSubs.forEach((id, i) => {
      const { series, yAxis } = buildSub(id, i + 1);
      allSubSeries.push(...series);
      subYAxes.push(yAxis);
      legendNames.push(...SUB_LEGEND[id]);
    });

    // Dynamic grid layout: main chart on top, sub-charts stacked below.
    const N = activeSubs.length;
    const TOP = 6, BOTTOM = 8, GAP = 3;
    const avail = 100 - TOP - BOTTOM;
    const subH = N > 0 ? Math.min(20, (avail * 0.5) / N) : 0;
    const mainH = avail - N * (subH + GAP);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const grids: any[] = [{ left: 8, right: 8, top: `${TOP}%`, height: `${mainH}%`, containLabel: true }];
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const xAxes: any[] = [{ type: "category", data: dates, gridIndex: 0, axisLine: { lineStyle: { color: t.axisColor } }, axisLabel: { color: t.textColor, fontSize: 10 }, boundaryGap: true }];
    let cursor = TOP + mainH + GAP;
    activeSubs.forEach((_id, i) => {
      grids.push({ left: 8, right: 8, top: `${cursor}%`, height: `${subH}%`, containLabel: true });
      xAxes.push({ type: "category", data: dates, gridIndex: i + 1, axisLine: { lineStyle: { color: t.axisColor } }, axisLabel: { show: false }, boundaryGap: true });
      cursor += subH + GAP;
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const yAxes: any[] = [{ scale: true, gridIndex: 0, splitLine: { lineStyle: { color: t.gridColor } }, axisLabel: { color: t.textColor, fontSize: 10 } }, ...subYAxes];
    const allXIdx = Array.from({ length: N + 1 }, (_unused, i) => i);

    // Backend custom indicators (Map-based O(1) lookup)
    const extraSeries = extraIndicators.map((ind, i) => {
      legendNames.push(ind.name);
      return { name: ind.name, type: "line" as const, data: ind.values, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: OVERLAY_COLORS[(colorIdx + i) % OVERLAY_COLORS.length], type: "dashed" as const } };
    });

    const maxBars = RANGE_BARS[range];
    const defaultStart = maxBars >= data.length ? 0 : Math.max(0, 100 - (maxBars / data.length) * 100);

    // 多空博弈区间: volume-profile value area over the CURRENTLY VISIBLE window.
    // Recomputed on range-button change AND on mouse-wheel zoom/pan (see the
    // dataZoom handler below), so it always matches what you're looking at.
    const visibleBars = (startPct: number, endPct: number): PriceBar[] => {
      const nb = data.length;
      if (nb === 0) return [];
      const i0 = Math.max(0, Math.round((startPct / 100) * (nb - 1)));
      const i1 = Math.min(nb, Math.round((endPct / 100) * (nb - 1)) + 1);
      return data.slice(i0, i1 > i0 ? i1 : i0 + 1);
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const zoneSeriesOpt = (z: ReturnType<typeof computeBattleZone>): any => ({
      id: "battlezone", name: "博弈区", type: "line", data: [], xAxisIndex: 0, yAxisIndex: 0, silent: true, z: 0, showSymbol: false,
      markArea: {
        silent: true,
        data: z
          ? z.bands.map((b) => [
              { yAxis: b.lo, itemStyle: { color: b.bull ? t.upColor : t.downColor, opacity: Number(b.opacity.toFixed(3)) } },
              { yAxis: b.hi },
            ])
          : [],
      },
      markLine: {
        symbol: "none", silent: true,
        data: z
          ? [{
              yAxis: Number(z.poc.toFixed(2)),
              lineStyle: { color: t.warningColor, type: "dashed", width: 1.2 },
              label: { formatter: `POC ${z.poc.toFixed(2)}`, color: t.warningColor, fontSize: 9, position: "insideStartTop" as const },
            }]
          : [],
      },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const zoneSeries: any[] = showZone ? [zoneSeriesOpt(computeBattleZone(visibleBars(defaultStart, 100)))] : [];

    chart.setOption({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis", axisPointer: { type: "cross" },
        backgroundColor: t.tooltipBg, borderColor: t.tooltipBorder,
        textStyle: { color: t.tooltipText, fontSize: 11 },
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (params: any) => {
          if (!Array.isArray(params) || !params.length) return "";
          let html = `<b>${params[0].axisValue}</b>`;
          for (const p of params) {
            if (p.seriesName === "K" && Array.isArray(p.value)) {
              const [open, close, low, high] = p.value;
              const chg = close - open;
              const pct = open ? ((chg / open) * 100).toFixed(2) : "0.00";
              const clr = chg >= 0 ? t.upColor : t.downColor;
              html += `<br/>O: ${open.toFixed(2)}&nbsp; H: ${high.toFixed(2)}`;
              html += `<br/>L: ${low.toFixed(2)}&nbsp; C: <span style="color:${clr}"><b>${close.toFixed(2)}</b> ${chg >= 0 ? "+" : ""}${chg.toFixed(2)} (${chg >= 0 ? "+" : ""}${pct}%)</span>`;
            } else if (p.seriesName === "Vol") {
              html += `<br/>Vol: ${abbreviateNum(Number(p.value))}`;
            } else if (p.value != null) {
              html += `<br/>${p.marker} ${p.seriesName}: ${Number(p.value).toFixed(2)}`;
            }
          }
          return html;
        },
      },
      toolbox: {
        feature: { saveAsImage: { title: "Save" }, dataZoom: { title: { zoom: "Zoom", back: "Reset" } }, restore: { title: "Reset" } },
        right: 8, top: 0, iconStyle: { borderColor: t.textColor },
      },
      legend: { data: legendNames, textStyle: { color: t.textColor, fontSize: 10 }, right: 80, top: 2, type: "scroll", itemWidth: 12, itemHeight: 8, itemGap: 8 },
      // Link the crosshair across the main chart + every sub-chart grid so the
      // vertical dashed line runs through vol/macd/rsi/kdj/hv together.
      axisPointer: {
        link: [{ xAxisIndex: "all" }],
        lineStyle: { color: t.axisColor, type: "dashed", width: 1 },
        label: { backgroundColor: t.tooltipBg, color: t.tooltipText, borderColor: t.tooltipBorder, borderWidth: 1 },
      },
      grid: grids,
      xAxis: xAxes,
      yAxis: yAxes,
      dataZoom: [
        { type: "inside", xAxisIndex: allXIdx, start: defaultStart, end: 100 },
        { type: "slider", xAxisIndex: allXIdx, bottom: 4, height: 20, labelFormatter: (val: string) => val },
      ],
      series: [
        ...zoneSeries,
        {
          name: "K", type: "candlestick", data: candle, xAxisIndex: 0, yAxisIndex: 0,
          itemStyle: { color: t.upColor, color0: t.downColor, borderColor: t.upColor, borderColor0: t.downColor },
          markPoint: marks.length > 0 ? { data: marks, symbolSize: 28, tooltip: { formatter: (p: { name?: string; value?: string }) => p.name || p.value || "" } } : undefined,
          markLine: (showLines && priceLines && priceLines.length > 0) ? {
            symbol: "none", silent: true,
            // Sort by price + alternate label side (left/right) so stacked levels
            // near the current price don't overlap into an unreadable clump.
            // When the battle zone is on, drop the fib lines (the zone already
            // supplies support/resistance context) so labels don't clump.
            data: priceLines.filter((l) => Number.isFinite(l.price) && !(showZone && /^fib/i.test(l.label)))
              .slice().sort((a, b) => b.price - a.price)
              .map((l, idx) => ({
                yAxis: l.price,
                lineStyle: { color: l.color || t.infoColor, type: l.dashed ? "dashed" : "solid", width: 1 },
                label: {
                  formatter: l.label, color: l.color || t.textColor, fontSize: 9,
                  position: idx % 2 === 0 ? "insideEndTop" as const : "insideStartTop" as const,
                },
              })),
          } : undefined,
        },
        ...overlaySeries,
        ...extraSeries,
        ...allSubSeries,
      ],
    }, true);

    // Recompute the battle zone whenever the visible window changes via the
    // mouse wheel / drag (debounced) so it auto-matches the scrolled range.
    let zoomTimer: number | null = null;
    const onZoom = () => {
      if (!showZone) return;
      if (zoomTimer) window.clearTimeout(zoomTimer);
      zoomTimer = window.setTimeout(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const opt = chart.getOption() as any;
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const dz = Array.isArray(opt.dataZoom) ? opt.dataZoom.find((d: any) => d && typeof d.start === "number") : null;
        const s = dz ? dz.start : defaultStart;
        const e = dz ? dz.end : 100;
        chart.setOption({ series: [zoneSeriesOpt(computeBattleZone(visibleBars(s, e)))] });
      }, 120);
    };
    chart.off("dataZoom");
    chart.on("dataZoom", onZoom);
    return () => {
      chart.off("dataZoom", onZoom);
      if (zoomTimer) window.clearTimeout(zoomTimer);
    };
  }, [data, markers, baseData, indicatorCache, extraIndicators, subs, range, overlays, dark, currentIv, priceLines, showLines, showZone]);

  if (data.length === 0) {
    return <div className="text-muted-foreground text-sm p-4">No price data</div>;
  }

  return (
    <div>
      <div className="flex items-center gap-2 mb-1 flex-wrap">
        {/* Time range */}
        <div className="flex gap-0.5">
          {(["1M", "3M", "6M", "1Y", "ALL"] as const).map((r) => (
            <button key={r} onClick={() => setRange(r)} className={cn("px-1.5 py-0.5 rounded text-[10px] font-mono transition-colors", range === r ? "bg-primary/15 text-primary font-medium" : "text-muted-foreground/50 hover:text-muted-foreground")}>{r}</button>
          ))}
        </div>

        <div className="w-px h-3 bg-border/40" />

        {/* Indicator dropdown */}
        <div className="relative">
          <button
            onClick={() => setShowMenu(!showMenu)}
            className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-colors"
          >
            Indicators ({overlays.size}) <ChevronDown className="h-3 w-3" />
          </button>
          {showMenu && (
            <div className="absolute top-full left-0 mt-1 z-50 bg-card border rounded-lg shadow-lg p-2 min-w-[160px]" onMouseLeave={() => setShowMenu(false)}>
              {["MA", "EMA", "Channel"].map(group => (
                <div key={group}>
                  <p className="text-[9px] text-muted-foreground/50 uppercase tracking-wider px-1 pt-1">{group}</p>
                  {OVERLAY_OPTIONS.filter(o => o.group === group).map(o => (
                    <label key={o.id} className="flex items-center gap-2 px-1 py-0.5 rounded hover:bg-muted/30 cursor-pointer">
                      <input type="checkbox" checked={overlays.has(o.id)} onChange={() => toggleOverlay(o.id)} className="h-3 w-3 rounded accent-primary" />
                      <span className="text-xs">{o.label}</span>
                    </label>
                  ))}
                </div>
              ))}
              <div className="border-t mt-1 pt-1">
                <button onClick={() => { setOverlays(new Set()); setShowMenu(false); }} className="text-[10px] text-muted-foreground hover:text-foreground px-1 py-0.5 w-full text-left rounded hover:bg-muted/30">
                  Bare K (clear all)
                </button>
              </div>
            </div>
          )}
        </div>

        <div className="w-px h-3 bg-border/40" />

        {/* Sub-chart selector */}
        <div className="flex gap-0.5">
          {(["vol", "macd", "rsi", "kdj", "hv"] as const).map((id) => (
            <button key={id} onClick={() => toggleSub(id)} title="副图可多选叠加" className={cn("px-1.5 py-0.5 rounded text-[10px] font-mono uppercase transition-colors", subs.has(id) ? "bg-primary/15 text-primary font-medium" : "text-muted-foreground/50 hover:text-muted-foreground")}>{id}</button>
          ))}
        </div>

        <div className="w-px h-3 bg-border/40" />
        <button onClick={() => setShowZone((s) => !s)} className={cn("px-1.5 py-0.5 rounded text-[10px] font-mono transition-colors", showZone ? "bg-primary/15 text-primary font-medium" : "text-muted-foreground/50 hover:text-muted-foreground")} title="多空博弈区间：成交量价值区（绿=多头主导/红=空头主导，色越深成交越密），虚线=POC控制点">博弈区</button>

        {priceLines && priceLines.length > 0 && (
          <>
            <div className="w-px h-3 bg-border/40" />
            <button onClick={() => setShowLines((s) => !s)} className={cn("px-1.5 py-0.5 rounded text-[10px] font-mono transition-colors", showLines ? "bg-primary/15 text-primary font-medium" : "text-muted-foreground/50 hover:text-muted-foreground")} title="行动价位线（买入/止损/止盈/现价）">价位线</button>
          </>
        )}
      </div>
      {showZone && (
        <div className="mb-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-muted-foreground">
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: "hsl(var(--success))" }} />
            绿=该价位<span className="text-foreground">买方(多头)主导·吸筹</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: "hsl(var(--danger))" }} />
            红=该价位<span className="text-foreground">卖方(空头)主导·出货</span>
          </span>
          <span>色越深=成交量越大(博弈越激烈)</span>
          <span className="inline-flex items-center gap-1">
            <span className="inline-block w-3 border-t border-dashed" style={{ borderColor: "hsl(var(--warning))" }} />
            黄虚线=POC公允价
          </span>
          <span className="opacity-80">看启动:放量站上/突破博弈区上沿→偏多启动;回落到绿区支撑→低吸;红区=上方套牢压力。(涨跌量代理，辅助看盘非独立信号)</span>
        </div>
      )}
      <div ref={containerRef} style={{ height }} />
    </div>
  );
}
