import type { ReactNode } from "react";
import { JARGON } from "@/lib/jargon";

/**
 * Inline tooltip for domain jargon. If `term` is in the JARGON dictionary,
 * the children/term get a dotted underline + cursor-help + native `title`
 * tooltip on hover and an `aria-label` for screen readers. If the term is
 * not in the dictionary, this renders the children/term untouched so the
 * wrapper is safe to drop into any label without worrying about misses.
 */
export function JargonTooltip({
  term,
  children,
  className = "",
}: {
  term: string;
  children?: ReactNode;
  className?: string;
}) {
  const def = JARGON[term];
  if (!def) {
    // No entry — render plain children/term without dotted underline
    return <span className={className}>{children ?? term}</span>;
  }
  return (
    <span
      tabIndex={0}
      title={def}
      aria-label={`${term}: ${def}`}
      className={`underline decoration-dotted decoration-[color:var(--fg-muted)] underline-offset-2 cursor-help focus:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded-sm ${className}`}
    >
      {children ?? term}
    </span>
  );
}
