import type { ReactNode } from "react";

type Variant = "default" | "ac" | "rc" | "sym" | "good" | "bad" | "warn";

const VARIANT_CLS: Record<Variant, string> = {
  default: "",
  ac: "text-accent",
  rc: "text-warn",
  sym: "text-good",
  good: "text-good",
  bad: "text-bad",
  warn: "text-warn",
};

export function Pill({ children, variant = "default", title }: { children: ReactNode; variant?: Variant; title?: string }) {
  return (
    <span className={`chip ${VARIANT_CLS[variant]}`} title={title}>
      {children}
    </span>
  );
}
