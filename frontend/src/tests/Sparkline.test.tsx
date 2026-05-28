import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { Sparkline } from "@/components/Sparkline";

describe("Sparkline", () => {
  it("renders nothing for <2 points (no fake trend)", () => {
    const { container: a } = render(<Sparkline points={[]} />);
    expect(a.firstChild).toBeNull();
    const { container: b } = render(<Sparkline points={[42]} />);
    expect(b.firstChild).toBeNull();
  });

  it("emits an SVG polyline path for 2+ points", () => {
    const { container } = render(<Sparkline points={[1, 4, 2, 5]} />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    const path = container.querySelector("path");
    expect(path).not.toBeNull();
    const d = path?.getAttribute("d") ?? "";
    // M + 3 L's = 4 points (one M, three L).
    expect(d.startsWith("M")).toBe(true);
    expect((d.match(/L/g) ?? []).length).toBe(3);
  });

  it("collapses flat series to a horizontal line without divide-by-zero", () => {
    const { container } = render(<Sparkline points={[7, 7, 7, 7]} />);
    const path = container.querySelector("path");
    const d = path?.getAttribute("d") ?? "";
    // All points should sit at the same Y (height value), confirming
    // the (max-min) || 1 fallback didn't NaN the path.
    expect(d).not.toMatch(/NaN/);
    // 4 coordinates → one M + three L
    expect((d.match(/[ML]/g) ?? []).length).toBe(4);
  });

  it("marks itself aria-hidden by default but exposes ariaLabel as img role", () => {
    const { container: hidden } = render(<Sparkline points={[1, 2]} />);
    const svgHidden = hidden.querySelector("svg")!;
    expect(svgHidden.getAttribute("aria-hidden")).toBe("true");
    expect(svgHidden.getAttribute("role")).toBeNull();

    const { container: labeled } = render(
      <Sparkline points={[1, 2]} ariaLabel="trend up" />,
    );
    const svgLabeled = labeled.querySelector("svg")!;
    expect(svgLabeled.getAttribute("role")).toBe("img");
    expect(svgLabeled.getAttribute("aria-label")).toBe("trend up");
  });

  it("honors width/height props in the viewBox", () => {
    const { container } = render(
      <Sparkline points={[1, 2]} width={120} height={40} />,
    );
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("viewBox")).toBe("0 0 120 40");
    expect(svg.getAttribute("width")).toBe("120");
    expect(svg.getAttribute("height")).toBe("40");
  });
});
