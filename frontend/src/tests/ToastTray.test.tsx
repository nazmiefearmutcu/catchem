import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, RouterProvider, createMemoryRouter } from "react-router-dom";
import {
  STICKY_SCORE_THRESHOLD,
  TOAST_TTL_BY_SEVERITY,
  dismissToast,
  pushToast,
  type ArrivalToast,
} from "@/hooks/useDesktopAlerts";
import { ToastTray } from "@/components/ToastTray";

/**
 * ToastTray is the top-right slide-in tray driven by the external toast
 * store (useDesktopAlerts). It renders nothing when empty, mounts a toast
 * pushed through `pushToast`, supports hover-pause + manual dismiss, and
 * auto-dismisses on a severity-aware timer (unless sticky).
 *
 * The toast queue is a module-global with no dedicated reset helper, so we
 * track the ids we push and `dismissToast` them between tests to keep each
 * case isolated.
 */

const pushedIds = new Set<string>();

function push(toast: ArrivalToast) {
  pushedIds.add(toast.id);
  act(() => {
    pushToast(toast);
  });
}

function baseToast(overrides: Partial<ArrivalToast> = {}): ArrivalToast {
  return {
    id: "cap-1",
    title: "Apple earnings beat consensus",
    domain: "reuters.com",
    score: 0.6, // → "info" tone by default
    reasons: ["earnings"],
    symbols: ["AAPL"],
    ...overrides,
  };
}

function renderTray() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <ToastTray />
    </MemoryRouter>,
  );
}

describe("ToastTray", () => {
  beforeEach(() => {
    pushedIds.clear();
  });

  afterEach(() => {
    // Drain anything still in the queue so the next test starts empty.
    act(() => {
      pushedIds.forEach((id) => dismissToast(id));
    });
    pushedIds.clear();
    vi.useRealTimers();
  });

  it("renders nothing when the queue is empty", () => {
    const { container } = renderTray();
    expect(container.firstChild).toBeNull();
    expect(screen.queryByLabelText(/high-relevance arrivals/i)).toBeNull();
  });

  it("renders a toast pushed through the store", () => {
    renderTray();
    push(baseToast());
    expect(screen.getByLabelText(/high-relevance arrivals/i)).toBeInTheDocument();
    expect(screen.getByText("Apple earnings beat consensus")).toBeInTheDocument();
    expect(screen.getByText("reuters.com")).toBeInTheDocument();
    // score is formatted to two decimals.
    expect(screen.getByText(/score 0\.60/)).toBeInTheDocument();
    // reason + symbol pills render.
    expect(screen.getByText("earnings")).toBeInTheDocument();
    expect(screen.getByText("AAPL")).toBeInTheDocument();
  });

  it("exposes a polite status region for a normal toast", () => {
    renderTray();
    push(baseToast());
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
  });

  it("uses an assertive live region for critical (sticky) toasts", () => {
    renderTray();
    push(baseToast({ id: "crit-1", score: STICKY_SCORE_THRESHOLD + 0.05 }));
    expect(screen.getByText(/critical arrival/i)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "assertive");
  });

  it("auto-dismisses a non-sticky toast after its severity TTL", () => {
    vi.useFakeTimers();
    renderTray();
    push(baseToast({ id: "ttl-1", score: 0.6 })); // info → 4000ms
    expect(screen.getByText("Apple earnings beat consensus")).toBeInTheDocument();

    // Advance past the info TTL plus the 220ms exit-cleanup window.
    act(() => {
      vi.advanceTimersByTime(TOAST_TTL_BY_SEVERITY.info + 300);
    });
    expect(screen.queryByText("Apple earnings beat consensus")).toBeNull();
  });

  it("does NOT auto-dismiss a sticky (critical) toast", () => {
    vi.useFakeTimers();
    renderTray();
    push(baseToast({ id: "sticky-1", title: "Critical breaking news", score: 0.97 }));
    expect(screen.getByText("Critical breaking news")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(TOAST_TTL_BY_SEVERITY.error + 5000);
    });
    // Still on screen — sticky toasts wait for explicit dismissal.
    expect(screen.getByText("Critical breaking news")).toBeInTheDocument();
  });

  it("pauses the auto-dismiss timer while hovered", () => {
    vi.useFakeTimers();
    renderTray();
    push(baseToast({ id: "hover-1", score: 0.6 })); // info → 4000ms
    const region = screen.getByRole("status");

    // Hover before the TTL elapses → timer is cleared.
    act(() => {
      fireEvent.mouseEnter(region);
    });
    act(() => {
      vi.advanceTimersByTime(TOAST_TTL_BY_SEVERITY.info + 1000);
    });
    // Still visible because the dismiss timer was paused on hover.
    expect(screen.getByText("Apple earnings beat consensus")).toBeInTheDocument();
  });

  it("dismisses when the × button is clicked", () => {
    vi.useFakeTimers();
    renderTray();
    push(baseToast({ id: "x-1" }));
    expect(screen.getByText("Apple earnings beat consensus")).toBeInTheDocument();

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    });
    // beginDismiss keeps the node alive for a ~220ms slide-out window; flush it.
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(screen.queryByText("Apple earnings beat consensus")).toBeNull();
  });

  it("navigates to /feed/<id> when the body is clicked", () => {
    const router = createMemoryRouter([{ path: "*", element: <ToastTray /> }], {
      initialEntries: ["/"],
    });
    render(<RouterProvider router={router} />);

    push(baseToast({ id: "nav-cap-1", title: "Open me" }));
    // The clickable body button's accessible name is its text content; match
    // on the toast title rather than the (non-naming) title attribute.
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /open me/i }));
    });
    expect(router.state.location.pathname).toBe("/feed/nav-cap-1");
  });

  it("renders multiple queued toasts together", () => {
    renderTray();
    push(baseToast({ id: "multi-a", title: "First arrival" }));
    push(baseToast({ id: "multi-b", title: "Second arrival" }));
    expect(screen.getByText("First arrival")).toBeInTheDocument();
    expect(screen.getByText("Second arrival")).toBeInTheDocument();
    expect(screen.getAllByRole("status").length).toBe(2);
  });
});
