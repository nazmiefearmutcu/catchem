import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { Skeleton } from "@/components/Skeleton";

describe("Skeleton (v22 shimmer)", () => {
  it("renders a single div with animate-shimmer + rounded by default", () => {
    const { container } = render(<Skeleton />);
    const root = container.firstChild as HTMLElement;
    expect(root.tagName).toBe("DIV");
    expect(root.className).toMatch(/animate-shimmer/);
    expect(root.className).toMatch(/rounded/);
    // Default sizing tokens propagate.
    expect(root.className).toMatch(/h-4/);
    expect(root.className).toMatch(/w-full/);
  });

  it("merges caller className over the defaults", () => {
    const { container } = render(<Skeleton className="h-72 w-1/2" />);
    const root = container.firstChild as HTMLElement;
    expect(root.className).toMatch(/animate-shimmer/);
    expect(root.className).toMatch(/h-72/);
    expect(root.className).toMatch(/w-1\/2/);
    // No inner shimmer div — single-element contract.
    expect(root.children.length).toBe(0);
  });

  it("is hidden from assistive tech (aria-hidden)", () => {
    const { container } = render(<Skeleton />);
    const root = container.firstChild as HTMLElement;
    expect(root.getAttribute("aria-hidden")).toBe("true");
  });
});
