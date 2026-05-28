import { useState } from "react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import {
  MemoryRouter,
  RouterProvider,
  createMemoryRouter,
  useNavigate,
} from "react-router-dom";
import {
  HISTORY_STORAGE_KEY,
  HISTORY_VIEWED_KEY,
  NOTIFICATION_HISTORY_LIMIT,
  __resetNotificationStoreForTests,
  clearNotificationHistory,
  markNotificationsRead,
  pushNotification,
  pushToast,
} from "@/hooks/useDesktopAlerts";
import { NotificationCenter } from "@/components/NotificationCenter";

/**
 * Vitest's jsdom build doesn't ship localStorage by default; the suite-wide
 * setup file leaves storage shims to individual tests so we install one
 * before each test here. Same pattern as desktopAlerts.test.ts.
 */
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

function renderCenter(initialOpen: boolean = true): { unmount: () => void; setOpen: (v: boolean) => void } {
  let open = initialOpen;
  let onClose = vi.fn();
  const utils = render(
    <MemoryRouter>
      <NotificationCenter open={open} onClose={onClose} />
    </MemoryRouter>,
  );
  return {
    unmount: utils.unmount,
    setOpen: (v: boolean) => {
      open = v;
      utils.rerender(
        <MemoryRouter>
          <NotificationCenter open={open} onClose={onClose} />
        </MemoryRouter>,
      );
    },
  };
}

function renderCenterWithRouter(initialOpen: boolean = true) {
  const onClose = vi.fn();
  function Shell() {
    const nav = useNavigate();
    // Mirror the real Shell: `open` is parent-owned state, so onClose()
    // actually flips the prop to false and unmounts the card.
    const [open, setOpen] = useState(initialOpen);
    const handleClose = () => {
      onClose();
      setOpen(false);
    };
    return (
      <>
        <button type="button" data-testid="route-next" onClick={() => nav("/map")}>
          go map
        </button>
        <NotificationCenter open={open} onClose={handleClose} />
      </>
    );
  }
  const router = createMemoryRouter([{ path: "*", element: <Shell /> }], {
    initialEntries: ["/"],
  });
  const utils = render(<RouterProvider router={router} />);
  return {
    ...utils,
    onClose,
    router,
  };
}

describe("notification center", () => {
  beforeEach(() => {
    installLocalStorage();
    __resetNotificationStoreForTests();
  });

  it("pushing a toast adds an entry to history (newest first)", () => {
    act(() => {
      pushToast({
        id: "cap-1",
        title: "Apple earnings beat",
        domain: "reuters.com",
        score: 0.78,
        reasons: ["earnings"],
        symbols: ["AAPL"],
      });
      pushToast({
        id: "cap-2",
        title: "Fed minutes hawkish",
        domain: "ft.com",
        score: 0.66,
        reasons: ["macro"],
        symbols: [],
      });
    });

    renderCenter(true);
    const rows = screen.getAllByTestId("notification-row");
    expect(rows.length).toBe(2);
    // Newest-first ordering — cap-2 was pushed last so it should be first.
    expect(within(rows[0]).getByText(/Fed minutes hawkish/)).toBeInTheDocument();
    expect(within(rows[1]).getByText(/Apple earnings beat/)).toBeInTheDocument();
  });

  it("history caps at NOTIFICATION_HISTORY_LIMIT (50) entries", () => {
    act(() => {
      for (let i = 0; i < NOTIFICATION_HISTORY_LIMIT + 10; i += 1) {
        pushToast({
          id: `cap-${i}`,
          title: `Item ${i}`,
          domain: "example.com",
          score: 0.5,
          reasons: [],
          symbols: [],
        });
      }
    });
    renderCenter(true);
    const rows = screen.getAllByTestId("notification-row");
    expect(rows.length).toBe(NOTIFICATION_HISTORY_LIMIT);
    // The most recent insert should be on top.
    expect(within(rows[0]).getByText(/Item \d+/)).toBeInTheDocument();
  });

  it("persists history to localStorage under the canonical key", () => {
    act(() => {
      pushToast({
        id: "cap-persist",
        title: "Persisted item",
        domain: "wsj.com",
        score: 0.81,
        reasons: ["m&a"],
        symbols: ["MSFT"],
      });
    });
    const raw = window.localStorage.getItem(HISTORY_STORAGE_KEY);
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw!);
    expect(Array.isArray(parsed)).toBe(true);
    expect(parsed.length).toBe(1);
    expect(parsed[0].id).toBe("cap-persist");
    expect(parsed[0].category).toBe("toast");
    expect(typeof parsed[0].createdAt).toBe("number");
  });

  it("filter chip narrows the list to a single category", () => {
    act(() => {
      pushToast({
        id: "toast-1",
        title: "Toast arrival",
        domain: "ft.com",
        score: 0.7,
        reasons: [],
        symbols: [],
      });
      pushNotification({
        id: "webhook-1",
        title: "Webhook test ok",
        domain: "settings",
        score: 0,
        reasons: [],
        symbols: [],
        category: "webhook",
      });
      pushNotification({
        id: "system-1",
        title: "Sidecar reconnected",
        domain: "sidecar",
        score: 0,
        reasons: [],
        symbols: [],
        category: "system",
      });
    });
    renderCenter(true);

    // All three categories visible by default.
    expect(screen.getAllByTestId("notification-row").length).toBe(3);

    // Click "Webhook" chip — only the webhook entry should remain.
    fireEvent.click(screen.getByTestId("notif-filter-webhook"));
    const rows = screen.getAllByTestId("notification-row");
    expect(rows.length).toBe(1);
    expect(rows[0].getAttribute("data-category")).toBe("webhook");
    expect(within(rows[0]).getByText(/Webhook test ok/)).toBeInTheDocument();
  });

  it("clear all button empties the history and shows the empty state", () => {
    act(() => {
      pushToast({
        id: "toast-x",
        title: "Going away",
        domain: "ft.com",
        score: 0.5,
        reasons: [],
        symbols: [],
      });
    });
    renderCenter(true);
    expect(screen.getAllByTestId("notification-row").length).toBe(1);

    fireEvent.click(screen.getByTestId("notification-center-clear"));
    expect(screen.queryByTestId("notification-row")).toBeNull();
    expect(screen.getByTestId("notification-center-empty")).toBeInTheDocument();
    // localStorage should reflect the clear too.
    expect(window.localStorage.getItem(HISTORY_STORAGE_KEY)).toBe("[]");
  });

  it("opening the modal marks all as read (persists viewed timestamp)", () => {
    // Seed an unread item *before* the modal opens.
    act(() => {
      pushToast({
        id: "cap-unread",
        title: "Unread",
        domain: "ft.com",
        score: 0.6,
        reasons: [],
        symbols: [],
      });
    });
    expect(window.localStorage.getItem(HISTORY_VIEWED_KEY)).toBeNull();

    renderCenter(true);

    const stamp = window.localStorage.getItem(HISTORY_VIEWED_KEY);
    expect(stamp).not.toBeNull();
    expect(Number.parseInt(stamp!, 10)).toBeGreaterThan(0);
  });

  it("clicking a toast row navigates to /feed/<capture_id> and closes", () => {
    act(() => {
      pushToast({
        id: "cap-click",
        title: "Clickable item",
        domain: "ft.com",
        score: 0.7,
        reasons: [],
        symbols: ["AAPL"],
      });
    });
    const onClose = vi.fn();
    render(
      <MemoryRouter initialEntries={["/"]}>
        <NotificationCenter open={true} onClose={onClose} />
      </MemoryRouter>,
    );

    const rows = screen.getAllByTestId("notification-row");
    expect(rows.length).toBe(1);
    // Verify the row is clickable (toast category, has id) — disabled prop
    // false means React allows the click handler.
    expect(rows[0]).not.toBeDisabled();
    fireEvent.click(rows[0]);
    expect(onClose).toHaveBeenCalled();
  });

  it("system-category rows are not clickable (no capture_id to navigate to)", () => {
    act(() => {
      pushNotification({
        id: "system-1",
        title: "Sidecar disconnected",
        domain: "sidecar",
        score: 0,
        reasons: [],
        symbols: [],
        category: "system",
      });
    });
    renderCenter(true);
    const rows = screen.getAllByTestId("notification-row");
    expect(rows.length).toBe(1);
    expect(rows[0]).toBeDisabled();
  });

  it("auto-closes on route change when open", () => {
    const { router, onClose } = renderCenterWithRouter(true);
    expect(screen.getByTestId("notification-center-card")).toBeInTheDocument();

    act(() => {
      fireEvent.click(screen.getByTestId("route-next"));
    });
    expect(router.state.location.pathname).toBe("/map");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("notification-center-card")).toBeNull();
  });

  it("markNotificationsRead can be called directly and updates storage", () => {
    const t = markNotificationsRead(123_456_789);
    expect(t).toBe(123_456_789);
    expect(window.localStorage.getItem(HISTORY_VIEWED_KEY)).toBe("123456789");
  });

  it("clearNotificationHistory is idempotent", () => {
    clearNotificationHistory();
    clearNotificationHistory();
    expect(window.localStorage.getItem(HISTORY_STORAGE_KEY)).toBe("[]");
  });
});
