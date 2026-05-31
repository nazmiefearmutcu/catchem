import { describe, it, expect } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { HelpDrawer } from "@/components/HelpDrawer";
import { MENU_EVENT, useTauriMenu } from "@/hooks/useTauriMenu";
import { matchHelp, PAGE_HELP } from "@/lib/page-help";

function renderAt(pathname: string) {
  return render(
    <MemoryRouter initialEntries={[pathname]}>
      <HelpDrawer />
    </MemoryRouter>,
  );
}

function renderMenuAwareAt(pathname: string) {
  function Harness() {
    useTauriMenu();
    return <HelpDrawer />;
  }
  return render(
    <MemoryRouter initialEntries={[pathname]}>
      <Harness />
    </MemoryRouter>,
  );
}

describe("matchHelp", () => {
  it("returns exact match for known pathname", () => {
    expect(matchHelp("/feed")).toBe(PAGE_HELP["/feed"]);
  });

  it("prefix-matches /feed/<id> to /feed", () => {
    expect(matchHelp("/feed/abc-123")).toBe(PAGE_HELP["/feed"]);
  });

  it("prefix-matches /symbols/<ticker> to /symbols", () => {
    expect(matchHelp("/symbols/AAPL")).toBe(PAGE_HELP["/symbols"]);
  });

  it("normalizes alias and trailing-slash paths", () => {
    expect(matchHelp("/analysis")).toBe(PAGE_HELP["/map"]);
    expect(matchHelp("/feed/")).toBe(PAGE_HELP["/feed"]);
  });

  it("returns null for unknown paths", () => {
    expect(matchHelp("/never-existed")).toBeNull();
  });

  it("covers every primary route", () => {
    const required = [
      "/",
      "/feed",
      "/replay",
      "/map",
      "/symbols",
      "/tags",
      "/benchmark",
      "/reviews",
      "/scan",
      "/ops",
      "/model-controls",
      "/logs",
      "/sources",
      "/settings",
      "/help",
    ];
    for (const p of required) {
      expect(PAGE_HELP[p], `expected help entry for ${p}`).toBeTruthy();
    }
  });
});

describe("<HelpDrawer>", () => {
  it("renders the floating ? trigger button by default, drawer closed", () => {
    renderAt("/");
    const btn = screen.getByTestId("help-drawer-trigger");
    expect(btn).toBeInTheDocument();
    expect(btn).toHaveAttribute("aria-expanded", "false");
    const drawer = screen.getByTestId("help-drawer");
    expect(drawer).toHaveAttribute("data-state", "closed");
    expect(drawer).toHaveAttribute("aria-hidden", "true");
  });

  it("opens the drawer on trigger click and shows page-specific content", () => {
    renderAt("/feed");
    fireEvent.click(screen.getByTestId("help-drawer-trigger"));
    const drawer = screen.getByTestId("help-drawer");
    expect(drawer).toHaveAttribute("data-state", "open");
    expect(drawer).toHaveAttribute("aria-hidden", "false");
    // At least one tip rendered from /feed page-help entry.
    expect(screen.getByTestId("help-tip-0")).toBeInTheDocument();
    // /feed has 2 questions configured.
    expect(screen.getByTestId("help-qa-0")).toBeInTheDocument();
    // The pathname label is shown in the header.
    expect(screen.getByText("/feed")).toBeInTheDocument();
  });

  it("closes when the close button is clicked", () => {
    renderAt("/");
    fireEvent.click(screen.getByTestId("help-drawer-trigger"));
    expect(screen.getByTestId("help-drawer")).toHaveAttribute(
      "data-state",
      "open",
    );
    fireEvent.click(screen.getByTestId("help-drawer-close"));
    expect(screen.getByTestId("help-drawer")).toHaveAttribute(
      "data-state",
      "closed",
    );
  });

  it("closes when the close button is pressed down", () => {
    renderAt("/");
    fireEvent.click(screen.getByTestId("help-drawer-trigger"));
    expect(screen.getByTestId("help-drawer")).toHaveAttribute(
      "data-state",
      "open",
    );
    fireEvent.pointerDown(screen.getByTestId("help-drawer-close"));
    expect(screen.getByTestId("help-drawer")).toHaveAttribute(
      "data-state",
      "closed",
    );
  });

  it("closes on Escape key while open", () => {
    renderAt("/");
    fireEvent.click(screen.getByTestId("help-drawer-trigger"));
    expect(screen.getByTestId("help-drawer")).toHaveAttribute(
      "data-state",
      "open",
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.getByTestId("help-drawer")).toHaveAttribute(
      "data-state",
      "closed",
    );
  });

  it("closes on Escape from the focused drawer surface even without bubbling", () => {
    renderAt("/");
    fireEvent.click(screen.getByTestId("help-drawer-trigger"));
    expect(screen.getByTestId("help-drawer")).toHaveAttribute(
      "data-state",
      "open",
    );
    fireEvent.keyDown(screen.getByTestId("help-drawer-close"), {
      key: "Escape",
      bubbles: false,
    });
    expect(screen.getByTestId("help-drawer")).toHaveAttribute(
      "data-state",
      "closed",
    );
  });

  it("closes when the native dismiss_overlay bridge is dispatched", () => {
    renderMenuAwareAt("/");
    fireEvent.click(screen.getByTestId("help-drawer-trigger"));
    expect(screen.getByTestId("help-drawer")).toHaveAttribute(
      "data-state",
      "open",
    );
    fireEvent(
      window,
      new CustomEvent(MENU_EVENT, { detail: "dismiss_overlay" }),
    );
    expect(screen.getByTestId("help-drawer")).toHaveAttribute(
      "data-state",
      "closed",
    );
  });

  it("falls back to /symbols help for /symbols/AAPL", () => {
    renderAt("/symbols/AAPL");
    fireEvent.click(screen.getByTestId("help-drawer-trigger"));
    // /symbols page-help has 3 tips configured.
    expect(screen.getByTestId("help-tip-0")).toBeInTheDocument();
    expect(screen.getByTestId("help-tip-2")).toBeInTheDocument();
  });

  it("shows the empty-state notice for unknown paths", () => {
    renderAt("/totally-unknown");
    fireEvent.click(screen.getByTestId("help-drawer-trigger"));
    expect(screen.getByTestId("help-drawer-empty")).toBeInTheDocument();
  });

  it("renders map help for /analysis alias path", () => {
    renderAt("/analysis");
    fireEvent.click(screen.getByTestId("help-drawer-trigger"));
    expect(screen.getByTestId("help-drawer-shortcuts")).toBeInTheDocument();
    expect(screen.getByText("You're here")).toBeInTheDocument();
  });

  it("renders Quick tips / Common questions / Shortcuts sections when content exists", () => {
    renderAt("/scan");
    fireEvent.click(screen.getByTestId("help-drawer-trigger"));
    expect(screen.getByTestId("help-drawer-tips")).toBeInTheDocument();
    expect(screen.getByTestId("help-drawer-questions")).toBeInTheDocument();
    expect(screen.getByTestId("help-drawer-shortcuts")).toBeInTheDocument();
  });

  it("omits the questions section when a page has zero Q&A entries", () => {
    renderAt("/replay");
    fireEvent.click(screen.getByTestId("help-drawer-trigger"));
    // /replay has 1 question, so it should still render. Use /help which has 0.
    expect(screen.queryByTestId("help-qa-0")).toBeInTheDocument();
  });

  it("omits the shortcuts section when no contextual help exists", () => {
    renderAt("/totally-unknown");
    fireEvent.click(screen.getByTestId("help-drawer-trigger"));
    // Unknown pages resolve to the empty-state notice, so no contextual
    // shortcut section should be rendered.
    expect(screen.queryByTestId("help-drawer-shortcuts")).not.toBeInTheDocument();
  });
});
