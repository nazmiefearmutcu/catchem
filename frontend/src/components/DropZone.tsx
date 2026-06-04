import { useCallback, useId, useRef, useState } from "react";
import type { ChangeEvent, DragEvent } from "react";

const ALLOWED = [".txt", ".md", ".markdown", ".html", ".htm", ".jsonl", ".json"];

export interface DropZoneProps {
  onFile: (file: File) => void;
  disabled?: boolean;
  className?: string;
}

/**
 * Drag-and-drop file zone with click fallback (keyboard + screen-reader safe).
 * Renders a hidden <input type="file"> as the canonical control; the visible
 * div delegates clicks/keypresses to it so the browser handles a11y for us.
 */
export function DropZone({ onFile, disabled = false, className = "" }: DropZoneProps) {
  const [hover, setHover] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const inputId = useId();

  const accept = (file: File | null) => {
    if (!file) return;
    setError(null);
    const lower = file.name.toLowerCase();
    if (!ALLOWED.some((ext) => lower.endsWith(ext))) {
      setError(`Unsupported file. Allowed: ${ALLOWED.join(", ")}`);
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      setError(`File too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Max 5 MB.`);
      return;
    }
    onFile(file);
  };

  const onDrop = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setHover(false);
    if (disabled) return;
    accept(e.dataTransfer?.files?.[0] ?? null);
  }, [disabled]);

  const onChange = (e: ChangeEvent<HTMLInputElement>) => {
    accept(e.target.files?.[0] ?? null);
    // Allow re-uploading the same file
    if (inputRef.current) inputRef.current.value = "";
  };

  return (
    <div className={className}>
      <div
        onDragOver={(e) => { if (!disabled) { e.preventDefault(); setHover(true); } }}
        onDragLeave={() => setHover(false)}
        onDrop={onDrop}
        onClick={() => !disabled && inputRef.current?.click()}
        onKeyDown={(e) => {
          if (disabled) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled}
        aria-describedby={`${inputId}-hint`}
        aria-label="Upload article file"
        className={`flex flex-col items-center justify-center rounded-lg border-2 border-dashed p-8 transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
          disabled
            ? "border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/50 opacity-50 cursor-not-allowed"
            : hover
              ? "border-accent bg-accent/5"
              : "border-[color:var(--border)] hover:border-accent/60 hover:bg-[color:var(--bg-elev2)]/50"
        }`}
      >
        <div className="text-sm font-semibold mb-1">Drop a file here</div>
        <div id={`${inputId}-hint`} className="text-xs text-[color:var(--fg-dim)]">
          or click to browse · {ALLOWED.join(" / ")} · ≤ 5 MB
        </div>
        <input
          ref={inputRef}
          id={inputId}
          type="file"
          accept={ALLOWED.join(",")}
          onChange={onChange}
          disabled={disabled}
          className="sr-only"
        />
      </div>
      {error && (
        <div className="mt-2 rounded border border-bad/40 bg-bad/10 px-3 py-1.5 text-xs text-bad" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
