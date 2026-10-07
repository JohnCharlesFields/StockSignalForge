import { useEffect } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { Briefcase, ChevronLeft, Crown, LayoutGrid, Moon, Newspaper, SearchCheck, Sun } from "lucide-react";
import { cn } from "@/lib/utils";
import { useDarkMode } from "@/hooks/useDarkMode";

interface Tab {
  to: string;
  label: string;
  icon: typeof Crown;
}

const TABS: Tab[] = [
  { to: "/m", label: "榜单", icon: Crown },
  { to: "/m/news", label: "新闻", icon: Newspaper },
  { to: "/m/stock", label: "研判", icon: SearchCheck },
  { to: "/m/portfolio", label: "持仓", icon: Briefcase },
  { to: "/m/more", label: "更多", icon: LayoutGrid },
];

const TITLES: Record<string, string> = {
  "/m": "回调买入榜",
  "/m/news": "盘前新闻",
  "/m/stock": "个股研判",
  "/m/portfolio": "持仓决策",
  "/m/watchlist": "自选池",
  "/m/more": "更多",
  "/m/settings": "设置",
};

const ROOT_PATHS = new Set(TABS.map((t) => t.to));

function titleFor(pathname: string): string {
  if (TITLES[pathname]) return TITLES[pathname];
  // longest-prefix match for nested paths
  const hit = Object.keys(TITLES)
    .filter((p) => p !== "/m" && pathname.startsWith(p))
    .sort((a, b) => b.length - a.length)[0];
  return hit ? TITLES[hit] : "easymoneysniper";
}

export function MobileLayout() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const { dark, toggle } = useDarkMode();
  const isRoot = ROOT_PATHS.has(pathname);

  // Tag the document so global mobile CSS (safe-area, dvh) can scope itself.
  useEffect(() => {
    document.documentElement.classList.add("m-active");
    return () => document.documentElement.classList.remove("m-active");
  }, []);

  return (
    <div className="m-app flex flex-col bg-background text-foreground">
      {/* top bar */}
      <header className="m-safe-top sticky top-0 z-30 flex h-12 shrink-0 items-center gap-2 border-b bg-card/95 px-2 backdrop-blur">
        {isRoot ? (
          <span className="grid h-8 w-8 place-items-center rounded-full border-2 border-amber-500/80 text-[10px] font-black text-amber-500">
            35
          </span>
        ) : (
          <button
            type="button"
            onClick={() => navigate(-1)}
            aria-label="返回"
            className="grid h-9 w-9 place-items-center rounded-lg text-muted-foreground active:bg-muted"
          >
            <ChevronLeft className="h-5 w-5" />
          </button>
        )}
        <h1 className="min-w-0 flex-1 truncate text-center text-base font-semibold">{titleFor(pathname)}</h1>
        <button
          type="button"
          onClick={toggle}
          aria-label="切换主题"
          className="grid h-9 w-9 place-items-center rounded-lg text-muted-foreground active:bg-muted"
        >
          {dark ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
        </button>
      </header>

      {/* scrollable content */}
      <main className="m-scroll flex-1 overflow-y-auto overflow-x-hidden">
        <Outlet />
      </main>

      {/* bottom tab bar */}
      <nav className="m-safe-bottom sticky bottom-0 z-30 grid shrink-0 grid-cols-5 border-t bg-card/95 backdrop-blur">
        {TABS.map(({ to, label, icon: Icon }) => {
          const active = to === "/m" ? pathname === "/m" : pathname.startsWith(to);
          return (
            <Link
              key={to}
              to={to}
              className={cn(
                "flex min-h-[52px] flex-col items-center justify-center gap-0.5 text-[11px] font-medium",
                active ? "text-primary" : "text-muted-foreground",
              )}
            >
              <Icon className={cn("h-5 w-5", active && "scale-110")} />
              {label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
