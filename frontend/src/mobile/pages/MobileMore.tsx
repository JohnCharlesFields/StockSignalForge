import { Link } from "react-router-dom";
import {
  Activity,
  BarChart3,
  CalendarClock,
  ChevronRight,
  LayoutDashboard,
  Layers,
  Monitor,
  Radar,
  Radio,
  Rocket,
  Star,
  TrendingUp,
} from "lucide-react";
import { setForceDesktop } from "../useIsMobile";

interface Item {
  label: string;
  icon: typeof Star;
  to: string;
  external?: boolean;
}

const NATIVE: Item[] = [{ label: "自选池", icon: Star, to: "/m/watchlist" }];

// Advanced research pages render as the desktop layout (labelled fallback).
const RESEARCH: Item[] = [
  { label: "启动信号(原始·研究)", icon: Rocket, to: "/launch-signal", external: true },
  { label: "实股信号", icon: TrendingUp, to: "/stock-signals", external: true },
  { label: "事件雷达", icon: Radar, to: "/event-radar", external: true },
  { label: "宏观恐慌雷达", icon: Activity, to: "/macro-panic-radar", external: true },
  { label: "三维信号汇总", icon: LayoutDashboard, to: "/signal-dashboard", external: true },
  { label: "隔夜驾驶舱", icon: BarChart3, to: "/overnight-cockpit", external: true },
  { label: "博主观点雷达", icon: Radio, to: "/creator-opinions", external: true },
  { label: "任务中心", icon: CalendarClock, to: "/batch-tasks", external: true },
  // 研究助手(AI 对话)已隐藏 —— 只改代码不用自然语言交互。
  { label: "Alpha Zoo", icon: Layers, to: "/alpha-zoo", external: true },
  { label: "相关性矩阵", icon: BarChart3, to: "/correlation", external: true },
];

function Tile({ item }: { item: Item }) {
  const Icon = item.icon;
  const inner = (
    <div className="flex items-center gap-3 rounded-xl border bg-card p-3.5 active:bg-muted/40">
      <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary">
        <Icon className="h-5 w-5" />
      </span>
      <span className="min-w-0 flex-1 truncate text-sm font-medium">{item.label}</span>
      <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
    </div>
  );
  if (item.external) {
    return (
      <a href={item.to} className="block">
        {inner}
      </a>
    );
  }
  return (
    <Link to={item.to} className="block">
      {inner}
    </Link>
  );
}

export function MobileMore() {
  const openDesktop = () => {
    setForceDesktop(true);
    window.location.href = "/";
  };

  return (
    <div className="space-y-5 p-3 pb-6">
      <section className="space-y-2">
        <h2 className="px-1 text-xs font-semibold text-muted-foreground">交易台</h2>
        <div className="space-y-2">
          {NATIVE.map((it) => (
            <Tile key={it.to} item={it} />
          ))}
        </div>
      </section>

      <section className="space-y-2">
        <h2 className="px-1 text-xs font-semibold text-muted-foreground">进阶 · 研究</h2>
        <p className="px-1 text-[11px] text-muted-foreground/80">这些是桌面版页面，手机上以桌面布局打开，横屏查看更佳。</p>
        <div className="space-y-2">
          {RESEARCH.map((it) => (
            <Tile key={it.to} item={it} />
          ))}
        </div>
      </section>

      <section className="space-y-2">
        <h2 className="px-1 text-xs font-semibold text-muted-foreground">系统</h2>
        <button
          type="button"
          onClick={openDesktop}
          className="flex w-full items-center gap-3 rounded-xl border bg-card p-3.5 text-left active:bg-muted/40"
        >
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary">
            <Monitor className="h-5 w-5" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-sm font-medium">切换到桌面完整版</span>
            <span className="block text-[11px] text-muted-foreground">本次会话生效，重开飞书自动回到手机版</span>
          </span>
          <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
        </button>
      </section>

      <p className="px-1 text-center text-[11px] text-muted-foreground/60">easymoneysniper · 手机版</p>
    </div>
  );
}
