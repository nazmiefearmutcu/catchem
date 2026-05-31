import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, render, cleanup } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { useTauriMenu, MENU_EVENT } from "@/hooks/useTauriMenu";
import { OPEN_SHORTCUT_OVERLAY_EVENT } from "@/components/CommandPalette";

// A throwaway component so we can mount the hook in the React tree.
function Harness() {
  useTauriMenu();
  return null;
}

function dispatch(action: string): void {
  window.dispatchEvent(new CustomEvent(MENU_EVENT, { detail: action }));
}

const STORAGE_KEY = "catchem.theme";

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

describe("useTauriMenu — Rust menu -> React bridge", () => {
  let openSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    installLocalStorage();
    // Start every test in dark theme so the toggle's expected end state
    // is deterministic.
    window.localStorage.setItem(STORAGE_KEY, "dark");
    document.documentElement.classList.add("dark");
    openSpy = vi.fn();
    Object.defineProperty(window, "open", { value: openSpy, configurable: true });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    document.documentElement.classList.remove("dark");
  });

  it("export_db opens /api/db/export in a new tab", () => {
    render(<MemoryRouter><Harness /></MemoryRouter>);
    dispatch("export_db");
    expect(openSpy).toHaveBeenCalledWith("/api/db/export", "_blank");
  });

  it("api_docs opens /api/docs in a new tab", () => {
    render(<MemoryRouter><Harness /></MemoryRouter>);
    dispatch("api_docs");
    expect(openSpy).toHaveBeenCalledWith("/api/docs", "_blank");
  });

  it("toggle_theme flips the persisted theme from dark to light", () => {
    render(<MemoryRouter><Harness /></MemoryRouter>);
    // act() so the setTheme() state update + the localStorage-writing
    // useEffect both flush before we assert.
    act(() => { dispatch("toggle_theme"); });
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("light");
  });

  it("show_shortcuts redispatches the global shortcut-overlay event", () => {
    render(<MemoryRouter><Harness /></MemoryRouter>);
    const onOverlay = vi.fn();
    window.addEventListener(OPEN_SHORTCUT_OVERLAY_EVENT, onOverlay);
    dispatch("show_shortcuts");
    expect(onOverlay).toHaveBeenCalledTimes(1);
    window.removeEventListener(OPEN_SHORTCUT_OVERLAY_EVENT, onOverlay);
  });

  it("import_db navigates to /settings#database", () => {
    let observed: string | null = null;
    function Probe() {
      // Captured on every render — including the one after dispatch().
      const loc = useLocation();
      observed = `${loc.pathname}${loc.hash}`;
      return null;
    }
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Harness />
        <Probe />
      </MemoryRouter>
    );
    act(() => { dispatch("import_db"); });
    expect(observed).toBe("/settings#database");
  });

  it("new_window opens window.location.href in a new tab as a fallback", () => {
    render(<MemoryRouter><Harness /></MemoryRouter>);
    dispatch("new_window");
    expect(openSpy).toHaveBeenCalledTimes(1);
    // First arg is the current location.href; the harness runs in jsdom
    // where `window.location.href` is the test URL, so we assert the
    // shape (string + target + noopener) rather than the exact value.
    const [url, target, features] = openSpy.mock.calls[0] as [string, string, string];
    expect(typeof url).toBe("string");
    expect(target).toBe("_blank");
    expect(features).toBe("noopener");
  });

  it("file_open navigates to /replay", () => {
    let observed: string | null = null;
    function Probe() {
      const loc = useLocation();
      observed = loc.pathname;
      return null;
    }
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Harness />
        <Probe />
      </MemoryRouter>
    );
    act(() => { dispatch("file_open"); });
    expect(observed).toBe("/replay");
  });

  it("ignores unknown menu actions without throwing", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    render(<MemoryRouter><Harness /></MemoryRouter>);
    expect(() => dispatch("does_not_exist")).not.toThrow();
    expect(openSpy).not.toHaveBeenCalled();
    expect(warnSpy).toHaveBeenCalledWith("[useTauriMenu] unknown menu action:", "does_not_exist");
  });

  it("removes the event listener on unmount (no leak across remounts)", () => {
    const { unmount } = render(<MemoryRouter><Harness /></MemoryRouter>);
    unmount();
    dispatch("export_db");
    expect(openSpy).not.toHaveBeenCalled();
  });
});
