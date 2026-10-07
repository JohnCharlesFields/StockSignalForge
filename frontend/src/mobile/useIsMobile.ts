import { useEffect, useState } from "react";

// Mobile module entry path. Feishu mobile homepage should point here (see README note).
export const MOBILE_BASE = "/m";

const FORCE_DESKTOP_KEY = "vibe_force_desktop";
const FORCE_MOBILE_KEY = "vibe_force_mobile";

/**
 * Heuristic for "this is a phone-sized WebView" (Feishu/Lark mobile, iOS/Android).
 * Pure read of UA + touch + viewport; no network, safe during render.
 */
export function detectMobileDevice(): boolean {
  if (typeof window === "undefined") return false;
  // Force-desktop is session-scoped so it can never permanently trap a phone
  // user — reopening the WebView returns them to the mobile layout.
  if (window.sessionStorage.getItem(FORCE_DESKTOP_KEY) === "1") return false;
  if (window.localStorage.getItem(FORCE_MOBILE_KEY) === "1") return true;
  const ua = navigator.userAgent || "";
  const uaMobile = /Mobi|Android|iPhone|iPod|Lark|Feishu/i.test(ua);
  const iPadOS = /Macintosh/i.test(ua) && navigator.maxTouchPoints > 1;
  const coarse = typeof window.matchMedia === "function" && window.matchMedia("(pointer: coarse)").matches;
  const narrow = window.innerWidth > 0 && window.innerWidth <= 820;
  // Treat as mobile when the UA says so, or a touch device on a narrow screen.
  return uaMobile || iPadOS || (coarse && narrow);
}

export function setForceDesktop(on: boolean): void {
  if (typeof window === "undefined") return;
  if (on) {
    window.sessionStorage.setItem(FORCE_DESKTOP_KEY, "1");
    window.localStorage.removeItem(FORCE_MOBILE_KEY);
  } else {
    window.sessionStorage.removeItem(FORCE_DESKTOP_KEY);
  }
}

export function setForceMobile(on: boolean): void {
  if (typeof window === "undefined") return;
  if (on) {
    window.localStorage.setItem(FORCE_MOBILE_KEY, "1");
    window.localStorage.removeItem(FORCE_DESKTOP_KEY);
  } else {
    window.localStorage.removeItem(FORCE_MOBILE_KEY);
  }
}

/** Reactive variant for components that should re-render on rotation/resize. */
export function useIsMobile(): boolean {
  const [mobile, setMobile] = useState(detectMobileDevice);
  useEffect(() => {
    const onResize = () => setMobile(detectMobileDevice());
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("orientationchange", onResize);
    };
  }, []);
  return mobile;
}
