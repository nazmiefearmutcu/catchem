import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { AppErrorBoundary } from "@/components/AppErrorBoundary";

/**
 * App-wide (fatal) error boundary tests.
 *
 * Contract:
 *  - Passes children through untouched when nothing throws.
 *  - Catches a throwing child and renders the fatal fallback (role=alert)
 *    instead of crashing the whole tree.
 *  - Deliberately omits in-app navigation (the Shell may itself be down);
 *    only Reload + Copy diagnostics are offered.
 *
 * Mirrors the throwing-child pattern from RouteErrorBoundary.test.tsx.
 */

/** Throws on render when the live ref says so. */
function Boom({ live }: { live: { throw: boolean } }) {
  if (live.throw) throw new Error("kaboom");
  return <div>recovered child</div>;
}

describe("AppErrorBoundary", () => {
  beforeEach(() => {
    // React + the boundary's componentDidCatch both log to console.error on
    // a caught throw; that's the contract here, not a real failure.
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders children when no error is thrown", () => {
    render(
      <AppErrorBoundary>
        <div>app shell content</div>
      </AppErrorBoundary>,
    );
    expect(screen.getByText("app shell content")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders the fatal fallback when a child throws (tree does not crash)", () => {
    const ref = { throw: true };
    render(
      <AppErrorBoundary>
        <Boom live={ref} />
      </AppErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(/catchem crashed/i)).toBeInTheDocument();
    expect(screen.getByText(/unrecoverable error/i)).toBeInTheDocument();
    // The thrown child must not leak through.
    expect(screen.queryByText("recovered child")).toBeNull();
  });

  it("offers Reload + Copy diagnostics but no in-app navigation", () => {
    const ref = { throw: true };
    render(
      <AppErrorBoundary>
        <Boom live={ref} />
      </AppErrorBoundary>,
    );
    expect(screen.getByRole("button", { name: /reload app/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy diagnostics/i })).toBeInTheDocument();
    // App-level boundary deliberately omits the route-level "back to overview" link.
    expect(screen.queryByRole("link", { name: /back to overview/i })).toBeNull();
  });

  it("surfaces the error name + message in the details disclosure", () => {
    const ref = { throw: true };
    render(
      <AppErrorBoundary>
        <Boom live={ref} />
      </AppErrorBoundary>,
    );
    expect(screen.getByText(/show error details/i)).toBeInTheDocument();
    expect(screen.getByText(/kaboom/)).toBeInTheDocument();
  });

  it("copies diagnostics to the clipboard when available", () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    const ref = { throw: true };
    render(
      <AppErrorBoundary>
        <Boom live={ref} />
      </AppErrorBoundary>,
    );
    fireEvent.click(screen.getByRole("button", { name: /copy diagnostics/i }));
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText.mock.calls[0][0]).toMatch(/Error: kaboom/);
  });

  it("implements custom focus-visible ring styles on all interactive controls for keyboard navigation", () => {
    const ref = { throw: true };
    render(
      <AppErrorBoundary>
        <Boom live={ref} />
      </AppErrorBoundary>,
    );

    const summary = screen.getByText(/show error details/i);
    const reloadBtn = screen.getByRole("button", { name: /reload app/i });
    const copyBtn = screen.getByRole("button", { name: /copy diagnostics/i });

    expect(summary).toHaveClass("focus:outline-none");
    expect(summary).toHaveClass("focus-visible:ring-1");
    expect(summary).toHaveClass("focus-visible:ring-accent");

    expect(reloadBtn).toHaveClass("focus:outline-none");
    expect(reloadBtn).toHaveClass("focus-visible:ring-1");
    expect(reloadBtn).toHaveClass("focus-visible:ring-accent");

    expect(copyBtn).toHaveClass("focus:outline-none");
    expect(copyBtn).toHaveClass("focus-visible:ring-1");
    expect(copyBtn).toHaveClass("focus-visible:ring-accent");
  });
});

