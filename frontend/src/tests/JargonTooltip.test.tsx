import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { JargonTooltip } from "@/components/JargonTooltip";
import { JARGON } from "@/lib/jargon";

// JargonTooltip wraps a term in a dotted-underline, focusable, screen-reader
// labelled span IFF the term exists in the JARGON dictionary. Unknown terms
// fall through to plain children/term so the wrapper is always safe to drop in.
// A known term picked from the real dictionary so the test tracks the contract.
const KNOWN = "DLQ";
const KNOWN_DEF = JARGON[KNOWN];

describe("JargonTooltip", () => {
  it("renders a focusable span with title + aria-label for a known term", () => {
    const { container } = render(<JargonTooltip term={KNOWN} />);
    const span = container.firstChild as HTMLElement;
    expect(span.tagName).toBe("SPAN");
    // Definition surfaces via the native title attribute (no popper lib).
    expect(span.getAttribute("title")).toBe(KNOWN_DEF);
    // Screen-reader label is "<term>: <definition>".
    expect(span.getAttribute("aria-label")).toBe(`${KNOWN}: ${KNOWN_DEF}`);
    // Keyboard focusable for hover-equivalent affordance.
    expect(span.getAttribute("tabindex")).toBe("0");
  });

  it("applies the dotted-underline + cursor-help affordance classes", () => {
    const { container } = render(<JargonTooltip term={KNOWN} />);
    const cls = (container.firstChild as HTMLElement).className;
    expect(cls).toMatch(/underline/);
    expect(cls).toMatch(/decoration-dotted/);
    expect(cls).toMatch(/cursor-help/);
  });

  it("uses the term itself as text when no children are given", () => {
    render(<JargonTooltip term={KNOWN} />);
    expect(screen.getByText(KNOWN)).toBeInTheDocument();
  });

  it("renders children instead of the term when provided", () => {
    render(<JargonTooltip term={KNOWN}>dead-letter queue</JargonTooltip>);
    expect(screen.getByText("dead-letter queue")).toBeInTheDocument();
    // The raw term is not shown when children override it.
    expect(screen.queryByText(KNOWN)).toBeNull();
  });

  it("merges a caller className onto the decorated span", () => {
    const { container } = render(
      <JargonTooltip term={KNOWN} className="font-bold extra" />,
    );
    const cls = (container.firstChild as HTMLElement).className;
    expect(cls).toMatch(/decoration-dotted/);
    expect(cls).toMatch(/font-bold/);
    expect(cls).toMatch(/extra/);
  });

  it("falls back to a plain span (no underline/title) for an unknown term", () => {
    const { container } = render(<JargonTooltip term="not-a-real-term-xyz" />);
    const span = container.firstChild as HTMLElement;
    expect(span.tagName).toBe("SPAN");
    // Renders the term text untouched.
    expect(span).toHaveTextContent("not-a-real-term-xyz");
    // No tooltip / a11y decoration on the miss path.
    expect(span.hasAttribute("title")).toBe(false);
    expect(span.hasAttribute("aria-label")).toBe(false);
    expect(span.hasAttribute("tabindex")).toBe(false);
    expect(span.className).not.toMatch(/decoration-dotted/);
  });

  it("on the unknown path still renders children when provided", () => {
    render(
      <JargonTooltip term="no-such-term">
        <em data-testid="plain">visible label</em>
      </JargonTooltip>,
    );
    const inner = screen.getByTestId("plain");
    expect(inner.tagName).toBe("EM");
    expect(inner).toHaveTextContent("visible label");
  });

  it("applies the caller className on the unknown-term fallback span", () => {
    const { container } = render(
      <JargonTooltip term="missing" className="text-bad" />,
    );
    const span = container.firstChild as HTMLElement;
    expect(span.className).toBe("text-bad");
  });
});
