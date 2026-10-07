import { Suspense, lazy, type ComponentType, type ReactElement } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { MobileLayout } from "./MobileLayout";
import { LoadingState } from "./components/ui";
import { detectMobileDevice } from "./useIsMobile";

const MobileBoard = lazy(() => import("./pages/MobileBoard").then((m) => ({ default: m.MobileBoard })));
const MobileNews = lazy(() => import("./pages/MobileNews").then((m) => ({ default: m.MobileNews })));
const MobileStock = lazy(() => import("./pages/MobileStock").then((m) => ({ default: m.MobileStock })));
const MobilePortfolio = lazy(() => import("./pages/MobilePortfolio").then((m) => ({ default: m.MobilePortfolio })));
const MobileWatchlist = lazy(() => import("./pages/MobileWatchlist").then((m) => ({ default: m.MobileWatchlist })));
const MobileMore = lazy(() => import("./pages/MobileMore").then((m) => ({ default: m.MobileMore })));

function wrap(Component: ComponentType): ReactElement {
  return (
    <Suspense fallback={<LoadingState />}>
      <Component />
    </Suspense>
  );
}

/**
 * Wraps the desktop layout: if this is a phone-sized Feishu/Lark WebView, bounce
 * the user to the matching `/m` screen (preserving the query string). Desktop
 * browsers fall straight through, so the existing desktop app is untouched.
 */
const DESKTOP_TO_MOBILE: Record<string, string> = {
  "/": "/m",
  "/premarket-news": "/m/news",
  "/premarket-news/list": "/m/news",
  "/single-stock-overnight": "/m/stock",
  "/watchlist": "/m/watchlist",
  "/portfolio": "/m/portfolio",
  "/settings": "/m/more",
};

export function MobileGate({ children }: { children: ReactElement }) {
  const { pathname, search } = useLocation();
  if (pathname.startsWith("/m")) return children;
  if (!detectMobileDevice()) return children;
  // Only the core money-path pages auto-redirect to their native mobile screen.
  // Advanced/research routes fall through to the desktop layout so they stay
  // reachable on a phone (a labelled fallback in the "更多" hub).
  const target = pathname === "/" ? "/m" : DESKTOP_TO_MOBILE[pathname];
  if (target) return <Navigate to={`${target}${search}`} replace />;
  return children;
}

export const mobileRoute = {
  path: "/m",
  element: <MobileLayout />,
  children: [
    { index: true, element: wrap(MobileBoard) },
    { path: "news", element: wrap(MobileNews) },
    { path: "stock", element: wrap(MobileStock) },
    { path: "portfolio", element: wrap(MobilePortfolio) },
    { path: "watchlist", element: wrap(MobileWatchlist) },
    { path: "more", element: wrap(MobileMore) },
  ],
};
