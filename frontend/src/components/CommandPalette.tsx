import { useEffect, useState } from "react";
import { Command } from "cmdk";
import { useNavigate } from "react-router-dom";
import { useTheme } from "@/hooks/useTheme";

const NAV: { label: string; path: string; kbd?: string }[] = [
  { label: "Overview", path: "/", kbd: "g o" },
  { label: "Live Feed", path: "/feed", kbd: "g f" },
  { label: "Market Map", path: "/map", kbd: "g m" },
  { label: "Symbols", path: "/symbols", kbd: "g s" },
  { label: "Benchmark Lab", path: "/benchmark", kbd: "g b" },
  { label: "System / Ops", path: "/ops", kbd: "g x" },
  { label: "Settings / Help", path: "/settings", kbd: "g , " },
  { label: "Legacy Dashboard", path: "/legacy" },
];

/**
 * Cmd/Ctrl+K opens the palette. Includes navigation, theme toggle, and
 * symbol/reason quick-jump (typing a known symbol like AAPL routes you).
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
          placeholder="Type a page, symbol, or command…"
          className="w-full bg-transparent px-4 py-3 text-sm outline-none border-b border-[color:var(--border)]"
        />
        <Command.List className="max-h-96 overflow-auto p-1">
          <Command.Empty className="px-4 py-3 text-xs text-[color:var(--fg-dim)]">
            No matches — press <span className="kbd">Enter</span> to look up symbol <b className="text-good">{input.trim().toUpperCase()}</b>
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
              value="lookup symbol"
              onSelect={goSymbol}
              className="rounded px-3 py-2 text-sm aria-selected:bg-[color:var(--bg-elev2)] cursor-pointer"
            >
              Look up symbol "{input.trim().toUpperCase() || "AAPL"}"
            </Command.Item>
          </Command.Group>
        </Command.List>
      </Command>
    </div>
  );
}
