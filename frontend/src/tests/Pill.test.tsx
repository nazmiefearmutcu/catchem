import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Pill } from "@/components/Pill";

// Pill is a tiny presentational <span class="chip ...">. The contract is:
// always the `chip` base class, a per-variant modifier from VARIANT_CLS, an
// optional caller className, an optional native `title`, and the children
// rendered verbatim. No providers/router/i18n required.
describe("Pill", () => {
  it("renders a <span> carrying the chip base class with its children", () => {
    const { container } = render(<Pill>hello</Pill>);
    const span = container.firstChild as HTMLElement;
    expect(span.tagName).toBe("SPAN");
    expect(span.className).toMatch(/\bchip\b/);
    expect(span).toHaveTextContent("hello");
  });

  it("adds no variant class for the default variant (chip only)", () => {
    const { container } = render(<Pill>x</Pill>);
    const span = container.firstChild as HTMLElement;
    // default maps to "" — trim() collapses the trailing spaces so the class
    // attribute is exactly "chip".
    expect(span.getAttribute("class")).toBe("chip");
  });

  // Each named variant contributes its own modifier class on top of `chip`.
  it.each([
    ["ac", /text-accent/],
    ["rc", /text-warn/],
    ["sym", /text-good/],
    ["good", /text-good/],
    ["bad", /text-bad/],
    ["warn", /text-warn/],
  ] as const)("applies the %s variant class", (variant, pattern) => {
    const { container } = render(<Pill variant={variant}>v</Pill>);
    const span = container.firstChild as HTMLElement;
    expect(span.className).toMatch(/\bchip\b/);
    expect(span.className).toMatch(pattern);
  });

  // The three diff variants drive the row-level diff in ReviewsComparePage and
  // carry richer class strings (borders / strikethrough / muted).
  it("applies diff-add border + good color", () => {
    const { container } = render(<Pill variant="diff-add">+</Pill>);
    const cls = (container.firstChild as HTMLElement).className;
    expect(cls).toMatch(/border/);
    expect(cls).toMatch(/text-good/);
  });

  it("applies diff-remove border + strikethrough", () => {
    const { container } = render(<Pill variant="diff-remove">-</Pill>);
    const cls = (container.firstChild as HTMLElement).className;
    expect(cls).toMatch(/border/);
    expect(cls).toMatch(/text-bad/);
    expect(cls).toMatch(/line-through/);
  });

  it("applies diff-kept muted styling", () => {
    const { container } = render(<Pill variant="diff-kept">=</Pill>);
    const cls = (container.firstChild as HTMLElement).className;
    expect(cls).toMatch(/fg-muted/);
    expect(cls).toMatch(/opacity-70/);
  });

  it("merges a caller className on top of chip + variant", () => {
    const { container } = render(
      <Pill variant="ac" className="ml-2 custom">
        c
      </Pill>,
    );
    const cls = (container.firstChild as HTMLElement).className;
    expect(cls).toMatch(/\bchip\b/);
    expect(cls).toMatch(/text-accent/);
    expect(cls).toMatch(/ml-2/);
    expect(cls).toMatch(/custom/);
  });

  it("sets the native title attribute when provided", () => {
    const { container } = render(<Pill title="tooltip text">t</Pill>);
    const span = container.firstChild as HTMLElement;
    expect(span.getAttribute("title")).toBe("tooltip text");
  });

  it("leaves title unset when omitted", () => {
    const { container } = render(<Pill>t</Pill>);
    const span = container.firstChild as HTMLElement;
    expect(span.hasAttribute("title")).toBe(false);
  });

  it("renders rich React-node children (not just strings)", () => {
    render(
      <Pill>
        <strong data-testid="inner">bold</strong>
      </Pill>,
    );
    const inner = screen.getByTestId("inner");
    expect(inner.tagName).toBe("STRONG");
    expect(inner).toHaveTextContent("bold");
  });
});
