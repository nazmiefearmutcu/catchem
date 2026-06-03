import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { NAV_SHORTCUTS, chordLabel } from "@/lib/nav-shortcuts";
import { t, useLang } from "@/lib/i18n";
import { closeAllOverlays, useOverlaySurface } from "@/context/overlayCoordinator";
import { useLocation } from "react-router-dom";

/**
 * Global "?" keyboard-shortcut overlay (v21 onboarding work, task #77).
 *
 * Press "?" anywhere outside an input/textarea/contentEditable to open a
 * full-screen modal listing every keyboard shortcut in the cockpit,
 * grouped into Navigation / Actions / Modals sections. Dismiss via Esc,
 * a backdrop click, or the top-right X button.
 *
 * The Navigation section reads from the canonical NAV_SHORTCUTS registry
 * (lib/nav-shortcuts.ts) so the docs in this overlay never drift from
 * the actual handler in Shell.tsx — same single-source pattern Round 7
 * adopted for SettingsPage / HelpPage / CommandPalette.
 *
 * Accessibility:
 *   - role="dialog" + aria-modal + aria-labelledby for SR announcement.
 *   - First focusable child (the X close button) gets focus on open.
 *   - Esc closes; clicking the dim backdrop closes; clicking inside
 *     stops propagation so a misclick on a kbd row doesn't dismiss.
 *   - prefers-reduced-motion: reduce skips the slide-up + fade-in
 *     (handled via globals.css `.animate-modal-enter` + media query).
 *
 * Wiring: rendered once by Shell.tsx alongside CommandPalette + ToastTray.
 * The "?" key isn't claimed by either the chord handler (which only
 * watches for "g <key>") or the command palette (which only watches
 * Cmd/Ctrl+K + Escape), so there's no double-handle.
 */

interface ShortcutRow {
  keys: string;
  label: string;
}

interface ShortcutGroup {
  title: string;
  eyebrow: string;
  rows: ShortcutRow[];
}

export function ShortcutOverlay() {
  const [open, setOpen] = useState(false);
  // Subscribe to locale changes so section labels re-render on swap.
  const lang = useLang();
  const cardRef = useRef<HTMLDivElement | null>(null);
  const firstFocusRef = useRef<HTMLButtonElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);
  const openRef = useRef(open);
  const location = useLocation();

  useOverlaySurface({
    id: "shortcut-overlay",
    open,
    onClose: () => setOpen(false),
    lockBody: true,
  });

  // Build groups from the canonical registry so the docs can't drift.
  // Recompute when `lang` changes so the section titles flip with the
  // user's locale (the chord labels + per-row `label` are still sourced
  // from the canonical registry — translating those would mean moving
  // them out of nav-shortcuts.ts, which is a bigger lift than v31 wants).
  const groups = useMemo<ShortcutGroup[]>(() => {
    return [
      {
        title: t("shortcuts.section.nav"),
        eyebrow: "go anywhere · g + key chord",
        rows: NAV_SHORTCUTS.map((s) => ({
          keys: chordLabel(s),
          label: s.label,
        })),
      },
      {
        title: t("shortcuts.section.actions"),
        eyebrow: "open palette · dismiss · this overlay",
        rows: [
          { keys: "⌘K  /  Ctrl+K", label: "Open command palette" },
          { keys: "Esc", label: "Close drawer / palette / modal" },
          { keys: "?", label: "This shortcuts overlay" },
        ],
      },
      {
        title: t("shortcuts.section.modals"),
        eyebrow: "reserved for future modals",
        // Leave the section visible but indicate intentional emptiness so
        // power users know more shortcut surfaces are coming.
        rows: [],
      },
    ];
    // `lang` is the change signal — depending on it re-builds the array
    // with fresh translations whenever the locale flips.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lang]);

  useEffect(() => {
    openRef.current = open;
  }, [open]);

  // Open on "?" (shift+/ on US, but we listen for the literal char so
  // any layout that produces "?" works). Close on Escape.
  //
  // Also listen for the global `catchem:open-shortcut-overlay` event so
  // the CommandPalette's "Show keyboard shortcuts" action can open us
  // imperatively without owning our state.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Skip when typing in inputs, textareas, selects, or contentEditable.
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (/^(input|textarea|select)$/i.test(target.tagName) ||
          target.isContentEditable)
      ) {
        return;
      }
      // Don't fight Cmd/Ctrl-modified keys (those belong to the palette
      // and the browser).
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "?") {
        e.preventDefault();
        if (openRef.current) {
          setOpen(false);
          return;
        }
        closeAllOverlays();
        // Stash whatever had focus before so we can restore it on close.
        lastFocusedRef.current =
          (document.activeElement as HTMLElement | null) ?? null;
        setOpen(true);
        return;
      }
    };
    const onOpenEvent = () => {
      if (openRef.current) return;
      closeAllOverlays();
      lastFocusedRef.current =
        (document.activeElement as HTMLElement | null) ?? null;
      setOpen(true);
    };
    document.addEventListener("keydown", onKey);
    window.addEventListener("catchem:open-shortcut-overlay", onOpenEvent);
    return () => {
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("catchem:open-shortcut-overlay", onOpenEvent);
    };
  }, []);

  // Keep overlay scope bound to current route.
  useEffect(() => {
    if (open) setOpen(false);
  }, [location.pathname]);

  // Basic focus trap: move focus to the close button when opened, then
  // restore the previously-focused element when closed.
  useEffect(() => {
    if (open) {
      const t = setTimeout(() => {
        (firstFocusRef.current ?? closeButtonRef.current)?.focus();
      }, 0);
      return () => {
        clearTimeout(t);
      };
    }
    // Restore focus to the previously-focused element on close.
    const prev = lastFocusedRef.current;
    if (prev && typeof prev.focus === "function") {
      try {
        prev.focus();
      } catch {
        /* element may be gone — ignore */
      }
    }
    return;
  }, [open]);

  const close = useCallback(() => setOpen(false), []);

  if (!open) return null;

  return (
    <div
      // Dim backdrop. The whole layer is the click target for dismissal;
      // the card inside stops propagation so clicks on rows / X don't
      // double-fire.
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 pt-16 px-4 overflow-y-auto"
      onClick={close}
      data-testid="shortcut-overlay-backdrop"
    >
      <div
        ref={cardRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="shortcut-overlay-title"
        aria-describedby="shortcut-overlay-instructions"
        data-testid="shortcut-overlay-card"
        className="relative w-full max-w-2xl rounded-xl border border-accent/40 hero-gradient shadow-soft animate-modal-enter overflow-hidden my-8"
        onClick={(e) => e.stopPropagation()}
      >
        <div id="shortcut-overlay-instructions" className="sr-only">
          Keyboard shortcuts list. Press Escape to close.
        </div>
        {/* Soft accent blob to echo the hero aesthetic used elsewhere
            (SettingsPage hero / quant heroes / Welcome page). */}
        <div
          aria-hidden
          className="pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full bg-accent/20 blur-3xl"
        />
        <div className="relative p-6">
          <header className="flex items-start justify-between gap-3 mb-5">
            <div>
              <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
                Keyboard · all shortcuts
              </div>
              <h2
                id="shortcut-overlay-title"
                className="text-lg font-semibold mt-0.5 tracking-tight"
              >
                {t("shortcuts.title")}
              </h2>
              <p className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                Press <span className="kbd">?</span> any time to open this overlay ·{" "}
                <span className="kbd">Esc</span> to dismiss
              </p>
            </div>
            <button
              ref={closeButtonRef}
              type="button"
              onClick={close}
              aria-label="Close shortcuts"
              className="btn shrink-0 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded"
              data-testid="shortcut-overlay-close"
            >
              ×
            </button>
          </header>

          <div className="grid gap-5">
            {groups.map((group, sectionIndex) => (
              <section
                key={group.title}
                className="grid gap-2"
                data-testid={`shortcut-group-${group.title.toLowerCase()}`}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <h3 className="label">{group.title}</h3>
                  <span className="text-[10px] text-[color:var(--fg-muted)] italic">
                    {group.eyebrow}
                  </span>
                </div>
                {group.rows.length === 0 ? (
                  <p className="text-[11px] text-[color:var(--fg-muted)] italic px-1 py-1.5">
                    No modal shortcuts yet — this section will fill in as
                    new dialogs land.
                  </p>
                ) : (
                  <ul className="grid gap-1.5">
                    {group.rows.map((row, idx) => (
                      <li
                        key={`${group.title}-${row.keys}`}
                        className="grid grid-cols-[140px_1fr] gap-3 items-center text-sm rounded-md px-2 py-1.5 border border-transparent hover:border-[color:var(--border-subtle)] hover:bg-[color:var(--bg-elev2)]/40 transition-colors"
                      >
                        {/* First Navigation row hosts the focus anchor so
                            keyboard users can start scrolling immediately
                            after open without re-tabbing through close. */}
                        {idx === 0 && sectionIndex === 0 ? (
                          <button
                            ref={firstFocusRef}
                            type="button"
                            tabIndex={-1}
                            className="kbd text-left"
                            aria-hidden="true"
                          >
                            {row.keys}
                          </button>
                        ) : (
                          <span className="kbd">{row.keys}</span>
                        )}
                        <span className="text-[color:var(--fg-dim)]">
                          {row.label}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            ))}
          </div>

          <footer className="mt-5 pt-3 border-t border-[color:var(--border-subtle)] text-[10px] text-[color:var(--fg-muted)]">
            Chord shortcuts: press the first key, release, then the second
            within ~1.5s. Inputs and editable fields don't trigger chords.
          </footer>
        </div>
      </div>
    </div>
  );
}

export default ShortcutOverlay;
