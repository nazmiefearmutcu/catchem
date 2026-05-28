import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { matchHelp } from "@/lib/page-help";
import { closeAllOverlays, useOverlaySurface } from "@/context/overlayCoordinator";

/**
 * Floating "?" button + right-side context-aware help drawer (v23, task #85).
 *
 * UX intent:
 *   - Bottom-right floating ? button always visible above content (z-30).
 *   - Click opens a 360px-wide right-side drawer with PAGE-SPECIFIC content
 *     pulled from lib/page-help.ts via matchHelp(pathname).
 *   - No focus trap, no backdrop dim — analyst can still scroll & interact
 *     with the page behind the drawer. That's a feature for this workstation.
 *   - Dismiss: X button, Esc key, click on the floating ? button again.
 *
 * Coexistence with the "?" overlay:
 *   - This drawer is BUTTON-triggered only. The literal "?" key still opens
 *     the full ShortcutOverlay (canonical shortcut surface). No double-handle.
 *
 * Accessibility:
 *   - role="dialog" + aria-labelledby on the drawer.
 *   - First focusable element (close button) receives focus on open.
 *   - Drawer is NOT focus-trapped — by design.
 *   - prefers-reduced-motion: transitions collapse to instant via globals.css
 *     (the transition utility classes respect the media query).
 *
 * Wiring: rendered once by Shell.tsx alongside CommandPalette /
 * ShortcutOverlay / OnboardingModal / ToastTray.
 */
export function HelpDrawer() {
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const help = matchHelp(location.pathname);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);
  const openRef = useRef(open);
  useOverlaySurface({
    id: "help-drawer",
    open,
    onClose: () => setOpen(false),
    lockBody: false,
  });

  useEffect(() => {
    openRef.current = open;
  }, [open]);

  // Esc closes the drawer via the global overlay coordinator.
  // We intentionally don't listen for "?" — that key belongs to
  // ShortcutOverlay. Triggering is button-only.
  useEffect(() => {
    if (!open) return;
    lastFocusedRef.current = (document.activeElement as HTMLElement | null) ?? null;
    const t = setTimeout(() => {
      closeButtonRef.current?.focus();
    }, 0);
    return () => {
      clearTimeout(t);
      const prev = lastFocusedRef.current;
      if (prev && typeof prev.focus === "function") {
        try {
          prev.focus();
        } catch {
          /* detached node */
        }
      }
    };
  }, [open]);

  // Close on route change — context drifts, content has changed underneath.
  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  const toggle = useCallback(() => {
    if (openRef.current) {
      setOpen(false);
      return;
    }
    closeAllOverlays();
    setOpen(true);
  }, []);
  const close = useCallback(() => setOpen(false), []);

  return (
    <>
      {/* Floating ? button — bottom-right with 16px margin. The drawer
          slides over from the right, so the button stays in view at all
          times (we shift drawer in via translate-x, button keeps its spot). */}
      <button
        type="button"
        onClick={toggle}
        aria-label={open ? "Close help drawer" : "Open contextual help"}
        aria-expanded={open}
        aria-controls="help-drawer"
        data-testid="help-drawer-trigger"
        title="Context help — quick tips for this page"
        className="fixed bottom-4 right-4 z-30 inline-flex h-9 w-9 items-center justify-center rounded-full bg-accent text-[#0b1018] font-bold text-sm shadow-soft hover:opacity-90 hover:-translate-y-0.5 transition-all"
      >
        ?
      </button>

      {/* Right-side drawer — fixed, no backdrop dim. Slides in via translate-x
          when `open` flips. Pointer-events guarded so it doesn't block clicks
          on the page when collapsed. */}
      <aside
        aria-hidden={!open}
        id="help-drawer"
        role="dialog"
        aria-modal="false"
        aria-labelledby="help-drawer-title"
        // The `inert` attribute removes the subtree from the tab order AND
        // hides it from assistive tech. `aria-hidden` alone hides from SR
        // but interactive children stay focusable, which makes Tab vanish
        // into off-screen content when the drawer is closed.
        // `inert` lands as `inert` on the DOM element (React 19+ supports
        // boolean prop; React 18 needs the attribute via a spread).
        {...(!open ? { inert: "" as unknown as boolean } : {})}
        data-testid="help-drawer"
        data-state={open ? "open" : "closed"}
        className={`fixed inset-y-0 right-0 z-30 w-[360px] border-l border-[color:var(--border)] bg-[color:var(--bg-elev)] shadow-soft transition-transform duration-220 ease-out flex flex-col ${
          open ? "translate-x-0 pointer-events-auto" : "translate-x-full pointer-events-none"
        }`}
        style={{ transitionDuration: "220ms" }}
      >
        <header className="flex items-start justify-between gap-3 border-b border-[color:var(--border-subtle)] px-4 py-3">
          <div>
            <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
              Context · this page
            </div>
            <h2
              id="help-drawer-title"
              className="text-sm font-semibold mt-0.5 tracking-tight"
            >
              Quick help
            </h2>
            <p className="mt-0.5 text-[10px] text-[color:var(--fg-muted)] font-mono">
              {location.pathname}
            </p>
          </div>
          <button
            type="button"
            onClick={close}
            aria-label="Close help drawer"
            ref={closeButtonRef}
            data-testid="help-drawer-close"
            className="btn shrink-0"
          >
            ×
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-4 py-4 grid gap-5">
          {!help ? (
            <p
              className="text-xs text-[color:var(--fg-muted)] italic"
              data-testid="help-drawer-empty"
            >
              No contextual tips for this page yet. Try the full Help page
              (<span className="kbd">g h</span>) or the keyboard shortcut
              overlay (<span className="kbd">?</span>).
            </p>
          ) : (
            <>
              {help.quickTips.length > 0 && (
                <section
                  className="grid gap-2"
                  data-testid="help-drawer-tips"
                >
                  <h3 className="label">Quick tips</h3>
                  <ul className="grid gap-2 text-xs leading-relaxed text-[color:var(--fg-dim)]">
                    {help.quickTips.map((tip, idx) => (
                      <li
                        key={idx}
                        className="flex gap-2 pl-1"
                        data-testid={`help-tip-${idx}`}
                      >
                        <span
                          aria-hidden
                          className="mt-1.5 inline-block h-1 w-1 rounded-full bg-accent shrink-0"
                        />
                        <span>{tip}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {help.questions.length > 0 && (
                <section
                  className="grid gap-2"
                  data-testid="help-drawer-questions"
                >
                  <h3 className="label">Common questions</h3>
                  <dl className="grid gap-3">
                    {help.questions.map((qa, idx) => (
                      <div
                        key={idx}
                        className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2"
                        data-testid={`help-qa-${idx}`}
                      >
                        <dt className="text-xs font-medium text-[color:var(--fg)]">
                          {qa.q}
                        </dt>
                        <dd className="mt-1 text-[11px] leading-relaxed text-[color:var(--fg-dim)]">
                          {qa.a}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </section>
              )}

              {help.shortcuts.length > 0 && (
                <section
                  className="grid gap-2"
                  data-testid="help-drawer-shortcuts"
                >
                  <h3 className="label">Shortcuts on this page</h3>
                  <ul className="grid gap-1.5">
                    {help.shortcuts.map((sc, idx) => (
                      <li
                        key={idx}
                        className="grid grid-cols-[80px_1fr] gap-3 items-center text-xs rounded-md px-2 py-1"
                        data-testid={`help-shortcut-${idx}`}
                      >
                        <span className="kbd">{sc.key}</span>
                        <span className="text-[color:var(--fg-dim)]">
                          {sc.description}
                        </span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          )}
        </div>

        <footer className="border-t border-[color:var(--border-subtle)] px-4 py-2 text-[10px] text-[color:var(--fg-muted)]">
          Press <span className="kbd">Esc</span> to close · <span className="kbd">?</span> for the full shortcuts overlay
        </footer>
      </aside>
    </>
  );
}

export default HelpDrawer;
