import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { api } from "@/lib/api";
import { resolveShortcut } from "@/lib/nav-shortcuts";
import { useTheme } from "@/hooks/useTheme";
import { useLiveStream } from "@/hooks/useLiveStream";
import { useDesktopAlerts } from "@/hooks/useDesktopAlerts";
import { StatusBanner } from "@/components/StatusBanner";
import { LiveDot } from "@/components/LiveDot";
import { CommandPalette } from "@/components/CommandPalette";
import { ToastTray } from "@/components/ToastTray";

const NAV = [
  { label: "Overview", path: "/" },
  { label: "Live Feed", path: "/feed" },
  { label: "Replay/Upload", path: "/replay" },
  { label: "Analysis", path: "/map" },
  { label: "Symbols", path: "/symbols" },
  { label: "Benchmark", path: "/benchmark" },
  { label: "Model Controls", path: "/model-controls" },
  { label: "Ops", path: "/ops" },
  { label: "Settings", path: "/settings" },
  { label: "Help", path: "/help" },
];

export function Shell() {
  const { theme, toggle } = useTheme();
  const nav = useNavigate();
  const { data: summary } = useQuery({
    queryKey: ["summary"],
    queryFn: api.summary,
    staleTime: 5_000,
  });
  const { status, lastBeatAt } = useLiveStream();
  // App-wide arrival toasts (works from any tab).
  useDesktopAlerts();

  // Power-user nav: "g o" / "g f" etc.
  useEffect(() => {
    let waiting = false;
    let timer: number | null = null;
    const onKey = (e: KeyboardEvent) => {
      // Skip when typing in inputs
      const target = e.target as HTMLElement | null;
      if (target && (/^(input|textarea|select)$/i.test(target.tagName) || target.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (!waiting) {
        if (e.key === "g") { waiting = true; timer = window.setTimeout(() => { waiting = false; }, 700); }
        return;
      }
      waiting = false;
      if (timer) window.clearTimeout(timer);
      // Canonical key→path lookup. Adding/renaming a chord lives in
      // lib/nav-shortcuts.ts; the test in tests/navShortcuts.test.ts
      // cross-checks the doc surfaces against that registry.
      const route = resolveShortcut(e.key);
      if (route) nav(route);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [nav]);

  return (
    <div className="min-h-full flex flex-col">
      <header className="border-b border-[color:var(--border)] bg-[color:var(--bg-elev)]">
        <div className="mx-auto max-w-screen-2xl px-4 py-3 flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <div className="font-bold tracking-wide text-sm">catchem</div>
            <span className="text-[10px] text-[color:var(--fg-dim)]">analyst workstation</span>
          </div>
          <nav className="flex flex-wrap items-center gap-1 text-xs" aria-label="Primary">
            {NAV.map((n) => (
              <NavLink
                key={n.path}
                to={n.path}
                end={n.path === "/"}
                className={({ isActive }) =>
                  `px-2 py-1 rounded-md hover:bg-[color:var(--bg-elev2)] ${
                    isActive ? "bg-[color:var(--bg-elev2)] text-fg" : "text-[color:var(--fg-dim)]"
                  }`
                }
              >
                {n.label}
              </NavLink>
            ))}
            <a href="/legacy"
               className="px-2 py-1 rounded-md text-[color:var(--fg-muted)] hover:bg-[color:var(--bg-elev2)] hover:text-[color:var(--fg-dim)]"
               aria-label="Legacy dashboard">
              /legacy
            </a>
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <LiveDot status={status} label={lastBeatAt ? status : "idle"} />
            {summary && (
              <span className="text-[10px] text-[color:var(--fg-dim)]">
                {summary.totals.finance_relevant}/{summary.totals.total} relevant
              </span>
            )}
            <button onClick={toggle}
                    className="btn"
                    title="Toggle theme (Cmd+K → theme)"
                    aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>
              {theme === "dark" ? "☼" : "☾"}
            </button>
            <span className="kbd hidden sm:inline" title="Press to open command palette">⌘K</span>
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-screen-2xl px-4 py-4 flex-1">
        {summary && (
          <StatusBanner
            mode={summary.mode}
            diagnosticAllowed={summary.diagnostic_allowed}
            guards={summary.guards}
            useMlStubs={summary.use_ml_stubs}
          />
        )}
        <Outlet />
      </main>

      <footer className="border-t border-[color:var(--border)] mt-6 px-4 py-3 text-[10px] text-[color:var(--fg-muted)] text-center">
        local-first · sidecar to <b>Awareness</b> · NewsImpact stays <em>quarantined / read-only</em>
      </footer>

      <CommandPalette />
      <ToastTray />
    </div>
  );
}
