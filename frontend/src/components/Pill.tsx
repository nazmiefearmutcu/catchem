import type { ReactNode } from "react";

type Variant =
  | "default"
  | "ac"
  | "rc"
  | "sym"
  | "good"
  | "bad"
  | "warn"
  | "diff-add"
  | "diff-remove"
  | "diff-kept";

const VARIANT_CLS: Record<Variant, string> = {
  default: "",
  ac: "text-accent",
  rc: "text-warn",
  sym: "text-good",
  good: "text-good",
  bad: "text-bad",
  warn: "text-warn",
  // Visual diff variants used in ReviewsComparePage row-level diff.
  // `diff-add`    = value only present in DeepSeek (green border).
  // `diff-remove` = value only present in stub (red border + strikethrough).
  // `diff-kept`   = value in both sides (muted).
  "diff-add": "border border-good/60 text-good",
  "diff-remove": "border border-bad/60 text-bad line-through",
  "diff-kept": "text-[color:var(--fg-muted)] opacity-70",
};

export function Pill({
  children,
  variant = "default",
  title,
  className = "",
}: {
  children: ReactNode;
  variant?: Variant;
  title?: string;
  className?: string;
}) {
  return (
    <span className={`chip ${VARIANT_CLS[variant]} ${className}`.trim()} title={title}>
      {children}
    </span>
  );
}
