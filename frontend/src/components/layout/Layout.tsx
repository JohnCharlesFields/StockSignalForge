import { useEffect, useState } from "react";
import { Link, Outlet, useLocation, useSearchParams } from "react-router-dom";
import {
  Activity,
  BarChart3,
  Bot,
  Briefcase,
  CalendarClock,
  ChevronDown,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  CircleDot,
  Crown,
  LayoutDashboard,
  Layers,
  MessageSquare,
  Moon,
  Newspaper,
  Pencil,
  Plus,
  Radar,
  Radio,
  Rocket,
  SearchCheck,
  Settings,
  Star,
  Sun,
  Trash2,
  TrendingUp,
  Zap,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useI18n } from "@/lib/i18n";
import { useDarkMode } from "@/hooks/useDarkMode";
import { api, type SessionItem } from "@/lib/api";
import { useAgentStore } from "@/stores/agent";
import { ConnectionBanner } from "@/components/layout/ConnectionBanner";
import { TopPickBanner } from "@/components/layout/TopPickBanner";

const APP_VERSION = "v0.1.8";

// 研究助手(自然语言 AI 对话)模块已隐藏 —— 仅改代码不用对话交互。
// 路由与页面仍在,改回 true 即可恢复导航入口与左侧会话列表。
const SHOW_AGENT = false;

const COLLAPSIBLE_SECTIONS = new Set(["进阶 · 研究"]);

const NAV = [
  { to: "/", icon: Crown, key: "home" as const, label: "回调买入榜", section: "交易台" },
  { to: "/premarket-news", icon: Newspaper, key: "home" as const, label: "盘前新闻", section: "交易台" },
  { to: "/single-stock-overnight", icon: SearchCheck, key: "home" as const, label: "个股研判", section: "交易台" },
  { to: "/watchlist", icon: Star, key: "home" as const, label: "自选池", section: "交易台" },
  { to: "/creator-opinions", icon: Radio, key: "home" as const, label: "博主观点雷达", section: "交易台" },
  { to: "/portfolio", icon: Briefcase, key: "home" as const, label: "持仓决策", section: "交易台" },
  { to: "/soxl-quant", icon: Zap, key: "home" as const, label: "SOXL 实时模拟", section: "交易台" },
  { to: "/batch-tasks", icon: CalendarClock, key: "home" as const, label: "任务中心", section: "交易台" },
  { to: "/launch-signal", icon: Rocket, key: "home" as const, label: "启动信号(原始·研究)", section: "进阶 · 研究" },
  { to: "/stock-signals", icon: TrendingUp, key: "home" as const, label: "实股信号", section: "进阶 · 研究" },
  { to: "/event-radar", icon: Radar, key: "home" as const, label: "事件雷达", section: "进阶 · 研究" },
  { to: "/macro-panic-radar", icon: Activity, key: "home" as const, label: "宏观恐慌雷达", section: "进阶 · 研究" },
  { to: "/signal-dashboard", icon: LayoutDashboard, key: "home" as const, label: "三维信号汇总", section: "进阶 · 研究" },
  { to: "/overnight-cockpit", icon: BarChart3, key: "home" as const, label: "隔夜驾驶舱", section: "进阶 · 研究" },
  { to: "/agent", icon: Bot, key: "agent" as const, label: "研究助手", section: "进阶 · 研究" },
  { to: "/alpha-zoo", icon: Layers, key: "alphaZoo" as const, label: "Alpha Zoo", section: "进阶 · 研究" },
  { to: "/correlation", icon: BarChart3, key: "correlation" as const, label: "相关性矩阵", section: "进阶 · 研究" },
  { to: "/settings", icon: Settings, key: "settings" as const, label: "设置", section: "系统" },
];

export function Layout() {
  const { pathname } = useLocation();
  const [searchParams] = useSearchParams();
  const { t, lang, toggleLang } = useI18n();
  const { dark, toggle } = useDarkMode();
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const sseStatus = useAgentStore((s) => s.sseStatus);
  const sseRetryAttempt = useAgentStore((s) => s.sseRetryAttempt);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("qa-sidebar") === "collapsed");
  const [openSections, setOpenSections] = useState<Record<string, boolean>>(() => {
    try {
      return JSON.parse(localStorage.getItem("qa-nav-sections") || "{}");
    } catch {
      return {};
    }
  });
  const isSectionOpen = (section: string) =>
    !COLLAPSIBLE_SECTIONS.has(section) || openSections[section] === true;
  const toggleSection = (section: string) =>
    setOpenSections((prev) => {
      const next = { ...prev, [section]: !(prev[section] === true) };
      localStorage.setItem("qa-nav-sections", JSON.stringify(next));
      return next;
    });
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const activeSessionId = searchParams.get("session");
  const isAgentPage = pathname.startsWith("/agent");

  useEffect(() => {
    localStorage.setItem("qa-sidebar", collapsed ? "collapsed" : "expanded");
  }, [collapsed]);

  const loadSessions = () => {
    api
      .listSessions()
      .then((list) => setSessions(Array.isArray(list) ? list : []))
      .catch(() => {})
      .finally(() => setSessionsLoading(false));
  };

  useEffect(() => {
    loadSessions();
  }, [isAgentPage, activeSessionId]);

  const deleteSession = async (sid: string) => {
    try {
      await api.deleteSession(sid);
      setSessions((prev) => prev.filter((s) => s.session_id !== sid));
    } catch {
      /* ignore */
    }
    setDeleteTarget(null);
  };

  const renameSession = async (sid: string) => {
    const title = renameValue.trim();
    if (!title) {
      setRenameTarget(null);
      return;
    }
    try {
      await api.renameSession(sid, title);
      setSessions((prev) => prev.map((s) => (s.session_id === sid ? { ...s, title } : s)));
    } catch {
      /* ignore */
    }
    setRenameTarget(null);
  };

  return (
    <div className="flex h-screen bg-background">
      <aside className={cn("flex shrink-0 flex-col border-r bg-card transition-all duration-200", collapsed ? "w-12" : "w-64")}>
        <div className={cn("border-b", collapsed ? "flex justify-center p-2" : "p-4")}>
          <Link to="/" className={cn("flex items-center text-sm font-bold tracking-tight", collapsed ? "justify-center" : "gap-2")}>
            <span className="relative grid h-7 w-7 shrink-0 place-items-center rounded-full border-2 border-amber-500/80 text-[9px] font-black text-amber-500">
              <CircleDot className="absolute h-5 w-5 opacity-45" />
              <span className="relative">35</span>
            </span>
            {!collapsed && (
              <span>
                <span className="block">StockSignalForge</span>
                <span className="block text-[9px] font-medium uppercase tracking-widest text-muted-foreground">research board</span>
              </span>
            )}
          </Link>
        </div>

        <nav className={cn("space-y-0.5", collapsed ? "p-1" : "p-2")}>
          {NAV.filter((n) => SHOW_AGENT || n.to !== "/agent").map(({ to, icon: Icon, label, section }, index, arr) => {
            const isFirstOfSection = index === 0 || arr[index - 1].section !== section;
            const collapsibleSection = COLLAPSIBLE_SECTIONS.has(section);
            const sectionOpen = isSectionOpen(section);
            // When collapsed-section is closed, hide its items entirely (but
            // always show the section header so it can be re-opened).
            if (!collapsed && collapsibleSection && !sectionOpen && !isFirstOfSection) {
              return null;
            }
            return (
              <div key={to}>
                {!collapsed && isFirstOfSection && (
                  collapsibleSection ? (
                    <button
                      type="button"
                      onClick={() => toggleSection(section)}
                      className={cn(
                        "flex w-full items-center gap-1 px-3 pb-1 text-[10px] font-semibold text-muted-foreground/70 hover:text-foreground",
                        index > 0 && "pt-3",
                      )}
                    >
                      {sectionOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                      {section}
                    </button>
                  ) : (
                    <div className={cn("px-3 pb-1 text-[10px] font-semibold text-muted-foreground/70", index > 0 && "pt-3")}>
                      {section}
                    </div>
                  )
                )}
                {(!collapsibleSection || sectionOpen || collapsed) && (
                  <Link
                    to={to}
                    className={cn(
                      "flex items-center rounded-md text-sm transition-colors",
                      collapsed ? "justify-center p-2" : "gap-3 px-3 py-2",
                      (to === "/" ? pathname === "/" : pathname.startsWith(to))
                        ? "bg-primary/10 font-medium text-primary"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                    title={collapsed ? label : undefined}
                  >
                    <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                    {!collapsed && label}
                  </Link>
                )}
              </div>
            );
          })}
        </nav>

        {SHOW_AGENT && !collapsed && (
          <div className="mt-2 flex flex-1 flex-col overflow-auto border-t">
            <div className="flex items-center justify-between px-4 py-2">
              <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <MessageSquare className="h-3.5 w-3.5" />
                {t.sessions}
              </span>
              <Link to="/agent" className="flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground" title={t.newChat}>
                <Plus className="h-3.5 w-3.5" />
              </Link>
            </div>

            <div className="flex-1 space-y-0.5 overflow-auto px-2 pb-2">
              {sessionsLoading ? (
                <div className="space-y-1.5 px-2 py-1">
                  {[1, 2, 3].map((i) => (
                    <div key={i} className="h-7 animate-pulse rounded-md bg-muted/50" />
                  ))}
                </div>
              ) : sessions.length === 0 ? (
                <p className="px-3 py-2 text-xs text-muted-foreground/60">{t.noSessions}</p>
              ) : null}
              {sessions.map((s) => {
                const isActive = s.session_id === activeSessionId;
                const isDeleting = deleteTarget === s.session_id;
                const isRenaming = renameTarget === s.session_id;
                return (
                  <div key={s.session_id} className="group relative flex items-center">
                    {isRenaming ? (
                      <input
                        autoFocus
                        value={renameValue}
                        onChange={(event) => setRenameValue(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") renameSession(s.session_id);
                          if (event.key === "Escape") setRenameTarget(null);
                        }}
                        onBlur={() => renameSession(s.session_id)}
                        className="min-w-0 flex-1 rounded-md border border-primary bg-background py-1 pl-3 pr-2 text-xs outline-none"
                      />
                    ) : (
                      <Link
                        to={`/agent?session=${s.session_id}`}
                        className={cn(
                          "block min-w-0 flex-1 truncate rounded-md border-l-2 py-1.5 pl-3 pr-14 text-xs transition-colors",
                          isActive
                            ? "border-l-primary bg-primary/10 font-medium text-primary"
                            : "border-l-transparent text-muted-foreground hover:bg-muted hover:text-foreground",
                        )}
                        title={s.title || s.session_id}
                      >
                        <span className="flex items-center gap-1.5">
                          <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", s.status === "failed" ? "bg-danger" : isActive ? "bg-warning" : "bg-success/60")} />
                          {s.title || s.session_id.slice(0, 16)}
                        </span>
                      </Link>
                    )}
                    {!isRenaming && isDeleting ? (
                      <div className="absolute right-0.5 flex items-center gap-0.5">
                        <button onClick={() => deleteSession(s.session_id)} className="rounded p-1 text-[10px] font-medium text-danger hover:bg-danger/10">
                          {t.confirmDelete}
                        </button>
                        <button onClick={() => setDeleteTarget(null)} className="rounded p-1 text-[10px] text-muted-foreground hover:bg-muted">
                          {t.cancelDelete}
                        </button>
                      </div>
                    ) : !isRenaming ? (
                      <div className="absolute right-1 flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                        <button
                          onClick={(event) => {
                            event.preventDefault();
                            event.stopPropagation();
                            setRenameTarget(s.session_id);
                            setRenameValue(s.title || "");
                          }}
                          className="rounded p-1 text-muted-foreground hover:text-foreground"
                          title="Rename"
                        >
                          <Pencil className="h-3 w-3" />
                        </button>
                        <button
                          onClick={(event) => {
                            event.preventDefault();
                            event.stopPropagation();
                            setDeleteTarget(s.session_id);
                          }}
                          className="rounded p-1 text-muted-foreground hover:text-danger"
                          title={t.deleteConfirm}
                        >
                          <Trash2 className="h-3 w-3" />
                        </button>
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {(collapsed || !SHOW_AGENT) && <div className="flex-1" />}

        <div className={cn("border-t", collapsed ? "flex flex-col items-center gap-1 p-1" : "space-y-2 p-3")}>
          {collapsed ? (
            <>
              <button onClick={toggle} className="rounded p-1.5 text-muted-foreground transition-colors hover:text-foreground" title={dark ? t.lightMode : t.darkMode}>
                {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
              </button>
              <button onClick={toggleLang} className="rounded px-1.5 py-1 text-[10px] font-medium text-muted-foreground transition-colors hover:text-foreground" title={t.language}>
                {lang === "zh" ? "EN" : "中"}
              </button>
              <button onClick={() => setCollapsed(false)} className="rounded p-1.5 text-muted-foreground transition-colors hover:text-foreground" title="Expand">
                <ChevronsRight className="h-3.5 w-3.5" />
              </button>
            </>
          ) : (
            <>
              <div className="flex items-center justify-between">
                <button onClick={toggle} className="flex items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground">
                  {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
                  {dark ? t.lightMode : t.darkMode}
                </button>
                <div className="flex items-center gap-1">
                  <button onClick={toggleLang} className="rounded px-2 py-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground" title={t.language}>
                    {t.languageToggle}
                  </button>
                  <button onClick={() => setCollapsed(true)} className="rounded p-1 text-muted-foreground transition-colors hover:text-foreground" title="Collapse">
                    <ChevronsLeft className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
              <p className="text-xs text-muted-foreground/60">{APP_VERSION}</p>
            </>
          )}
        </div>
      </aside>

      <div className="flex flex-1 flex-col overflow-hidden">
        <ConnectionBanner status={sseStatus} retryAttempt={sseRetryAttempt} />
        <TopPickBanner />
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
