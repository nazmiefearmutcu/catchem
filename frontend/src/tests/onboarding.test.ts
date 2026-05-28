import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  ONBOARDING_STORAGE_KEY,
  OPEN_ONBOARDING_EVENT,
  hasSeenOnboarding,
  markOnboardingSeen,
  resetOnboarding,
  requestOpenOnboarding,
} from "@/lib/onboarding";

/**
 * Unit tests for the onboarding single-source module.
 *
 * jsdom doesn't ship localStorage by default and the suite-wide setup
 * leaves storage shims to individual tests — install an in-memory shim,
 * same pattern as OnboardingModal.test.tsx / CommandPalette.test.tsx.
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

describe("lib/onboarding", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  it("hasSeenOnboarding is false until the flag is written", () => {
    expect(hasSeenOnboarding()).toBe(false);
    markOnboardingSeen();
    expect(hasSeenOnboarding()).toBe(true);
    expect(window.localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBe("true");
  });

  it("resetOnboarding clears the flag so the tour shows again", () => {
    markOnboardingSeen();
    expect(hasSeenOnboarding()).toBe(true);
    resetOnboarding();
    expect(hasSeenOnboarding()).toBe(false);
    expect(window.localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBeNull();
  });

  it("treats storage failure as 'seen' so we never nag", () => {
    // Simulate Safari private mode / disabled storage: getItem throws.
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("storage disabled");
      },
    });
    expect(hasSeenOnboarding()).toBe(true);
    // write/reset must swallow the error rather than crash the app.
    expect(() => markOnboardingSeen()).not.toThrow();
    expect(() => resetOnboarding()).not.toThrow();
  });

  it("requestOpenOnboarding dispatches the open event", () => {
    const spy = vi.fn();
    window.addEventListener(OPEN_ONBOARDING_EVENT, spy);
    requestOpenOnboarding();
    window.removeEventListener(OPEN_ONBOARDING_EVENT, spy);
    expect(spy).toHaveBeenCalledTimes(1);
  });
});
