import { Suspense, lazy, type ComponentType } from "react";
import { createBrowserRouter, isRouteErrorResponse, useRouteError } from "react-router-dom";
import { Layout } from "@/components/layout/Layout";
import { MobileGate, mobileRoute } from "@/mobile/routes";

const PriorityBoard = lazy(() => import("@/pages/PriorityBoard").then((m) => ({ default: m.PriorityBoard })));
const BatchTasks = lazy(() => import("@/pages/BatchTasks").then((m) => ({ default: m.BatchTasks })));
const Portfolio = lazy(() => import("@/pages/Portfolio").then((m) => ({ default: m.Portfolio })));
const Watchlist = lazy(() => import("@/pages/Watchlist").then((m) => ({ default: m.Watchlist })));
const Home = lazy(() => import("@/pages/Home").then((m) => ({ default: m.Home })));
const PremarketNewsSwipe = lazy(() =>
  import("@/pages/PremarketNewsSwipe").then((m) => ({ default: m.PremarketNewsSwipe })),
);
const PremarketNews = lazy(() => import("@/pages/PremarketNews").then((m) => ({ default: m.PremarketNews })));
const SingleStockOvernight = lazy(() =>
  import("@/pages/SingleStockOvernight").then((m) => ({ default: m.SingleStockOvernight })),
);
const Agent = lazy(() => import("@/pages/Agent").then((m) => ({ default: m.Agent })));
const RunDetail = lazy(() => import("@/pages/RunDetail").then((m) => ({ default: m.RunDetail })));
const Compare = lazy(() => import("@/pages/Compare").then((m) => ({ default: m.Compare })));
const Settings = lazy(() => import("@/pages/Settings").then((m) => ({ default: m.Settings })));
const Correlation = lazy(() => import("@/pages/Correlation").then((m) => ({ default: m.Correlation })));
const AlphaZoo = lazy(() => import("@/pages/AlphaZoo").then((m) => ({ default: m.AlphaZoo })));
const EventRadar = lazy(() => import("@/pages/EventRadar").then((m) => ({ default: m.EventRadar })));
const StockSignals = lazy(() => import("@/pages/StockSignals").then((m) => ({ default: m.StockSignals })));
const LaunchSignal = lazy(() => import("@/pages/LaunchSignal").then((m) => ({ default: m.LaunchSignal })));
const SignalDashboard = lazy(() =>
  import("@/pages/SignalDashboard").then((m) => ({ default: m.SignalDashboard })),
);
const MacroPanicRadar = lazy(() =>
  import("@/pages/MacroPanicRadar").then((m) => ({ default: m.MacroPanicRadar })),
);
const CreatorOpinionRadar = lazy(() =>
  import("@/pages/CreatorOpinionRadar").then((m) => ({ default: m.CreatorOpinionRadar })),
);
const SoxlQuant = lazy(() => import("@/pages/SoxlQuant").then((m) => ({ default: m.SoxlQuant })));

function PageLoader() {
  return (
    <div className="flex h-[60vh] items-center justify-center text-muted-foreground">
      加载中...
    </div>
  );
}

function RouteErrorFallback() {
  const error = useRouteError();
  const message = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : "页面加载失败";
  const chunkFailed = /dynamically imported module|Loading chunk|Importing a module script failed/i.test(message);

  return (
    <div className="flex min-h-[60vh] items-center justify-center px-4">
      <div className="w-full max-w-lg rounded-lg border bg-card p-5 shadow-sm">
        <div className="text-base font-semibold">{chunkFailed ? "前端资源已更新" : "页面加载失败"}</div>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {chunkFailed
            ? "浏览器可能还缓存着旧的页面入口，导致某个按需加载的页面文件没有及时拉到。刷新后会重新获取最新资源。"
            : message}
        </p>
        {!chunkFailed && <p className="mt-2 break-words text-xs text-muted-foreground/80">{message}</p>}
        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground"
            onClick={() => window.location.reload()}
          >
            刷新页面
          </button>
          <button
            type="button"
            className="rounded-md border px-3 py-2 text-sm text-muted-foreground hover:text-foreground"
            onClick={() => {
              window.location.href = "/";
            }}
          >
            回到首页
          </button>
        </div>
      </div>
    </div>
  );
}

function wrap(Component: ComponentType) {
  return (
    <Suspense fallback={<PageLoader />}>
      <Component />
    </Suspense>
  );
}

export const router = createBrowserRouter([
  // Mobile module (Feishu/Lark WebView). Self-contained under /m.
  {
    ...mobileRoute,
    errorElement: <RouteErrorFallback />,
  },
  {
    element: (
      <MobileGate>
        <Layout />
      </MobileGate>
    ),
    errorElement: <RouteErrorFallback />,
    children: [
      { path: "/", element: wrap(PriorityBoard) },
      { path: "/premarket-news", element: wrap(PremarketNewsSwipe) },
      { path: "/premarket-news/list", element: wrap(PremarketNews) },
      { path: "/single-stock-overnight", element: wrap(SingleStockOvernight) },
      { path: "/watchlist", element: wrap(Watchlist) },
      { path: "/creator-opinions", element: wrap(CreatorOpinionRadar) },
      { path: "/portfolio", element: wrap(Portfolio) },
      { path: "/soxl-quant", element: wrap(SoxlQuant) },
      { path: "/batch-tasks", element: wrap(BatchTasks) },
      { path: "/overnight-cockpit", element: wrap(Home) },
      { path: "/agent", element: wrap(Agent) },
      { path: "/settings", element: wrap(Settings) },
      { path: "/runs/:runId", element: wrap(RunDetail) },
      { path: "/compare", element: wrap(Compare) },
      { path: "/correlation", element: wrap(Correlation) },
      { path: "/event-radar", element: wrap(EventRadar) },
      { path: "/stock-signals", element: wrap(StockSignals) },
      { path: "/macro-panic-radar", element: wrap(MacroPanicRadar) },
      { path: "/signal-dashboard", element: wrap(SignalDashboard) },
      { path: "/alpha-zoo", element: wrap(AlphaZoo) },
      { path: "/launch-signal", element: wrap(LaunchSignal) },
      { path: "/alpha-zoo/bench", element: wrap(AlphaZoo) },
      { path: "/alpha-zoo/:alphaId", element: wrap(AlphaZoo) },
    ],
  },
]);
