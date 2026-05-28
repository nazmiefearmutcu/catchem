import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { SignalExplainer } from "@/components/SignalExplainer";
import { __resetOverlayStateForTests } from "@/context/overlayCoordinator";

/**
 * SignalExplainer is the inline "?" popover next to numeric quant-signal
 * badges. It pulls its description from JARGON and its formula/example
 * from SIGNAL_FORMULAS (both real dicts — no mock). These tests pin the
 * toggle behaviour, the data-driven sections, prop overrides, the
 * empty-state fallback, and the global click-outside / overlay close path.
 *
 * "z-score" is the canonical term that exists in BOTH dictionaries, so it
 * exercises description + formula + example in one render.
 */

beforeEach(() => {
  __resetOverlayStateForTests();
});

afterEach(() => {
  vi.clearAllMocks();
  __resetOverlayStateForTests();
});

describe("SignalExplainer", () => {
  it("renders the trigger button (with children) but no dialog until opened", () => {
    render(
      <SignalExplainer term="z-score">
        <span>z-score</span>
      </SignalExplainer>,
    );
    // Children render alongside the trigger.
    expect(screen.getByText("z-score")).toBeInTheDocument();
    // Accessible trigger label is derived from the term.
    expect(
      screen.getByRole("button", { name: "Explain z-score" }),
    ).toHaveAttribute("aria-expanded", "false");
    // Closed by default — popover dialog is absent.
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens the popover on click and surfaces description + formula + example from the dicts", () => {
    render(<SignalExplainer term="z-score" />);
    fireEvent.click(screen.getByRole("button", { name: "Explain z-score" }));

    const dialog = screen.getByRole("dialog", { name: "z-score explanation" });
    expect(dialog).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Explain z-score" }),
    ).toHaveAttribute("aria-expanded", "true");

    // Description (from JARGON), Formula + Example (from SIGNAL_FORMULAS).
    expect(
      screen.getByText(/how many standard deviations a value is from the mean/i),
    ).toBeInTheDocument();
    expect(screen.getByText("Formula")).toBeInTheDocument();
    expect(screen.getByText("(value - mean) / std_dev")).toBeInTheDocument();
    expect(screen.getByText("Example")).toBeInTheDocument();
    expect(screen.getByText(/12\/min burst has z =/i)).toBeInTheDocument();
  });

  it("toggles closed when the trigger is clicked a second time", () => {
    render(<SignalExplainer term="z-score" />);
    const trigger = screen.getByRole("button", { name: "Explain z-score" });

    fireEvent.click(trigger);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.click(trigger);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("prefers explicit formula/example props over the SIGNAL_FORMULAS lookup", () => {
    render(
      <SignalExplainer
        term="z-score"
        formula="custom = a / b"
        example="custom worked example"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Explain z-score" }));

    expect(screen.getByText("custom = a / b")).toBeInTheDocument();
    expect(screen.getByText("custom worked example")).toBeInTheDocument();
    // The dict default must NOT also render once a prop overrides it.
    expect(screen.queryByText("(value - mean) / std_dev")).toBeNull();
  });

  it("shows the graceful fallback when the term is unknown to every dictionary", () => {
    render(<SignalExplainer term="totally-unknown-term" />);
    fireEvent.click(
      screen.getByRole("button", { name: "Explain totally-unknown-term" }),
    );

    expect(
      screen.getByText("No explanation available for this signal yet."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Formula")).toBeNull();
    expect(screen.queryByText("Example")).toBeNull();
  });

  it("closes on an outside mousedown (click-outside handler)", () => {
    render(
      <div>
        <SignalExplainer term="z-score" />
        <button type="button">outside</button>
      </div>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Explain z-score" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // mousedown anywhere outside the component's wrapper span dismisses it.
    act(() => {
      fireEvent.mouseDown(screen.getByRole("button", { name: "outside" }));
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
