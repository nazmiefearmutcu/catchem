import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { ExportMenu } from "@/components/ExportMenu";
import { __resetOverlayStateForTests } from "@/context/overlayCoordinator";

/**
 * ExportMenu is a dumb chip-styled picker for CSV / JSON downloads. The
 * parent supplies a `buildUrl(format)` resolver; the component owns only
 * the open/close popover and the rendered <a download> links. These tests
 * pin all three render flavors (menu, inline, single-format collapse),
 * the open/close lifecycle, and that each option resolves the correct
 * href + download attribute. No network — `buildUrl` is a spy.
 */

const buildUrl = (fmt: "csv" | "json") => `https://x.invalid/export.${fmt}`;

beforeEach(() => {
  __resetOverlayStateForTests();
});

afterEach(() => {
  vi.clearAllMocks();
  __resetOverlayStateForTests();
});

describe("ExportMenu", () => {
  it("renders a closed trigger chip by default (menu flavor)", () => {
    render(<ExportMenu buildUrl={buildUrl} testId="exp" />);
    const trigger = screen.getByTestId("exp");
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    // Default label.
    expect(trigger).toHaveTextContent("export");
    // Menu is not mounted until opened.
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("opens the menu on click and renders both format options with correct hrefs", () => {
    const spy = vi.fn(buildUrl);
    render(<ExportMenu buildUrl={spy} testId="exp" hint="filtered rows" />);

    fireEvent.click(screen.getByTestId("exp"));
    expect(screen.getByRole("menu")).toBeInTheDocument();
    expect(screen.getByTestId("exp")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("filtered rows")).toBeInTheDocument();

    const csv = screen.getByTestId("exp-csv");
    const json = screen.getByTestId("exp-json");
    expect(csv).toHaveAttribute("href", "https://x.invalid/export.csv");
    expect(json).toHaveAttribute("href", "https://x.invalid/export.json");
    expect(csv).toHaveAttribute("role", "menuitem");
    // buildUrl was consulted for every offered format.
    expect(spy).toHaveBeenCalledWith("csv");
    expect(spy).toHaveBeenCalledWith("json");
  });

  it("closes the menu when a format option is clicked", () => {
    render(<ExportMenu buildUrl={buildUrl} testId="exp" />);
    fireEvent.click(screen.getByTestId("exp"));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("exp-csv"));
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("toggles shut when the trigger is clicked twice", () => {
    render(<ExportMenu buildUrl={buildUrl} testId="exp" />);
    const trigger = screen.getByTestId("exp");

    fireEvent.click(trigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.click(trigger);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("closes on an outside mousedown (click-outside handler)", () => {
    render(
      <div>
        <ExportMenu buildUrl={buildUrl} testId="exp" />
        <button type="button">elsewhere</button>
      </div>,
    );
    fireEvent.click(screen.getByTestId("exp"));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    act(() => {
      fireEvent.mouseDown(screen.getByRole("button", { name: "elsewhere" }));
    });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("collapses to a single download link when only one format is offered", () => {
    render(
      <ExportMenu
        buildUrl={buildUrl}
        formats={["json"]}
        filenameHint="scan.json"
        testId="single"
      />,
    );
    // No popover trigger — it's a direct anchor.
    expect(screen.queryByRole("button")).toBeNull();
    const link = screen.getByTestId("single");
    expect(link.tagName).toBe("A");
    expect(link).toHaveAttribute("href", "https://x.invalid/export.json");
    expect(link).toHaveAttribute("download", "scan.json");
    expect(link).toHaveTextContent("JSON");
  });

  it("renders both anchors side-by-side in inline flavor (no popover)", () => {
    render(
      <ExportMenu buildUrl={buildUrl} inline label="download" testId="inl" />,
    );
    // Inline = both links visible immediately, no menu/button toggle.
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("download")).toBeInTheDocument();

    const csv = screen.getByTestId("inl-csv");
    const json = screen.getByTestId("inl-json");
    expect(csv).toHaveAttribute("href", "https://x.invalid/export.csv");
    expect(csv).toHaveTextContent("CSV");
    expect(json).toHaveAttribute("href", "https://x.invalid/export.json");
    expect(json).toHaveTextContent("JSON");
  });
});
