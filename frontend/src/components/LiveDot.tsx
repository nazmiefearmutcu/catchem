import type { LiveStatus } from "@/hooks/useLiveStream";

const COLOR: Record<LiveStatus, string> = {
  idle: "bg-[color:var(--fg-muted)]",
  connecting: "bg-warn",
  open: "bg-good",
  polling: "bg-accent",
  error: "bg-bad",
};

export function LiveDot({ status, label }: { status: LiveStatus; label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)]">
      <span className={`inline-block h-2 w-2 rounded-full animate-pulse-dot ${COLOR[status]}`} />
      {label ?? status}
    </span>
  );
}
