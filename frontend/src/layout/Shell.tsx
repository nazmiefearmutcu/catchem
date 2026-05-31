import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { prefetchRoute } from "@/lib/route-prefetch";
import { useQuery } from "@tanstack/react-query";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { resolveShortcut } from "@/lib/nav-shortcuts";
import { t, useLang } from "@/lib/i18n";
import { useTheme } from "@/hooks/useTheme";
import { useAccent } from "@/hooks/useAccent";
import { useLiveStream } from "@/hooks/useLiveStream";
import {
  useDesktopAlerts,
  useUnreadNotificationCount,
} from "@/hooks/useDesktopAlerts";
import { useTauriMenu } from "@/hooks/useTauriMenu";
import {
  closeAllOverlays,
  hasOpenOverlays,
  useOverlaySurface,
} from "@/context/overlayCoordinator";
import { StatusBanner } from "@/components/StatusBanner";
import { SidecarBanner } from "@/components/SidecarBanner";
import { LiveDot } from "@/components/LiveDot";
import { CommandPalette, OPEN_COMMAND_PALETTE_EVENT } from "@/components/CommandPalette";
import { OPEN_SEARCH_PALETTE_EVENT, SearchPalette } from "@/components/SearchPalette";
import { ShortcutOverlay } from "@/components/ShortcutOverlay";
import { OnboardingModal } from "@/components/OnboardingModal";
import { ToastTray } from "@/components/ToastTray";
import { NotificationCenter } from "@/components/NotificationCenter";
import { HelpDrawer } from "@/components/HelpDrawer";
import { Icon } from "@/components/Icon";
import { Skeleton } from "@/components/Skeleton";
import { RouteErrorBoundary } from "@/components/RouteErrorBoundary";

// Routes are static; labels are read from the i18n dictionary at render
// time so a language switch re-paints the nav without remounting Shell.
const NAV: { key: string; path: string }[] = [
  { key: "nav.overview", path: "/" },
  { key: "nav.feed", path: "/feed" },
  { key: "nav.replay", path: "/replay" },
  { key: "nav.analysis", path: "/map" },
  { key: "nav.symbols", path: "/symbols" },
  { key: "nav.portfolio", path: "/portfolio" },
  { key: "nav.tags", path: "/tags" },
  { key: "nav.benchmark", path: "/benchmark" },
  { key: "nav.backtest", path: "/backtest" },
  { key: "nav.reviews", path: "/reviews" },
  { key: "nav.scan", path: "/scan" },
  { key: "nav.model_controls", path: "/model-controls" },
  { key: "nav.ops", path: "/ops" },
  { key: "nav.logs", path: "/logs" },
  { key: "nav.sources", path: "/sources" },
  { key: "nav.settings", path: "/settings" },
  { key: "nav.help", path: "/help" },
];

const MOBILE_NAV_PANEL_FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Shell() {
  const { theme, toggle } = useTheme();
  // useAccent injects the `--accent` CSS variable override on mount and
  // any time the user picks a different preset/custom hex on SettingsPage.
  // Called here (one node up from <Outlet/>) so the override exists before
  // the first paint of any route component. We don't need the returned
  // controls — SettingsPage opens its own consumer.
  useAccent();
  // Native macOS menu bridge — Rust dispatches a `catchem:menu` DOM event
  // and this hook routes the action (export DB, toggle theme, open
  // shortcut overlay, etc.). Mounted in Shell so a single subscription
  // covers every route. See `desktop/catchem/src-tauri/src/menu.rs`.
  useTauriMenu();
  // Subscribe to locale changes so the nav labels re-render when the
  // user flips between English and Turkish on the Settings page.
  useLang();
  const nav = useNavigate();
  const location = useLocation();
  const { data: summary } = useQuery({
    queryKey: ["summary"],
    queryFn: api.summary,
    staleTime: 5_000,
  });
  const { status, lastBeatAt, stalenessSeconds } = useLiveStream();
  const liveDotStatus = status === "open" && !lastBeatAt ? "idle" : status;
  // App-wide arrival toasts (works from any tab).
  useDesktopAlerts();

  // Notification Center (v37 task #142). Modal lives in Shell so the bell
  // icon, the `g n` chord, and the modal share one piece of open-state.
  const [notifOpen, setNotifOpen] = useState(false);
  const unreadCount = useUnreadNotificationCount();
  const toggleNotif = useCallback(() => {
    if (notifOpen) {
      setNotifOpen(false);
      return;
    }
    // If any modal/overlay is open, close it first so notification
    // center becomes the active destination instead of being blocked.
    closeAllOverlays();
    setNotifOpen(true);
  }, [notifOpen]);
  const closeNotif = useCallback(() => setNotifOpen(false), []);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const toggleMobileNav = useCallback(() => setMobileNavOpen((v) => !v), []);
  const closeMobileNav = useCallback(() => setMobileNavOpen(false), []);

  useOverlaySurface({
    id: "mobile-nav-drawer",
    open: mobileNavOpen,
    onClose: closeMobileNav,
    lockBody: true,
  });

  const openCommandPalette = useCallback(() => {
    window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT));
  }, []);
  const openSearchPalette = useCallback(() => {
    window.dispatchEvent(new Event(OPEN_SEARCH_PALETTE_EVENT));
  }, []);
  const mobileNavPanelRef = useRef<HTMLDivElement | null>(null);
  const mobileNavTriggerRef = useRef<HTMLButtonElement | null>(null);
  const trapMobileNavFocus = useCallback((event: KeyboardEvent) => {
    if (event.key !== "Tab") return;
    const panel = mobileNavPanelRef.current;
    if (!panel) return;
    const focusables = Array.from(
      panel.querySelectorAll<HTMLElement>(MOBILE_NAV_PANEL_FOCUSABLE),
    ).filter((el) => !el.hasAttribute("disabled") && el.tabIndex >= 0);
    if (focusables.length === 0) {
      event.preventDefault();
      panel.focus();
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement as HTMLElement | null;
    if (event.shiftKey) {
      if (!active || active === first) {
        event.preventDefault();
        last.focus();
      }
      return;
    }
    if (active === last) {
      event.preventDefault();
      first.focus();
    }
  }, []);

  // Power-user nav: "g o" / "g f" etc.
  useEffect(() => {
    let waiting = false;
    let waitingAfterOverlayDismiss = false;
    let timer: number | null = null;
    const isTypingContext = (target: EventTarget | null) => {
      if (!target) return false;
      if (target instanceof HTMLElement) {
        return /^(input|textarea|select)$/i.test(target.tagName) || target.isContentEditable;
      }
      return false;
    };

    const clearPending = () => {
      if (timer) {
        window.clearTimeout(timer);
      }
      timer = null;
      waiting = false;
      waitingAfterOverlayDismiss = false;
    };

    const armPending = (afterOverlayDismiss: boolean) => {
      clearPending();
      waiting = true;
      waitingAfterOverlayDismiss = afterOverlayDismiss;
      timer = window.setTimeout(() => {
        waiting = false;
        waitingAfterOverlayDismiss = false;
      }, 700);
    };

    const onKey = (e: KeyboardEvent) => {
      // Skip when typing in inputs
      if (isTypingContext(e.target)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      const key = e.key.toLowerCase();

      if (hasOpenOverlays()) {
        if (waiting) {
          clearPending();
        }
        if (key === "g") {
          closeAllOverlays();
          armPending(true);
        }
        return;
      }

      if (waitingAfterOverlayDismiss && key === "n") {
        clearPending();
        toggleNotif();
        return;
      }

      // While any overlay is open, preserve focus inside that surface and
      // avoid driving route navigation from the background.
      if (!waiting) {
        if (key === "g") {
          armPending(false);
        }
        return;
      }

      // Capture the dismissal flag before clearPending() resets it — the
      // second key after a `g`-dismissal (anything other than the `g n`
      // case handled above) must be swallowed, not routed.
      const afterOverlayDismiss = waitingAfterOverlayDismiss;
      clearPending();

      if (afterOverlayDismiss) {
        return;
      }

      // `g n` is special-cased here so adding it to NAV_SHORTCUTS — which
      // is a registry of *routed* destinations — doesn't break the
      // navShortcuts test that asserts every chord resolves to a path.
      // The Notification Center is a modal, not a route, so it lives
      // outside the registry but shares the same `g <key>` ergonomics.
      if (key === "n") {
        toggleNotif();
        return;
      }
      // Canonical key→path lookup. Adding/renaming a chord lives in
      // lib/nav-shortcuts.ts; the test in tests/navShortcuts.test.ts
      // cross-checks the doc surfaces against that registry.
      const route = resolveShortcut(key);
      if (route) nav(route);
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (timer != null) window.clearTimeout(timer);
    };
  }, [nav, toggleNotif]);

  useEffect(() => {
    closeMobileNav();
  }, [location.pathname, closeMobileNav]);

  useEffect(() => {
    if (!mobileNavOpen) return;
    const trigger = mobileNavTriggerRef.current;
    const prevFocused = (document.activeElement as HTMLElement | null) ?? null;
    const t = setTimeout(() => mobileNavPanelRef.current?.focus(), 0);
    document.addEventListener("keydown", trapMobileNavFocus);
    return () => {
      document.removeEventListener("keydown", trapMobileNavFocus);
      clearTimeout(t);
      if (prevFocused && typeof prevFocused.focus === "function") {
        try {
          prevFocused.focus();
        } catch {
          trigger?.focus();
        }
      } else {
        trigger?.focus();
      }
    };
  }, [mobileNavOpen, trapMobileNavFocus]);

  return (
    <div className="min-h-full flex flex-col">
      {/* Skip-to-content link — first focusable element so keyboard users can
          bypass the nav. Visually hidden until focused (see globals.css). */}
      <a href="#main" className="skip-link">Skip to main content</a>
      {/* Sticky header with subtle backdrop blur — sits above page content
          on scroll so the analyst can navigate without losing context. */}
      <header className="sticky top-0 z-30 border-b border-[color:var(--border)] bg-[color:var(--bg-elev)]/85 backdrop-blur supports-[backdrop-filter]:bg-[color:var(--bg-elev)]/70">
        <div className="mx-auto max-w-screen-2xl px-4 py-3 flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            {/* Accent monogram so the wordmark reads as a brand, not raw text. */}
            <span
              aria-hidden
              className="inline-flex h-5 w-5 items-center justify-center rounded-md bg-accent text-[#0b1018] font-bold text-[10px] tracking-tighter"
            >
              cm
            </span>
            <div className="font-bold tracking-wide text-sm">catchem</div>
            <button
              ref={mobileNavTriggerRef}
              type="button"
              onClick={toggleMobileNav}
              className="btn md:hidden"
              aria-label={mobileNavOpen ? "Close navigation" : "Open navigation"}
              aria-expanded={mobileNavOpen}
              aria-controls="mobile-nav-drawer"
            >
              <span aria-hidden>☰</span>
            </button>
            <span className="text-[10px] text-[color:var(--fg-dim)] hidden sm:inline">analyst workstation</span>
          </div>
          {/* Active route gets an accent-underline strip — sleeker than a
              pill background and reads as a real "tab" group. */}
          <nav className="hidden md:flex flex-wrap items-center gap-0.5 text-xs" aria-label="Primary">
            {NAV.map((n) => (
              <NavLink
                key={n.path}
                to={n.path}
                end={n.path === "/"}
                onMouseEnter={() => prefetchRoute(n.path)}
                onFocus={() => prefetchRoute(n.path)}
                className={({ isActive }) =>
                  `relative px-2.5 py-1.5 transition-colors hover:text-[color:var(--fg)] ${
                    isActive ? "text-[color:var(--fg)] font-semibold" : "text-[color:var(--fg-dim)]"
                  } after:content-[''] after:absolute after:left-2 after:right-2 after:-bottom-[10px] after:h-px ${
                    isActive ? "after:bg-accent" : "after:bg-transparent"
                  }`
                }
              >
                {t(n.key)}
              </NavLink>
            ))}
            <a
              href="/legacy"
              className="ml-1 px-2 py-1 rounded-md text-[color:var(--fg-muted)] hover:bg-[color:var(--bg-elev2)] hover:text-[color:var(--fg-dim)]"
              aria-label="Legacy dashboard"
            >
              /legacy
            </a>
          </nav>
          <div className="ml-auto flex items-center gap-2 sm:gap-3">
            <LiveDot status={liveDotStatus} stalenessSeconds={stalenessSeconds} />
            {summary && (
              <span className="hidden sm:inline text-[10px] text-[color:var(--fg-dim)] tabular-nums">
                <span className="text-good">{summary.totals.finance_relevant}</span>
                /{summary.totals.total} relevant
              </span>
            )}
            {/* Notification bell — opens history modal. Badge appears only when
                there's an unread count so it doesn't clutter the header at rest. */}
            <button
              type="button"
              onClick={toggleNotif}
              aria-label={
                unreadCount > 0
                  ? `Open notifications (${unreadCount} unread)`
                  : "Open notifications"
              }
              aria-expanded={notifOpen}
              data-testid="notification-bell"
              data-unread={unreadCount > 0 ? "1" : "0"}
              title="Notifications (g n)"
              className="btn relative"
            >
              <Icon name={unreadCount > 0 ? "bell" : "bellOff"} size={14} />
              {unreadCount > 0 && (
                <span
                  data-testid="notification-bell-badge"
                  className="absolute -top-1 -right-1 inline-flex h-4 min-w-[1rem] items-center justify-center rounded-full bg-accent px-1 text-[9px] font-bold text-[#0b1018] tabular-nums leading-none shadow-soft"
                  aria-hidden
                >
                  {unreadCount > 99 ? "99+" : unreadCount}
                </span>
              )}
            </button>
            <button onClick={toggle}
                    className="btn"
                    title="Toggle theme (Cmd+K → theme)"
                    aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>
              {theme === "dark" ? "☼" : "☾"}
            </button>
            <button
              type="button"
              onClick={openCommandPalette}
              className="btn hidden sm:inline-flex"
              title="Open command palette (nav + actions)"
              aria-label="Open command palette"
            >
              <span className="kbd">⌘K</span>
            </button>
            <button
              type="button"
              onClick={openSearchPalette}
              className="btn hidden sm:inline-flex"
              title="Open search palette (records, symbols, clusters)"
              aria-label="Open search palette"
            >
              <span className="kbd">⌘P</span>
            </button>
          </div>
        </div>
      </header>

      {mobileNavOpen && (
        <div className="md:hidden fixed inset-0 z-40" onClick={closeMobileNav}>
          <div className="absolute inset-0 bg-black/50" />
          <div
              className="absolute left-0 right-0 top-16 border-b border-[color:var(--border)] bg-[color:var(--bg-elev)] shadow-soft"
              onClick={(e) => e.stopPropagation()}
            >
            <div
              id="mobile-nav-drawer"
              ref={mobileNavPanelRef}
              role="dialog"
              aria-modal="true"
              aria-label="Navigation menu"
              tabIndex={-1}
              className="max-h-[60vh] overflow-auto py-2"
            >
              <div className="mx-auto max-w-screen-2xl px-3">
                <div className="flex items-center justify-between px-1 pb-1">
                  <div className="text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)]">
                    Navigation
                  </div>
                  <button
                    type="button"
                    onClick={closeMobileNav}
                    className="btn px-2 py-1 text-[10px] leading-none"
                    aria-label="Close navigation panel"
                  >
                    close
                  </button>
                </div>
                {NAV.map((n) => (
                  <NavLink
                    key={`mobile-${n.path}`}
                    to={n.path}
                    end={n.path === "/"}
                    onMouseEnter={() => prefetchRoute(n.path)}
                    onFocus={() => prefetchRoute(n.path)}
                    onClick={closeMobileNav}
                    className={({ isActive }) =>
                      `block rounded-md px-3 py-2 text-sm transition-colors ${
                        isActive
                          ? "bg-[color:var(--bg-elev2)] text-[color:var(--fg)] font-semibold"
                          : "text-[color:var(--fg-dim)] hover:bg-[color:var(--bg-elev2)]"
                      }`
                    }
                  >
                    {t(n.key)}
                  </NavLink>
                ))}
                <a
                  href="/legacy"
                  onClick={closeMobileNav}
                  className="block rounded-md px-3 py-2 mt-1 text-[color:var(--fg-dim)] hover:bg-[color:var(--bg-elev2)] hover:text-[color:var(--fg-dim)]"
                  aria-label="Legacy dashboard"
                >
                  /legacy
                </a>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Unified sidecar-health banner. Self-hides when /healthz returns
          200 so we never show a spacer; on outage it stickies just under
          the header and replaces a page full of per-query error cards
          with a single, honest "Reconnecting…" / "Sidecar is offline"
          message. See useSidecarHealth.ts for the 15s/3s cadence. */}
      <SidecarBanner />

      <main id="main" tabIndex={-1} className="mx-auto w-full max-w-screen-2xl px-4 py-4 flex-1 focus:outline-none">
        {summary && (
          <StatusBanner
            mode={summary.mode}
            diagnosticAllowed={summary.diagnostic_allowed}
            guards={summary.guards}
            useMlStubs={summary.use_ml_stubs}
          />
        )}
        <div key={location.pathname} className="animate-page-enter">
          {/* Suspense lives INSIDE the page-enter wrapper so the lazy-route
              fallback skeleton inherits the same fade-in animation as the
              loaded page — no jarring snap when chunks resolve mid-fade.
              RouteErrorBoundary is keyed by pathname so navigation to a new
              route remounts (and resets) the boundary; otherwise the error
              fallback would persist across route changes. */}
          <RouteErrorBoundary key={location.pathname}>
            <Suspense fallback={<Skeleton className="h-72" />}>
              <Outlet />
            </Suspense>
          </RouteErrorBoundary>
        </div>
      </main>

      <footer className="border-t border-[color:var(--border)] mt-6 px-4 py-3 text-[10px] text-[color:var(--fg-muted)] text-center">
        local-first · sidecar to <b>Awareness</b> · NewsImpact stays <em>quarantined / read-only</em>
      </footer>

      <CommandPalette />
      <SearchPalette />
      <ShortcutOverlay />
      <OnboardingModal />
      <ToastTray />
      <NotificationCenter open={notifOpen} onClose={closeNotif} />
      <HelpDrawer />
    </div>
  );
}
