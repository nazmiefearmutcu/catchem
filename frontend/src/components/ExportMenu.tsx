import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "@/components/Icon";
import { closeAllOverlays, useOverlaySurface } from "@/context/overlayCoordinator";

/**
 * Small chip-styled menu that exposes a "CSV / JSON" picker for
 * filtered analyst exports. The parent passes precomputed download
 * URLs (built by `api.exportRecordsUrl` etc.) so this component stays
 * dumb — no knowledge of filter state or endpoint paths.
 *
 * Two flavors:
 *  - menu (default): chip → click → tiny popover with both formats.
 *  - inline: render both buttons side-by-side (useful in toolbars).
 *
 * If `formats` only includes "json" we collapse to a single chip — the
 * QuantScan page does this because nested signals don't survive CSV.
 */
export interface ExportMenuProps {
  label?: string;
  formats?: ReadonlyArray<"csv" | "json">;
  /** Resolve a download URL for the chosen format. */
  buildUrl: (format: "csv" | "json") => string;
  /** Filename hint sent to the browser as the `download` attribute. */
  filenameHint?: string;
  /** Render inline (no popover, both buttons visible). */
  inline?: boolean;
  /** Extra title text for the trigger chip. */
  title?: string;
  /** data-testid for tests. */
  testId?: string;
  /** Short description shown inside the popover. */
  hint?: string;
}

export function ExportMenu({
  label = "export",
  formats = ["csv", "json"] as const,
  buildUrl,
  filenameHint,
  inline = false,
  title,
  testId,
  hint,
}: ExportMenuProps) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const surfaceId = useId();
  useOverlaySurface({
    id: `export-menu:${surfaceId}`,
    open,
    onClose: () => setOpen(false),
    lockBody: false,
  });

  // Click-outside close. Escape is delegated to the global overlay
  // coordinator so stacked overlays behave consistently.
  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    }
    window.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  if (formats.length === 1) {
    const fmt = formats[0]!;
    return (
      <a
        href={buildUrl(fmt)}
        download={filenameHint ?? true}
        className="chip text-[10px] hover:bg-[color:var(--bg-elev2)]"
        title={title ?? `Download ${fmt.toUpperCase()}`}
        data-testid={testId}
      >
        <span className="inline-flex items-center gap-1">
          <Icon name="download" />
          {label} {fmt.toUpperCase()}
        </span>
      </a>
    );
  }
  if (inline) {
    return (
      <span className="inline-flex items-center gap-1">
        <span className="text-[10px] text-[color:var(--fg-muted)]">{label}</span>
        {formats.map((f) => (
          <a
            key={f}
            href={buildUrl(f)}
            download={filenameHint ?? true}
            className="chip text-[10px] hover:bg-[color:var(--bg-elev2)]"
            title={title ?? `Download ${f.toUpperCase()}`}
            data-testid={testId ? `${testId}-${f}` : undefined}
          >
            {f.toUpperCase()}
          </a>
        ))}
      </span>
    );
  }
  return (
    <div ref={wrapRef} className="relative inline-block">
      <button
        type="button"
        className="chip text-[10px] hover:bg-[color:var(--bg-elev2)]"
        onClick={() => {
          if (open) {
            setOpen(false);
            return;
          }
          closeAllOverlays();
          setOpen(true);
        }}
        aria-expanded={open}
        aria-haspopup="menu"
        title={title ?? "Download filtered export"}
        data-testid={testId}
      >
        <span className="inline-flex items-center gap-1">
          <Icon name="download" />
          {label}
        </span>
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-20 mt-1 min-w-[160px] rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elev2)] p-1.5 shadow-lg"
        >
          {hint && (
            <p className="px-1 pb-1 text-[10px] text-[color:var(--fg-muted)]">{hint}</p>
          )}
          <div className="grid gap-1">
            {formats.map((f) => (
              <a
                key={f}
                role="menuitem"
                href={buildUrl(f)}
                download={filenameHint ?? true}
                className="block rounded-sm px-2 py-1 text-[11px] hover:bg-[color:var(--bg-elev)] text-[color:var(--fg)]"
                onClick={() => setOpen(false)}
                data-testid={testId ? `${testId}-${f}` : undefined}
              >
                <span className="font-semibold">{f.toUpperCase()}</span>
                <span className="ml-1 text-[10px] text-[color:var(--fg-dim)]">
                  {f === "csv" ? "flat spreadsheet" : "structured records"}
                </span>
              </a>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
