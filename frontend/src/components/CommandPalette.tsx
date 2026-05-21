import { useEffect, useState } from "react";
import { Command } from "cmdk";
import { useNavigate } from "react-router-dom";
import { useTheme } from "@/hooks/useTheme";
import { NAV_SHORTCUTS, chordLabel } from "@/lib/nav-shortcuts";

/**
 * Palette entries. Routed entries are mirrored verbatim from the
 * canonical NAV_SHORTCUTS table; the legacy dashboard is appended as a
 * non-chord shortcut.
 */
export const NAV: { label: string; path: string; kbd?: string }[] = [
  ...NAV_SHORTCUTS.map((s) => ({ label: s.label, path: s.path, kbd: chordLabel(s) })),
  { label: "Legacy Dashboard", path: "/legacy" },
];

export const SYMBOL_MENTION_PLACEHOLDER = "Type a page, symbol mention, or command...";
export const symbolMentionEmptyText = (raw: string) =>
  `No matches - press Enter to find symbol mentions for ${raw.trim().toUpperCase()}`;
export const symbolMentionActionLabel = (raw: string) =>
  `Find symbol mentions "${raw.trim().toUpperCase() || "AAPL"}"`;

/**
 * Cmd/Ctrl+K opens the palette. Includes navigation, theme toggle, and
 * symbol/reason mention quick-jump (typing a known symbol like AAPL routes you
 * to matching news records, not to a quote subsystem).
 */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const nav = useNavigate();
  const { theme, toggle } = useTheme();

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      }
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  const goSymbol = () => {
    const sym = input.trim().replace(/^\$/, "").toUpperCase();
    if (!sym) return;
    setOpen(false);
    nav(`/symbols/${encodeURIComponent(sym)}`);
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 pt-24 px-4"
      onClick={() => setOpen(false)}
    >
      <Command
        label="Command palette"
        className="w-full max-w-xl rounded-lg border border-[color:var(--border)] bg-[color:var(--bg-elev)] shadow-soft"
        onClick={(e) => e.stopPropagation()}
      >
        <Command.Input
          autoFocus
          value={input}
          onValueChange={setInput}
          placeholder={SYMBOL_MENTION_PLACEHOLDER}
          className="w-full bg-transparent px-4 py-3 text-sm outline-none border-b border-[color:var(--border)]"
        />
        <Command.List className="max-h-96 overflow-auto p-1">
          <Command.Empty className="px-4 py-3 text-xs text-[color:var(--fg-dim)]">
            No matches - press <span className="kbd">Enter</span> to find symbol mentions for <b className="text-good">{input.trim().toUpperCase()}</b>
          </Command.Empty>
          <Command.Group heading="Pages" className="px-2 py-1 text-[10px] uppercase text-[color:var(--fg-dim)]">
            {NAV.map((it) => (
              <Command.Item
                key={it.path}
                value={`page ${it.label}`}
                onSelect={() => {
                  setOpen(false);
                  if (it.path.startsWith("/legacy")) window.location.href = it.path;
                  else nav(it.path);
                }}
                className="flex items-center justify-between rounded px-3 py-2 text-sm aria-selected:bg-[color:var(--bg-elev2)] cursor-pointer"
              >
                <span>{it.label}</span>
                {it.kbd && <span className="kbd">{it.kbd}</span>}
              </Command.Item>
            ))}
          </Command.Group>
          <Command.Group heading="Actions" className="px-2 py-1 text-[10px] uppercase text-[color:var(--fg-dim)]">
            <Command.Item
              value="theme toggle"
              onSelect={() => {
                toggle();
                setOpen(false);
              }}
              className="rounded px-3 py-2 text-sm aria-selected:bg-[color:var(--bg-elev2)] cursor-pointer"
            >
              Toggle theme (currently {theme})
            </Command.Item>
            <Command.Item
              value="find symbol mentions"
              onSelect={goSymbol}
              className="rounded px-3 py-2 text-sm aria-selected:bg-[color:var(--bg-elev2)] cursor-pointer"
            >
              {symbolMentionActionLabel(input)}
            </Command.Item>
          </Command.Group>
        </Command.List>
      </Command>
    </div>
  );
}
