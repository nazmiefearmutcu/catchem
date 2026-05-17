import { describe, it, expect, beforeEach } from "vitest";
import { readDesktopAlertState, toggleDesktopAlerts } from "@/hooks/useDesktopAlerts";

// jsdom in this project ships without a full Storage implementation, so we
// install our own minimal shim before each test.
function installLocalStorage(): Storage {
  const store = new Map<string, string>();
  const shim: Storage = {
    get length() { return store.size; },
    clear: () => store.clear(),
    getItem: (k) => (store.has(k) ? store.get(k)! : null),
    key: (i) => Array.from(store.keys())[i] ?? null,
    removeItem: (k) => { store.delete(k); },
    setItem: (k, v) => { store.set(k, String(v)); },
  };
  Object.defineProperty(window, "localStorage", { value: shim, configurable: true });
  return shim;
}

describe("desktop alerts (in-app toast toggle)", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  it("defaults to 'on' when nothing is persisted", () => {
    expect(readDesktopAlertState()).toBe("on");
  });

  it("returns 'off' once the user has muted", () => {
    toggleDesktopAlerts(false);
    expect(readDesktopAlertState()).toBe("off");
  });

  it("toggleDesktopAlerts(false) persists '0' so the choice survives reload", () => {
    toggleDesktopAlerts(false);
    expect(window.localStorage.getItem("catchem:arrival-toasts-enabled")).toBe("0");
  });

  it("toggleDesktopAlerts(true) clears the storage key (default state)", () => {
    toggleDesktopAlerts(false);
    expect(window.localStorage.getItem("catchem:arrival-toasts-enabled")).toBe("0");
    toggleDesktopAlerts(true);
    expect(window.localStorage.getItem("catchem:arrival-toasts-enabled")).toBeNull();
  });

  it("toggle returns the new state synchronously", () => {
    expect(toggleDesktopAlerts(false)).toBe("off");
    expect(toggleDesktopAlerts(true)).toBe("on");
  });
});
