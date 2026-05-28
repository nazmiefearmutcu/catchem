import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { LiveDot } from "@/components/LiveDot";

describe("LiveDot", () => {
  it("renders an idle dot when status=idle and no staleness given", () => {
    render(<LiveDot status="idle" />);
    const dot = screen.getByTestId("live-dot");
    expect(dot.getAttribute("data-status")).toBe("idle");
    expect(dot.getAttribute("data-staleness")).toBe("none");
    expect(dot.textContent).toMatch(/idle/i);
  });

  it("status=open with fresh staleness reports 'live' + green dot", () => {
    render(<LiveDot status="open" stalenessSeconds={5} />);
    const dot = screen.getByTestId("live-dot");
    expect(dot.getAttribute("data-status")).toBe("open");
    expect(dot.textContent).toMatch(/live/i);
    expect(dot.getAttribute("title")).toMatch(/live · last beat 5s ago/);
    // Inner dot uses the good token.
    const inner = dot.querySelector("span[aria-hidden]")!;
    expect(inner.className).toMatch(/bg-good/);
  });

  it("status=open with 30-89s staleness degrades to warn (idle label)", () => {
    render(<LiveDot status="open" stalenessSeconds={45} />);
    const dot = screen.getByTestId("live-dot");
    expect(dot.textContent).toMatch(/idle/i);
    const inner = dot.querySelector("span[aria-hidden]")!;
    expect(inner.className).toMatch(/bg-warn/);
    expect(dot.getAttribute("title")).toMatch(/stale · last beat 45s ago/);
  });

  it("status=open with >=90s staleness escalates to bad (stale label)", () => {
    render(<LiveDot status="open" stalenessSeconds={120} />);
    const dot = screen.getByTestId("live-dot");
    expect(dot.textContent).toMatch(/stale/i);
    const inner = dot.querySelector("span[aria-hidden]")!;
    expect(inner.className).toMatch(/bg-bad/);
    expect(dot.getAttribute("title")).toMatch(/2m ago/);
  });

  it("polling status uses its own tooltip even when staleness is unknown", () => {
    render(<LiveDot status="polling" />);
    const dot = screen.getByTestId("live-dot");
    expect(dot.getAttribute("title")).toMatch(/polling fallback/i);
    expect(dot.textContent).toMatch(/polling/i);
  });

  it("error status surfaces the retry tooltip", () => {
    render(<LiveDot status="error" />);
    const dot = screen.getByTestId("live-dot");
    expect(dot.getAttribute("title")).toMatch(/errored.*retrying/i);
    expect(dot.textContent).toMatch(/error/i);
  });

  it("respects an explicit label override", () => {
    render(<LiveDot status="open" stalenessSeconds={200} label="reconnecting" />);
    const dot = screen.getByTestId("live-dot");
    // Label override takes effect even when staleness would have set its own.
    expect(dot.textContent).toMatch(/reconnecting/);
  });

  it("fmtAgo: minute-bucket boundary uses 'm ago'", () => {
    render(<LiveDot status="open" stalenessSeconds={150} />);
    const dot = screen.getByTestId("live-dot");
    expect(dot.getAttribute("title")).toMatch(/2m ago/);
  });

  it("fmtAgo: hour-bucket boundary uses 'h ago'", () => {
    render(<LiveDot status="open" stalenessSeconds={7200} />);
    const dot = screen.getByTestId("live-dot");
    expect(dot.getAttribute("title")).toMatch(/2h ago/);
  });
});
