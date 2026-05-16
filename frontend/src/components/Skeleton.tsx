export function Skeleton({ className = "h-4 w-full" }: { className?: string }) {
  return (
    <div
      className={`animate-pulse rounded bg-[color:var(--bg-elev2)] ${className}`}
      aria-hidden="true"
    />
  );
}

export function ErrorBox({ err }: { err: unknown }) {
  const msg = err instanceof Error ? err.message : String(err);
  return (
    <div className="rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad" role="alert">
      {msg}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="card text-center text-xs text-[color:var(--fg-dim)]">
      <div className="font-semibold text-[color:var(--fg)]">{title}</div>
      {hint && <div className="mt-1">{hint}</div>}
    </div>
  );
}
