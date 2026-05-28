import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, render, screen, fireEvent } from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  RouterProvider,
  Routes,
  createMemoryRouter,
  useNavigate,
} from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CommandPalette, OPEN_COMMAND_PALETTE_EVENT } from "@/components/CommandPalette";
import { ShortcutOverlay } from "@/components/ShortcutOverlay";

/**
 * Shortcut overlay contract:
 *  - Opens via "?".
 *  - Closes on route transitions.
 *  - Opening by app event clears already-open overlays first.
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

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderShell() {
  const qc = makeClient();
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route
          path="*"
          element={
            <QueryClientProvider client={qc}>
              <input aria-label="typing-field" defaultValue="x" />
              <ShortcutOverlay />
              <CommandPalette />
            </QueryClientProvider>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

function renderShellWithRouter() {
  const qc = makeClient();
  function Shell() {
    const nav = useNavigate();
    return (
      <>
        <button type="button" data-testid="route-next" onClick={() => nav("/analysis")}>
          go analysis
        </button>
        <QueryClientProvider client={qc}>
          <ShortcutOverlay />
        </QueryClientProvider>
      </>
    );
  }
  const router = createMemoryRouter(
    [{ path: "*", element: <Shell /> }],
    { initialEntries: ["/"] },
  );
  return {
    router,
    ...render(<RouterProvider router={router} />),
  };
}

beforeEach(() => {
  installLocalStorage();
  vi.clearAllMocks();
});

describe("shortcut overlay", () => {
  it("opens on '?' key", () => {
    renderShell();
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "?" }));
    });
    expect(screen.getByTestId("shortcut-overlay-card")).toBeInTheDocument();
  });

  it("does not open when focus is in text input", () => {
    renderShell();
    const input = screen.getByLabelText("typing-field");
    input.focus();
    fireEvent.keyDown(input, { key: "?" });
    expect(screen.queryByTestId("shortcut-overlay-card")).toBeNull();
  });

  it("closes on route change", () => {
    const { router } = renderShellWithRouter();
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "?" }));
    });
    expect(screen.getByTestId("shortcut-overlay-card")).toBeInTheDocument();

    act(() => {
      fireEvent.click(screen.getByTestId("route-next"));
    });
    expect(router.state.location.pathname).toBe("/analysis");
    expect(screen.queryByTestId("shortcut-overlay-card")).toBeNull();
  });

  it("opens when requested and closes other overlays first", () => {
    renderShell();
    act(() => {
      window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT));
    });
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new Event("catchem:open-shortcut-overlay"));
    });
    expect(screen.getByTestId("shortcut-overlay-card")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull();
  });
});
