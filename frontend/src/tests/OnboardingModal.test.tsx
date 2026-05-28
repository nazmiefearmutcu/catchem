import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import {
  ONBOARDING_STEPS,
  ONBOARDING_STORAGE_KEY,
  OnboardingModal,
} from "@/components/OnboardingModal";
import { __resetOverlayStateForTests } from "@/context/overlayCoordinator";

/**
 * First-run onboarding modal tests.
 *
 * Contract:
 *  - Shows on first run (storage flag absent).
 *  - Hidden when the "completed" flag is already present.
 *  - Dismissing (Skip X or "Get started") writes the flag and hides the modal.
 *
 * jsdom doesn't ship localStorage by default and the suite-wide setup leaves
 * storage shims to individual tests, so we install an in-memory shim here —
 * same pattern as notificationCenter.test.tsx / CommandPalette.test.tsx.
 */
function installLocalStorage(): Storage {
  const store = new Map<string, string>();
  const shim: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (k) => (store.has(k) ? store.get(k)! : null),
    key: (i) => Array.from(store.keys())[i] ?? null,
    removeItem: (k) => {
      store.delete(k);
    },
    setItem: (k, v) => {
      store.set(k, String(v));
    },
  };
  Object.defineProperty(window, "localStorage", { value: shim, configurable: true });
  return shim;
}

describe("onboarding modal", () => {
  beforeEach(() => {
    installLocalStorage();
    __resetOverlayStateForTests();
    // The modal moves focus on a setTimeout(0); run real timers so the
    // deferred focus call doesn't leak across tests.
    vi.useFakeTimers();
  });

  afterEach(() => {
    act(() => {
      vi.runOnlyPendingTimers();
    });
    vi.useRealTimers();
    __resetOverlayStateForTests();
  });

  it("renders on first run when the completed flag is absent", () => {
    expect(window.localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBeNull();
    render(<OnboardingModal />);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByTestId("onboarding-card")).toBeInTheDocument();
    // First step content is shown.
    expect(screen.getByText(ONBOARDING_STEPS[0].title)).toBeInTheDocument();
    expect(screen.getByTestId("onboarding-skip")).toBeInTheDocument();
  });

  it("stays hidden when the completed flag is already set", () => {
    window.localStorage.setItem(ONBOARDING_STORAGE_KEY, "true");
    render(<OnboardingModal />);

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByTestId("onboarding-card")).toBeNull();
  });

  it("renders one dot per step with the first active", () => {
    render(<OnboardingModal />);
    const dots = screen.getByTestId("onboarding-dots");
    expect(dots.querySelectorAll("[role='tab']").length).toBe(ONBOARDING_STEPS.length);
    expect(screen.getByTestId("onboarding-dot-0")).toHaveAttribute("data-active", "1");
    expect(screen.getByTestId("onboarding-dot-1")).toHaveAttribute("data-active", "0");
  });

  it("dismissing via the Skip X writes the flag and hides the modal", () => {
    render(<OnboardingModal />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("onboarding-skip"));

    expect(window.localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBe("true");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("advances through steps and 'Get started' on the final step writes the flag + closes", () => {
    render(<OnboardingModal />);
    // Step 1 → Next is shown, finish is not.
    expect(screen.getByTestId("onboarding-next")).toBeInTheDocument();
    expect(screen.queryByTestId("onboarding-finish")).toBeNull();

    // Click Next until the last step.
    for (let i = 0; i < ONBOARDING_STEPS.length - 1; i += 1) {
      fireEvent.click(screen.getByTestId("onboarding-next"));
    }

    const finish = screen.getByTestId("onboarding-finish");
    expect(finish).toHaveTextContent(/get started/i);
    expect(window.localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBeNull();

    fireEvent.click(finish);
    expect(window.localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBe("true");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Back is disabled on the first step and clicking a dot jumps to that step", () => {
    render(<OnboardingModal />);
    expect(screen.getByTestId("onboarding-prev")).toBeDisabled();

    fireEvent.click(screen.getByTestId("onboarding-dot-2"));
    expect(screen.getByText(ONBOARDING_STEPS[2].title)).toBeInTheDocument();
    expect(screen.getByTestId("onboarding-prev")).not.toBeDisabled();
  });

  it("ArrowRight / ArrowLeft step through the flow", () => {
    render(<OnboardingModal />);
    expect(screen.getByText(ONBOARDING_STEPS[0].title)).toBeInTheDocument();

    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight" }));
    });
    expect(screen.getByText(ONBOARDING_STEPS[1].title)).toBeInTheDocument();

    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft" }));
    });
    expect(screen.getByText(ONBOARDING_STEPS[0].title)).toBeInTheDocument();
  });
});
