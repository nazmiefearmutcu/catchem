import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, fmtDate, fmtScore, safeHref, scoreToneClass } from "@/lib/api";
import { Pill } from "@/components/Pill";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import { Icon } from "@/components/Icon";
import { useOverlaySurface } from "@/context/overlayCoordinator";

// Mirror the backend regex (catchem.storage._validate_tag) so the user gets
// fast feedback before a 400. Anything matching this passes the API gate.
const TAG_PATTERN = /^[a-zA-Z0-9_\-.]+$/;

interface Props {
  captureId: string;
  onClose: () => void;
}

export function RecordDrawer({ captureId, onClose }: Props) {
  const lastFocusedRef = useRef<HTMLElement | null>(null);
  useOverlaySurface({
    id: `record-drawer:${captureId}`,
    open: true,
    onClose,
    lockBody: true,
  });

  const { data, isLoading, error } = useQuery({
    queryKey: ["record", captureId],
    queryFn: () => api.record(captureId),
  });
  const [showRaw, setShowRaw] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    lastFocusedRef.current = (document.activeElement as HTMLElement | null) ?? null;
    const t = setTimeout(() => closeRef.current?.focus(), 0);
    return () => {
      clearTimeout(t);
      const prev = lastFocusedRef.current;
      if (prev && typeof prev.focus === "function") {
        try {
          prev.focus();
        } catch {
          /* ignore */
        }
      }
    };
  }, []);

  return (
    <div
      className="fixed inset-0 z-40 flex justify-end bg-black/40"
      role="dialog"
      aria-modal="true"
      aria-label="Record detail"
      onClick={onClose}
    >
      <aside
        className="w-full sm:w-[640px] h-full bg-[color:var(--bg)] border-l border-[color:var(--border)] overflow-auto animate-slide-in"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="sticky top-0 z-10 bg-[color:var(--bg)] border-b border-[color:var(--border)] px-4 py-3 flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">RECORD · NEWS DETAIL</div>
            <div className="text-xs text-[color:var(--fg-dim)] truncate" title={captureId}>{captureId}</div>
          </div>
          <button ref={closeRef} className="btn shrink-0" onClick={onClose} aria-label="Close">esc · close</button>
        </header>
        <div className="p-4 grid gap-4">
          {isLoading && <DetailSkeleton />}
          {error && <ErrorBox err={error} />}
          {data && (
            <>
              <section>
                <div className="text-[10px] text-[color:var(--fg-dim)] mb-1">
                  {fmtDate(data.published_ts) || fmtDate(data.created_at)} · {data.domain ?? "—"} · {data.language ?? "—"}
                </div>
                <h2 className="text-lg font-semibold leading-snug">{data.title ?? "(untitled)"}</h2>
                {safeHref(data.url) && (
                  <a href={safeHref(data.url)} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
                    open source
                    <Icon name="external" size={12} />
                  </a>
                )}
              </section>

              <section className="grid grid-cols-2 gap-3">
                <div className="card">
                  <div className="label">finance score</div>
                  <div className={`mt-1 text-xl font-semibold tabular-nums ${scoreToneClass(data.finance_relevance_score)}`}>
                    {fmtScore(data.finance_relevance_score)}
                  </div>
                </div>
                <Card label="relevant" value={data.is_finance_relevant ? "YES" : "no"}
                      tone={data.is_finance_relevant ? "good" : "bad"} />
                <Card label="sentiment" value={data.sentiment_label ?? "—"} hint={data.sentiment_score != null ? fmtScore(data.sentiment_score) : undefined} />
                <Card label="processing mode" value={data.processing_mode} />
              </section>

              <section>
                <div className="label mb-1">labels</div>
                <div className="flex flex-wrap gap-1">
                  {data.asset_classes.map((ac) => <Pill key={ac} variant="ac">{ac}</Pill>)}
                  {data.impact_reason_codes.map((rc) => <Pill key={rc} variant="rc">{rc}</Pill>)}
                  {data.impact_horizons.map((h) => <Pill key={h}>{h}</Pill>)}
                  {data.asset_classes.length + data.impact_reason_codes.length === 0 && (
                    <span className="text-xs text-[color:var(--fg-dim)]">none</span>
                  )}
                </div>
              </section>

              {(data.candidate_symbols.length > 0 || data.candidate_entities.length > 0) && (
                <section>
                  <div className="label mb-1">symbols / entities</div>
                  <div className="flex flex-wrap gap-1">
                    {data.candidate_symbols.map((s) => <Pill key={s} variant="sym">{s}</Pill>)}
                    {data.candidate_entities.slice(0, 12).map((e) => <Pill key={e}>{e}</Pill>)}
                  </div>
                </section>
              )}

              <TagsSection captureId={captureId} />


              <section>
                <div className="label mb-1">evidence sentences</div>
                {data.evidence_sentences.length === 0 ? (
                  <EmptyState title="no extractive evidence picked" hint="evidence pipeline returned an empty set for this record" />
                ) : (
                  <ul className="space-y-1">
                    {data.evidence_sentences.map((s, i) => (
                      <li key={i} className="text-sm italic text-[color:var(--fg-dim)] before:content-['“'] after:content-['”']">{s}</li>
                    ))}
                  </ul>
                )}
                {data.reason_text && (
                  <p className="mt-2 text-[10px] text-[color:var(--fg-muted)] font-mono">{data.reason_text}</p>
                )}
              </section>

              <section>
                <div className="label mb-1">component scores</div>
                <ComponentTable scores={data.component_scores as unknown as Record<string, number>} />
              </section>

              <section>
                <div className="label mb-1">model versions</div>
                <ul className="text-xs grid grid-cols-2 gap-y-1">
                  {Object.entries(data.model_versions as unknown as Record<string, string>).map(([k, v]) => (
                    <li key={k}><span className="text-[color:var(--fg-dim)]">{k}</span> <span>{v}</span></li>
                  ))}
                </ul>
              </section>

              {data.diagnostic_multimodal_enabled && data.diagnostic_multimodal_result && (
                <section className="card border-warn/40 bg-warn/5">
                  <div className="label mb-1 text-warn">diagnostic (read-only)</div>
                  <pre className="text-[10px] overflow-x-auto">{JSON.stringify(data.diagnostic_multimodal_result, null, 2)}</pre>
                </section>
              )}

              <section>
                <button className="btn" onClick={() => setShowRaw((v) => !v)}>
                  {showRaw ? "hide raw JSON" : "show raw JSON"}
                </button>
                {showRaw && (
                  <pre className="mt-2 max-h-72 overflow-auto rounded border border-[color:var(--border)] bg-[color:var(--bg-elev)] p-2 text-[10px]">
                    {JSON.stringify(data, null, 2)}
                  </pre>
                )}
              </section>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

function Card({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "good" | "bad" }) {
  const cls = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : "";
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${cls}`}>{value}</div>
      {hint && <div className="text-[10px] text-[color:var(--fg-dim)]">{hint}</div>}
    </div>
  );
}

function ComponentTable({ scores }: { scores: Record<string, number> }) {
  const entries = Object.entries(scores).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) return <p className="text-xs text-[color:var(--fg-dim)]">none</p>;
  const max = Math.max(...entries.map(([, n]) => Math.abs(n) || 0), 1);
  return (
    <ul className="grid gap-1">
      {entries.map(([k, v]) => (
        <li key={k} className="grid grid-cols-[160px_1fr_60px] gap-2 items-center text-xs">
          <span className="text-[color:var(--fg-dim)] truncate" title={k}>{k}</span>
          <span className="h-1.5 rounded bg-[color:var(--bg-elev2)] overflow-hidden">
            <span className="block h-full bg-accent/70" style={{ width: `${100 * Math.min(1, Math.abs(v) / max)}%` }} />
          </span>
          <span className="text-right tabular-nums">{v.toFixed(3)}</span>
        </li>
      ))}
    </ul>
  );
}

function DetailSkeleton() {
  return (
    <div className="grid gap-3" aria-busy="true">
      <Skeleton className="h-6 w-3/4" />
      <Skeleton className="h-4 w-1/2" />
      <Skeleton className="h-32" />
      <Skeleton className="h-24" />
    </div>
  );
}

/**
 * User-defined tag editor. Sits below the asset-class / reason-code pills so
 * the analyst can layer free-form tags on top of the pipeline-derived labels.
 *
 * Validation is duplicated on the client (TAG_PATTERN) so the user sees a red
 * inline note instantly; the backend re-checks identically and returns 400 if
 * the client check is bypassed. Optimistic update keeps the UI snappy on the
 * happy path — on failure we roll back to the server snapshot.
 */
export function TagsSection({ captureId }: { captureId: string }) {
  const qc = useQueryClient();
  const queryKey = ["record-tags", captureId];
  const tagsQuery = useQuery({
    queryKey,
    queryFn: () => api.getTags(captureId),
    staleTime: 5_000,
  });
  const [draft, setDraft] = useState("");
  const [inlineError, setInlineError] = useState<string | null>(null);
  const [lastAction, setLastAction] = useState<
    { kind: "added" | "removed" | "noop"; tag: string } | null
  >(null);

  const addMutation = useMutation({
    mutationFn: (tag: string) => api.addTag(captureId, tag),
    onMutate: async (tag) => {
      await qc.cancelQueries({ queryKey });
      const previous = qc.getQueryData<{ capture_id: string; tags: string[] }>(queryKey);
      // Optimistic insert — keep sorted to match the server response shape.
      if (previous && !previous.tags.includes(tag)) {
        qc.setQueryData(queryKey, {
          ...previous,
          tags: [...previous.tags, tag].sort(),
        });
      }
      return { previous };
    },
    onError: (err, _tag, ctx) => {
      if (ctx?.previous) qc.setQueryData(queryKey, ctx.previous);
      const msg = err instanceof ApiError
        ? err.status === 400
          ? "tag rejected by server"
          : `request failed (${err.status})`
        : "request failed";
      setInlineError(msg);
    },
    onSuccess: (res, tag) => {
      qc.setQueryData(queryKey, { capture_id: captureId, tags: res.tags });
      qc.invalidateQueries({ queryKey: ["tags-top"] });
      // The Tags aggregation page (frontend/.../tags/TagsPage.tsx) keys
      // its useQuery on ["tags-aggregate"]. Without this invalidation a
      // tag added or removed in the record drawer wouldn't propagate to
      // the page until the next 60s refetch poll fired — analysts saw
      // chip counts stuck at stale values for up to a minute.
      qc.invalidateQueries({ queryKey: ["tags-aggregate"] });
      setDraft("");
      setInlineError(null);
      setLastAction({ kind: res.added ? "added" : "noop", tag });
    },
  });

  const removeMutation = useMutation({
    mutationFn: (tag: string) => api.removeTag(captureId, tag),
    onMutate: async (tag) => {
      await qc.cancelQueries({ queryKey });
      const previous = qc.getQueryData<{ capture_id: string; tags: string[] }>(queryKey);
      if (previous) {
        qc.setQueryData(queryKey, {
          ...previous,
          tags: previous.tags.filter((t) => t !== tag),
        });
      }
      return { previous };
    },
    onError: (err, _tag, ctx) => {
      if (ctx?.previous) qc.setQueryData(queryKey, ctx.previous);
      const msg = err instanceof ApiError
        ? `request failed (${err.status})`
        : "request failed";
      setInlineError(msg);
    },
    onSuccess: (res, tag) => {
      qc.setQueryData(queryKey, { capture_id: captureId, tags: res.tags });
      qc.invalidateQueries({ queryKey: ["tags-top"] });
      // The Tags aggregation page (frontend/.../tags/TagsPage.tsx) keys
      // its useQuery on ["tags-aggregate"]. Without this invalidation a
      // tag added or removed in the record drawer wouldn't propagate to
      // the page until the next 60s refetch poll fired — analysts saw
      // chip counts stuck at stale values for up to a minute.
      qc.invalidateQueries({ queryKey: ["tags-aggregate"] });
      setInlineError(null);
      setLastAction({ kind: "removed", tag });
    },
  });

  function submitDraft() {
    const cleaned = draft.trim();
    if (!cleaned) {
      setInlineError("tag must not be empty");
      return;
    }
    if (cleaned.length > 50) {
      setInlineError("tag must be <= 50 characters");
      return;
    }
    if (!TAG_PATTERN.test(cleaned)) {
      setInlineError("only [a-zA-Z0-9_-.] allowed, no whitespace");
      return;
    }
    setInlineError(null);
    addMutation.mutate(cleaned);
  }

  const tags = tagsQuery.data?.tags ?? [];

  return (
    <section data-testid="tags-section">
      <div className="label mb-1">tags</div>
      <div className="flex flex-wrap gap-1" data-testid="tags-list">
        {tags.length === 0 && !tagsQuery.isLoading && (
          <span className="text-xs text-[color:var(--fg-dim)]">no tags yet</span>
        )}
        {tags.map((tag) => (
          <button
            key={tag}
            type="button"
            className="chip chip-active inline-flex items-center gap-1"
            onClick={() => removeMutation.mutate(tag)}
            disabled={removeMutation.isPending}
            data-testid={`tag-pill-${tag}`}
            title={`Remove tag "${tag}"`}
            aria-label={`Remove tag ${tag}`}
          >
            <span>{tag}</span>
            <span aria-hidden className="text-[10px] opacity-70">×</span>
          </button>
        ))}
      </div>
      <div className="mt-2 flex items-center gap-2">
        <input
          type="text"
          className="input flex-1"
          placeholder="add tag (a-z 0-9 _ - .)"
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            if (inlineError) setInlineError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submitDraft();
            }
          }}
          maxLength={50}
          aria-label="New tag"
          data-testid="tag-input"
          disabled={addMutation.isPending}
        />
        <button
          type="button"
          className="btn"
          onClick={submitDraft}
          disabled={addMutation.isPending || !draft.trim()}
          data-testid="tag-add"
        >
          add
        </button>
      </div>
      {inlineError && (
        <div className="mt-1 text-[11px] text-bad" data-testid="tag-error">
          {inlineError}
        </div>
      )}
      {!inlineError && lastAction && (
        <div className="mt-1 text-[11px] text-[color:var(--fg-dim)]" data-testid="tag-action">
          {lastAction.kind === "added" && `added "${lastAction.tag}"`}
          {lastAction.kind === "removed" && `removed "${lastAction.tag}"`}
          {lastAction.kind === "noop" && `"${lastAction.tag}" already attached`}
        </div>
      )}
    </section>
  );
}
