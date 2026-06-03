import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { EChart } from "@/charts/EChart";

// Mock useTheme hook
vi.mock("@/hooks/useTheme", () => ({
  useTheme: () => ({ theme: "dark" }),
}));

describe("EChart Accessibility and Skeletons", () => {
  const dummyOption = {
    title: { text: "Test Chart" },
    xAxis: { data: ["A", "B", "C"] },
    series: [
      {
        name: "Test Series",
        data: [10, 20, 30],
      },
    ],
  };

  it("renders a skeleton fallback layout while loading lazily", () => {
    const { getByLabelText, container } = render(
      <EChart option={dummyOption} height={200} />
    );

    // Verify it contains the fallback element with aria-busy="true"
    const skeleton = getByLabelText("Loading chart");
    expect(skeleton).not.toBeNull();
    expect(skeleton.getAttribute("aria-busy")).toBe("true");

    // Verify it contains skeleton sub-components simulating title, legend, bars, and axis
    // There should be multiple shimmer elements
    const shimmers = container.querySelectorAll(".animate-shimmer");
    expect(shimmers.length).toBeGreaterThanOrEqual(10);
  });

  it("has appropriate keyboard navigation attributes on the container", () => {
    const { container } = render(<EChart option={dummyOption} />);
    const chartWrapper = container.firstChild as HTMLElement;

    expect(chartWrapper.getAttribute("tabIndex")).toBe("0");
    expect(chartWrapper.getAttribute("role")).toBe("application");
    expect(chartWrapper.getAttribute("aria-label")).toContain("Chart: Test Chart");
  });

  it("handles keyboard events for navigation safely", () => {
    const { container } = render(<EChart option={dummyOption} />);
    const chartWrapper = container.firstChild as HTMLElement;

    // Simulate focusing the wrapper
    fireEvent.focus(chartWrapper);

    // Press right arrow
    fireEvent.keyDown(chartWrapper, { key: "ArrowRight" });

    // Press left arrow
    fireEvent.keyDown(chartWrapper, { key: "ArrowLeft" });

    // Press escape
    fireEvent.keyDown(chartWrapper, { key: "Escape" });

    // Blur
    fireEvent.blur(chartWrapper);
  });
});
