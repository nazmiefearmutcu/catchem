import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  RouterProvider,
  Routes,
  createMemoryRouter,
  useNavigate,
} from "react-router-dom";

import {
  SearchPalette,
  OPEN_SEARCH_PALETTE_EVENT,
  escapeRegExp,
  splitMatches,
  flattenResponse,
} from "@/components/SearchPalette";
import type { SearchResponse } from "@/lib/api";

/**
 * v26 (task #96) — pins the ⌘P content-search palette contract:
 *   - Pure helpers (escapeRegExp, splitMatches, flattenResponse) are
 *     deterministic and side-effect-free.
 *   - ⌘P opens + Esc closes; ⌘P preventDefaults the browser Print.
 *   - 200ms debounce + min_length=2 means no fetch on a single char or
 *     before the debounce window expires.
 *   - Empty + non-empty result states render appropriate strings.
 */

vi.mock("@/lib/api", () => ({
  api: {
    search: vi.fn(),
  },
}));

import { api } from "@/lib/api";

const apiMock = api as unknown as { search: ReturnType<typeof vi.fn> };

const EMPTY_RESP: SearchResponse = { query: "", records: [], symbols: [], clusters: [] };
const POPULATED_RESP: SearchResponse = {
  query: "tesla",
  records: [
    {
      capture_id: "cap-123",
      title: "Tesla beats deliveries forecast",
      domain: "bloomberg.com",
      score: 0.71,
      published_ts: "2026-05-28T12:00:00+00:00",
    },
  ],
  symbols: [{ symbol: "TSLA", count: 3 }],
  clusters: [
    {
      cluster_id: "deadbeefcafe1234",
      size: 2,
      symbols: ["TSLA", "GM"],
    },
  ],
};

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

function renderShell() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route path="*" element={<SearchPalette />} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderShellWithInput() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <input aria-label="typing-field" defaultValue="x" />
              <SearchPalette />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

function renderShellWithRouter(initialEntries: string[] = ["/"]) {
  function Shell() {
    const nav = useNavigate();
    return (
      <>
        <button type="button" data-testid="route-next" onClick={() => nav("/analysis")}>
          go analysis
        </button>
        <SearchPalette />
      </>
    );
  }
  const router = createMemoryRouter(
    [{ path: "*", element: <Shell /> }],
    { initialEntries },
  );
  return {
    router,
    ...render(<RouterProvider router={router} />),
  };
}

beforeEach(() => {
  installLocalStorage();
  vi.clearAllMocks();
  vi.useFakeTimers();
  apiMock.search.mockResolvedValue(EMPTY_RESP);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("escapeRegExp + splitMatches — pure helpers", () => {
  it("escapes regex metacharacters so user input is safe", () => {
    expect(escapeRegExp("a.b*c(d)")).toBe("a\\.b\\*c\\(d\\)");
    expect(escapeRegExp("$AAPL")).toBe("\\$AAPL");
  });

  it("splits a label into match / non-match segments (case-insensitive)", () => {
    const segs = splitMatches("Tesla beats deliveries", "tes");
    // "Tesla beats deliveries" → ["Tes", "la beats deliveries"]
    expect(segs[0]).toEqual({ text: "Tes", match: true });
    expect(segs[1].match).toBe(false);
  });

  it("returns the original string when the query is empty", () => {
    expect(splitMatches("anything", "")).toEqual([{ text: "anything", match: false }]);
  });

  it("does not crash on regex metachars in the query", () => {
    expect(() => splitMatches("price ($100)", "(")).not.toThrow();
    const segs = splitMatches("price ($100)", "(");
    expect(segs.some((s) => s.match && s.text === "(")).toBe(true);
  });
});

describe("flattenResponse — bucket → flat list", () => {
  it("returns empty for null", () => {
    expect(flattenResponse(null)).toEqual([]);
  });

  it("orders records → symbols → clusters", () => {
    const flat = flattenResponse(POPULATED_RESP);
    expect(flat.map((r) => r.kind)).toEqual(["record", "symbol", "cluster"]);
  });

  it("substitutes a fallback title for untitled records", () => {
    const resp: SearchResponse = {
      query: "x",
      records: [
        {
          capture_id: "abcdef1234567890",
          title: null,
          domain: null,
          score: null,
          published_ts: null,
        },
      ],
      symbols: [],
      clusters: [],
    };
    const flat = flattenResponse(resp);
    expect((flat[0] as { title: string }).title).toBe("(untitled abcdef12)");
  });
});

describe("⌘P toggles the palette + preventDefault stops browser Print", () => {
  it("⌘P opens; Esc closes", () => {
    renderShell();
    // Initially hidden.
    expect(screen.queryByTestId("search-palette")).toBeNull();
    // Open via ⌘P.
    act(() => {
      const event = new KeyboardEvent("keydown", { key: "p", metaKey: true });
      document.dispatchEvent(event);
    });
    expect(screen.getByTestId("search-palette")).toBeInTheDocument();
    // Close via Esc.
    act(() => {
      const esc = new KeyboardEvent("keydown", { key: "Escape" });
      document.dispatchEvent(esc);
    });
    expect(screen.queryByTestId("search-palette")).toBeNull();
  });

  it("does not open with ⌘P while typing in text input", () => {
    renderShellWithInput();
    const input = screen.getByLabelText("typing-field");
    input.focus();
    fireEvent.keyDown(input, { key: "p", metaKey: true });
    expect(screen.queryByTestId("search-palette")).toBeNull();
  });

  it("preventDefault always fires on ⌘P (browser Print never sneaks in)", () => {
    renderShell();
    let pd = false;
    const evt = new KeyboardEvent("keydown", { key: "p", metaKey: true, cancelable: true });
    // Spy on preventDefault — jsdom's KeyboardEvent doesn't track it
    // directly, so we wrap before dispatch.
    const origPD = evt.preventDefault.bind(evt);
    evt.preventDefault = () => {
      pd = true;
      origPD();
    };
    act(() => {
      document.dispatchEvent(evt);
    });
    expect(pd).toBe(true);
  });

  it("custom event also opens the palette (programmatic open path)", () => {
    renderShell();
    act(() => {
      window.dispatchEvent(new Event(OPEN_SEARCH_PALETTE_EVENT));
    });
    expect(screen.getByTestId("search-palette")).toBeInTheDocument();
  });

  it("closes when route path changes", () => {
    const { router } = renderShellWithRouter();

    act(() => {
      const event = new KeyboardEvent("keydown", { key: "p", metaKey: true });
      document.dispatchEvent(event);
    });
    expect(screen.getByTestId("search-palette")).toBeInTheDocument();

    act(() => {
      fireEvent.click(screen.getByTestId("route-next"));
    });
    expect(router.state.location.pathname).toBe("/analysis");
    expect(screen.queryByTestId("search-palette")).toBeNull();
  });
});

describe("debounce + min-length contract", () => {
  it("does not call api.search until 200ms after the last keystroke", async () => {
    renderShell();
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "p", metaKey: true }));
    });
    const input = screen.getByTestId("search-palette-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "tes" } });
    // Before 200ms elapse: no call.
    await act(async () => {
      vi.advanceTimersByTime(150);
    });
    expect(apiMock.search).not.toHaveBeenCalled();
    // After full window: exactly one call.
    await act(async () => {
      vi.advanceTimersByTime(60);
    });
    expect(apiMock.search).toHaveBeenCalledTimes(1);
    expect(apiMock.search).toHaveBeenCalledWith("tes", 20);
  });

  it("does NOT issue a request for a 1-char query (matches backend min_length=2)", async () => {
    renderShell();
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "p", metaKey: true }));
    });
    const input = screen.getByTestId("search-palette-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "x" } });
    await act(async () => {
      vi.advanceTimersByTime(500);
    });
    expect(apiMock.search).not.toHaveBeenCalled();
  });

  it("shows 'Searching…' state during fetch and clears it on resolve", async () => {
    let resolveFn: (value: SearchResponse) => void = () => {};
    apiMock.search.mockReturnValueOnce(
      new Promise<SearchResponse>((r) => {
        resolveFn = r;
      }),
    );
    renderShell();
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "p", metaKey: true }));
    });
    const input = screen.getByTestId("search-palette-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "tesla" } });
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(screen.getByTestId("search-palette-loading")).toBeInTheDocument();
    // Resolve the in-flight request and flush microtasks under fake timers.
    await act(async () => {
      resolveFn(EMPTY_RESP);
      vi.advanceTimersByTime(0);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByTestId("search-palette-loading")).toBeNull();
  });

  it("renders populated results with section headers", async () => {
    apiMock.search.mockResolvedValueOnce(POPULATED_RESP);
    renderShell();
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "p", metaKey: true }));
    });
    const input = screen.getByTestId("search-palette-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "tesla" } });
    await act(async () => {
      vi.advanceTimersByTime(250);
      // Flush the resolved promise + state-update microtask under fake timers.
      await Promise.resolve();
      await Promise.resolve();
      vi.advanceTimersByTime(0);
      await Promise.resolve();
    });
    expect(screen.getByText(/Records \(1\)/)).toBeInTheDocument();
    expect(screen.getByText(/Symbols \(1\)/)).toBeInTheDocument();
    expect(screen.getByText(/Clusters \(1\)/)).toBeInTheDocument();
    // Each row carries data-row-kind.
    expect(document.querySelectorAll('[data-row-kind="record"]').length).toBe(1);
    expect(document.querySelectorAll('[data-row-kind="symbol"]').length).toBe(1);
    expect(document.querySelectorAll('[data-row-kind="cluster"]').length).toBe(1);
  });

  it("renders 'No matches.' when query resolves to empty buckets", async () => {
    apiMock.search.mockResolvedValueOnce(EMPTY_RESP);
    renderShell();
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "p", metaKey: true }));
    });
    const input = screen.getByTestId("search-palette-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "zzzzz" } });
    await act(async () => {
      vi.advanceTimersByTime(250);
      await Promise.resolve();
      await Promise.resolve();
      vi.advanceTimersByTime(0);
      await Promise.resolve();
    });
    expect(screen.getByTestId("search-palette-empty")).toBeInTheDocument();
  });

  it("falls back to /symbols/:symbol when Enter is pressed with no results", async () => {
    apiMock.search.mockResolvedValueOnce(EMPTY_RESP);
    const { router } = renderShellWithRouter(["/"]);
    act(() => {
      const event = new KeyboardEvent("keydown", { key: "p", metaKey: true });
      document.dispatchEvent(event);
    });
    const input = screen.getByTestId("search-palette-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "aapl" } });
    await act(async () => {
      vi.advanceTimersByTime(250);
      await Promise.resolve();
      await Promise.resolve();
      vi.advanceTimersByTime(0);
      await Promise.resolve();
    });

    act(() => {
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(router.state.location.pathname).toBe("/symbols/AAPL");
  });

  it("does not navigate for invalid symbol text in Enter fallback", async () => {
    apiMock.search.mockResolvedValueOnce(EMPTY_RESP);
    const { router } = renderShellWithRouter(["/"]);
    act(() => {
      const event = new KeyboardEvent("keydown", { key: "p", metaKey: true });
      document.dispatchEvent(event);
    });
    const input = screen.getByTestId("search-palette-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "$AAPL!" } });
    await act(async () => {
      vi.advanceTimersByTime(250);
      await Promise.resolve();
      await Promise.resolve();
      vi.advanceTimersByTime(0);
      await Promise.resolve();
    });

    act(() => {
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(router.state.location.pathname).toBe("/");
    expect(screen.getByTestId("search-palette")).toBeInTheDocument();
  });
});

describe("Accessibility (O-10)", () => {
  it("enforces combobox and listbox accessibility contracts", async () => {
    apiMock.search.mockResolvedValueOnce(POPULATED_RESP);
    renderShell();
    
    // Open the palette
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "p", metaKey: true }));
    });
    
    const input = screen.getByTestId("search-palette-input") as HTMLInputElement;
    
    // Verify initial combobox attributes on input
    expect(input).toHaveAttribute("role", "combobox");
    expect(input).toHaveAttribute("aria-autocomplete", "list");
    expect(input).toHaveAttribute("aria-expanded", "true");
    expect(input).toHaveAttribute("aria-controls", "search-palette-listbox");
    expect(input).toHaveAttribute("aria-describedby", "search-palette-instructions");
    
    // Verify instruction element exists and describes list navigation
    const instructions = document.getElementById("search-palette-instructions");
    expect(instructions).toBeInTheDocument();
    expect(instructions?.textContent).toContain("arrow keys");

    // Perform query to get search results
    fireEvent.change(input, { target: { value: "tesla" } });
    await act(async () => {
      vi.advanceTimersByTime(250);
      await Promise.resolve();
      await Promise.resolve();
      vi.advanceTimersByTime(0);
      await Promise.resolve();
    });

    // Check listbox role and id
    const listbox = screen.getByRole("listbox");
    expect(listbox).toHaveAttribute("id", "search-palette-listbox");

    // Check option list attributes (records, symbols, clusters)
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(3);

    // Option 0 (Record)
    expect(options[0]).toHaveAttribute("id", "search-palette-option-0");
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    expect(options[0]).toHaveAttribute("aria-label", "Record: Tesla beats deliveries forecast (bloomberg.com)");

    // Option 1 (Symbol)
    expect(options[1]).toHaveAttribute("id", "search-palette-option-1");
    expect(options[1]).toHaveAttribute("aria-selected", "false");
    expect(options[1]).toHaveAttribute("aria-label", "Symbol: TSLA, 3 mentions");

    // Option 2 (Cluster)
    expect(options[2]).toHaveAttribute("id", "search-palette-option-2");
    expect(options[2]).toHaveAttribute("aria-selected", "false");
    expect(options[2]).toHaveAttribute("aria-label", "Cluster: #deadbeef, size 2, symbols: TSLA, GM");

    // Navigate to next option (Symbol) and verify active descendant
    act(() => {
      fireEvent.keyDown(input, { key: "ArrowDown" });
    });
    expect(input).toHaveAttribute("aria-activedescendant", "search-palette-option-1");
    expect(options[1]).toHaveAttribute("aria-selected", "true");
  });

  it("applies correct focus outline and visible ring CSS classes to interactive controls", async () => {
    apiMock.search.mockResolvedValueOnce(POPULATED_RESP);
    renderShell();
    
    // Open the palette
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "p", metaKey: true }));
    });
    
    const input = screen.getByTestId("search-palette-input") as HTMLInputElement;
    const inputWrapper = input.parentElement;
    expect(inputWrapper).toHaveClass("focus-within:ring-1");

    const closeBtn = screen.getByLabelText("Close search palette");
    expect(closeBtn).toHaveClass("focus-visible:ring-accent");

    // Execute query to reveal Save button
    fireEvent.change(input, { target: { value: "tesla" } });
    await act(async () => {
      vi.advanceTimersByTime(250);
      await Promise.resolve();
      await Promise.resolve();
      vi.advanceTimersByTime(0);
      await Promise.resolve();
    });

    const saveBtn = screen.getByTestId("search-palette-save-button");
    expect(saveBtn).toHaveClass("focus-visible:ring-accent");
  });
});

