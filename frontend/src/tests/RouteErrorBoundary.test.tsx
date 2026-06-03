import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { RouteErrorBoundary } from "@/components/RouteErrorBoundary";
import { AppErrorBoundary } from "@/components/AppErrorBoundary";

/** Throws on first render; flip the ref to recover. */
function Boom({ live }: { live: { throw: boolean } }) {
  if (live.throw) throw new Error("kaboom");
  return <div>recovered</div>;
}

describe("RouteErrorBoundary", () => {
  beforeEach(() => {
    // Silence the React-built-in error-overlay output for this suite —
    // throwing is the contract, not a real failure.
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders children when no error is thrown", () => {
    render(
      <MemoryRouter>
        <RouteErrorBoundary>
          <div>page content</div>
        </RouteErrorBoundary>
      </MemoryRouter>
    );
    expect(screen.getByText("page content")).toBeInTheDocument();
  });

  it("shows the fallback when a child throws", () => {
    const ref = { throw: true };
    render(
      <MemoryRouter>
        <RouteErrorBoundary>
          <Boom live={ref} />
        </RouteErrorBoundary>
      </MemoryRouter>
    );
    expect(screen.getByText(/this page hit an unexpected error/i)).toBeInTheDocument();
    expect(screen.getByText(/page crash/i)).toBeInTheDocument();
  });

  it("exposes a role=alert region for screen readers", () => {
    const ref = { throw: true };
    render(
      <MemoryRouter>
        <RouteErrorBoundary>
          <Boom live={ref} />
        </RouteErrorBoundary>
      </MemoryRouter>
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("offers a Retry button that resets the boundary", () => {
    const ref = { throw: true };
    render(
      <MemoryRouter>
        <RouteErrorBoundary>
          <Boom live={ref} />
        </RouteErrorBoundary>
      </MemoryRouter>
    );
    // Stop throwing before pressing Retry — clicking Retry remounts children.
    ref.throw = false;
    fireEvent.click(screen.getByRole("button", { name: /retry this page/i }));
    expect(screen.getByText("recovered")).toBeInTheDocument();
  });

  it("exposes a Back to overview link to '/'", () => {
    const ref = { throw: true };
    render(
      <MemoryRouter>
        <RouteErrorBoundary>
          <Boom live={ref} />
        </RouteErrorBoundary>
      </MemoryRouter>
    );
    const link = screen.getByRole("link", { name: /back to overview/i });
    expect(link).toHaveAttribute("href", "/");
  });

  it("implements custom focus-visible ring styles on all interactive controls for keyboard navigation", () => {
    const ref = { throw: true };
    render(
      <MemoryRouter>
        <RouteErrorBoundary>
          <Boom live={ref} />
        </RouteErrorBoundary>
      </MemoryRouter>
    );

    const summary = screen.getByText(/show error details/i);
    const retryBtn = screen.getByRole("button", { name: /retry this page/i });
    const backLink = screen.getByRole("link", { name: /back to overview/i });
    const copyBtn = screen.getByRole("button", { name: /copy diagnostics/i });

    expect(summary).toHaveClass("focus:outline-none");
    expect(summary).toHaveClass("focus-visible:ring-1");
    expect(summary).toHaveClass("focus-visible:ring-accent");

    expect(retryBtn).toHaveClass("focus:outline-none");
    expect(retryBtn).toHaveClass("focus-visible:ring-1");
    expect(retryBtn).toHaveClass("focus-visible:ring-accent");

    expect(backLink).toHaveClass("focus:outline-none");
    expect(backLink).toHaveClass("focus-visible:ring-1");
    expect(backLink).toHaveClass("focus-visible:ring-accent");

    expect(copyBtn).toHaveClass("focus:outline-none");
    expect(copyBtn).toHaveClass("focus-visible:ring-1");
    expect(copyBtn).toHaveClass("focus-visible:ring-accent");
  });
});

describe("AppErrorBoundary", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders children when no error is thrown", () => {
    render(
      <AppErrorBoundary>
        <div>app shell</div>
      </AppErrorBoundary>
    );
    expect(screen.getByText("app shell")).toBeInTheDocument();
  });

  it("shows the fatal-fallback with a single Reload button", () => {
    const ref = { throw: true };
    render(
      <AppErrorBoundary>
        <Boom live={ref} />
      </AppErrorBoundary>
    );
    expect(screen.getByText(/catchem crashed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reload app/i })).toBeInTheDocument();
    // App-level boundary deliberately omits in-app nav (Shell may itself be down).
    expect(screen.queryByRole("link", { name: /back to overview/i })).not.toBeInTheDocument();
  });
});
