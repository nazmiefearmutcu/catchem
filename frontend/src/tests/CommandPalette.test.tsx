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
import {
  CommandPalette,
  OPEN_COMMAND_PALETTE_EVENT,
} from "@/components/CommandPalette";

/**
 * Command-palette hardening tests:
 *  - Opens from the global keyboard shortcut.
 *  - Opens from the explicit app event.
 *  - Closes cleanly when the route changes.
 */

const RECENT_KEY = "catchem.palette.recent";

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
        <input aria-label="typing-field" defaultValue="x" />
        <QueryClientProvider client={qc}>
          <CommandPalette />
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
  window.localStorage.removeItem(RECENT_KEY);
  vi.clearAllMocks();
});

describe("command palette", () => {
  it("opens with ⌘K and closes from the close button", () => {
    renderShell();
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }));
    });
    const dialog = screen.getByRole("dialog", { name: "Command palette" });
    expect(dialog).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /close command palette/i }));
    expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull();
  });

  it("does not open with ⌘K when focus is on text input", () => {
    renderShell();
    const input = screen.getByLabelText("typing-field");
    input.focus();
    fireEvent.keyDown(input, { key: "k", metaKey: true });
    expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull();
  });

  it("opens on the dedicated app event", () => {
    renderShell();
    act(() => {
      window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT));
    });
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();
  });

  it("stays open and clears query when Escape is pressed with non-empty input", () => {
    renderShell();
    act(() => {
      window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT));
    });
    const input = screen.getByLabelText("Command query");
    act(() => {
      fireEvent.change(input, { target: { value: "AAPL" } });
    });
    expect(input).toHaveValue("AAPL");

    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();
    expect(input).toHaveValue("");
  });

  it("closes on route change", () => {
    const { router } = renderShellWithRouter();

    act(() => {
      window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT));
    });
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();

    act(() => {
      fireEvent.click(screen.getByTestId("route-next"));
    });
    expect(router.state.location.pathname).toBe("/analysis");
    expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull();
  });

  it("jumps to symbol page when Enter is pressed with a valid symbol query", () => {
    const { router } = renderShellWithRouter();

    act(() => {
      window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT));
    });
    const input = screen.getByLabelText("Command query");
    fireEvent.change(input, { target: { value: "$aapl" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(router.state.location.pathname).toBe("/symbols/AAPL");
    expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull();
  });

  it("keeps palette open for invalid symbol text when selected row is missing", () => {
    const { router } = renderShellWithRouter();

    act(() => {
      window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT));
    });
    const input = screen.getByLabelText("Command query");
    fireEvent.change(input, { target: { value: "AAPL!" } });
    fireEvent.keyDown(input, { key: "End" });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(router.state.location.pathname).toBe("/");
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();
  });

  describe("Accessibility (O-11)", () => {
    it("enforces combobox and listbox accessibility contracts", () => {
      renderShell();
      
      // Open the palette
      act(() => {
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }));
      });
      
      const input = screen.getByLabelText("Command query") as HTMLInputElement;
      
      // Verify initial combobox attributes on input
      expect(input).toHaveAttribute("role", "combobox");
      expect(input).toHaveAttribute("aria-autocomplete", "list");
      expect(input).toHaveAttribute("aria-expanded", "true");
      expect(input).toHaveAttribute("aria-controls", "command-palette-listbox");
      expect(input).toHaveAttribute("aria-describedby", "command-palette-instructions");
      
      // Verify instruction element exists and describes list navigation
      const instructions = document.getElementById("command-palette-instructions");
      expect(instructions).toBeInTheDocument();
      expect(instructions?.textContent).toContain("arrow keys");

      // Check listbox role and id
      const listbox = screen.getByRole("listbox");
      expect(listbox).toHaveAttribute("id", "command-palette-listbox");

      // Check option list attributes
      const options = screen.getAllByRole("option");
      expect(options.length).toBeGreaterThan(0);

      // Verify Option 0 attributes
      expect(options[0]).toHaveAttribute("id", "command-palette-option-0");
      expect(options[0]).toHaveAttribute("aria-selected", "true");
      expect(options[0]).toHaveAttribute("tabIndex", "-1");
      expect(options[0]).toHaveAttribute("aria-label");

      // Navigate to next option and verify active descendant updates
      act(() => {
        fireEvent.keyDown(input, { key: "ArrowDown" });
      });
      expect(input).toHaveAttribute("aria-activedescendant", "command-palette-option-1");
      expect(options[1]).toHaveAttribute("aria-selected", "true");
    });

    it("applies correct focus outline and visible ring CSS classes to interactive controls", () => {
      renderShell();
      
      // Open the palette
      act(() => {
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }));
      });
      
      const input = screen.getByLabelText("Command query") as HTMLInputElement;
      const inputWrapper = input.parentElement;
      expect(inputWrapper).toHaveClass("focus-within:ring-1");

      const closeBtn = screen.getByLabelText("Close command palette");
      expect(closeBtn).toHaveClass("focus-visible:ring-accent");
    });
  });
});
