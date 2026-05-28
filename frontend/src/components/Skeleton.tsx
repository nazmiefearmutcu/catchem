import type { ReactNode } from "react";

export function Skeleton({ className = "h-4 w-full" }: { className?: string }) {
  return (
    <div
      className={`animate-shimmer rounded ${className}`}
      aria-hidden="true"
    />
  );
}

export function ErrorBox({ err }: { err: unknown }) {
  const rawMsg = err instanceof Error ? err.message : String(err);
  // Backend rate limiter (src/catchem/rate_limit.py) emits a 429 with
  // the verbatim "Rate limit" substring in the detail body. The api.ts
  // wrapper rolls the status + body into `${path} → 429 {…}`. Detect
  // that and swap in a humane message so the user sees "slow down"
  // instead of HTTP wreckage. Substring match on purpose — the wrapper
  // can change formatting without breaking this hook.
  const isRateLimit = rawMsg.includes("Rate limit") || rawMsg.includes(" 429 ");
  const msg = isRateLimit
    ? "Slow down — too many requests. Please wait a moment and try again."
    : rawMsg;
  return (
    <div className="rounded-md border border-bad/40 bg-bad/10 px-3 py-2.5 text-xs text-bad flex items-start gap-2" role="alert">
      <span className="mt-0.5 inline-block h-1.5 w-1.5 rounded-full bg-bad shrink-0" aria-hidden />
      <span className="leading-relaxed">{msg}</span>
    </div>
  );
}

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="card text-center text-xs text-[color:var(--fg-dim)] py-6">
      <div className="font-semibold text-[color:var(--fg)]">{title}</div>
      {hint && <div className="mt-1 max-w-md mx-auto leading-relaxed">{hint}</div>}
      {action && <div className="mt-3 flex items-center justify-center">{action}</div>}
    </div>
  );
}
