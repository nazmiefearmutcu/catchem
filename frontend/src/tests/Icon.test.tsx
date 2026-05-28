import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { Icon } from "@/components/Icon";

describe("Icon", () => {
  // Each test inspects the rendered <svg>; we keep these helpers tight so
  // the assertions read close to the contract: viewBox 24, currentColor
  // stroke, aria-hidden, baseline size.
  it("renders an <svg> with the canonical lucide viewBox + currentColor stroke", () => {
    const { container } = render(<Icon name="close" />);
    const svg = container.querySelector("svg")!;
    expect(svg).toBeInTheDocument();
    expect(svg.getAttribute("viewBox")).toBe("0 0 24 24");
    expect(svg.getAttribute("stroke")).toBe("currentColor");
    expect(svg.getAttribute("fill")).toBe("none");
    expect(svg.getAttribute("aria-hidden")).toBe("true");
    // Default 14px sizing matches the surrounding text-[10px] chip baseline.
    expect(svg.getAttribute("width")).toBe("14");
    expect(svg.getAttribute("height")).toBe("14");
  });

  it("applies a custom size to both width and height", () => {
    const { container } = render(<Icon name="bell" size={20} />);
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("width")).toBe("20");
    expect(svg.getAttribute("height")).toBe("20");
  });

  it("merges a custom className on top of the inline-block default", () => {
    const { container } = render(<Icon name="refresh" className="text-accent" />);
    const svg = container.querySelector("svg")!;
    const cls = svg.getAttribute("class") ?? "";
    // Built-in classes survive.
    expect(cls).toMatch(/inline-block/);
    expect(cls).toMatch(/flex-shrink-0/);
    // Caller class is appended.
    expect(cls).toMatch(/text-accent/);
  });

  it("forwards extra SVG props (e.g. data-testid) to the underlying <svg>", () => {
    const { container } = render(
      <Icon name="download" data-testid="dl-icon" />,
    );
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("data-testid")).toBe("dl-icon");
  });

  it("renders nothing for an unknown icon name (graceful no-op)", () => {
    // Silence the dev warning for this test; the prod behaviour is
    // 'render nothing, don't throw' regardless.
    const spy = vi.spyOn(console, "warn").mockImplementation(() => {});
    // Cast through unknown so TS doesn't reject a deliberate bad key.
    const Bad = Icon as unknown as React.FC<{ name: string }>;
    const { container } = render(<Bad name="not-a-real-icon" />);
    expect(container.querySelector("svg")).toBeNull();
    spy.mockRestore();
  });
});
