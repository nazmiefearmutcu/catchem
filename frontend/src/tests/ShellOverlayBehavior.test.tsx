import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryRouter,
  RouterProvider,
} from "react-router-dom";
import { Shell } from "@/layout/Shell";
import { OPEN_COMMAND_PALETTE_EVENT } from "@/components/CommandPalette";
import type { UISummary } from "@/types/api";

vi.mock("@/hooks/useLiveStream", () => ({
  useLiveStream: vi.fn(() => ({ status: "idle", lastBeatAt: null, stalenessSeconds: null })),
}));

vi.mock("@/hooks/useDesktopAlerts", () => ({
  useDesktopAlerts: vi.fn(),
  useUnreadNotificationCount: vi.fn(() => 0),
  useDesktopAlertState: vi.fn(() => ["off", vi.fn()]),
  getAlertThreshold: vi.fn(() => 0.65),
  setAlertThreshold: vi.fn((value: number) => value),
  useAlertLog: vi.fn(() => []),
  pushNotification: vi.fn(),
  pushToast: vi.fn(),
  markNotificationsRead: vi.fn(),
}));

vi.mock("@/hooks/useTauriMenu", () => ({
  useTauriMenu: vi.fn(),
}));

vi.mock("@/hooks/useAccent", () => ({
  useAccent: vi.fn(),
}));

vi.mock("@/hooks/useTheme", () => ({
  useTheme: vi.fn(() => ({ theme: "dark", setTheme: vi.fn(), toggle: vi.fn() })),
}));

vi.mock("@/components/NotificationCenter", () => ({
  NotificationCenter: ({ open }: { open: boolean }) =>
    open ? <div data-testid="notification-center-card">Notifications</div> : null,
}));

vi.mock("@/components/StatusBanner", () => ({
  StatusBanner: () => null,
}));

vi.mock("@/components/SidecarBanner", () => ({
  SidecarBanner: () => null,
}));

vi.mock("@/components/ToastTray", () => ({
  ToastTray: () => null,
}));

vi.mock("@/components/OnboardingModal", () => ({
  OnboardingModal: () => null,
}));

vi.mock("@/components/ShortcutOverlay", () => ({
  ShortcutOverlay: () => null,
}));

vi.mock("@/components/HelpDrawer", () => ({
  HelpDrawer: () => null,
}));

vi.mock("@/components/RouteErrorBoundary", () => ({
  RouteErrorBoundary: ({ children }: { children: ReactNode }) => children,
}));

vi.mock("@/components/Skeleton", () => ({
  Skeleton: () => <div data-testid="skeleton-fallback" />,
}));

vi.mock("@/components/LiveDot", () => ({
  LiveDot: ({ status }: { status: string }) => <span>{status}</span>,
}));

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

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      summary: vi.fn(),
    },
  };
});

function summaryMock(): UISummary {
  return {
    mode: "production_safe",
    is_production_safe: true,
    diagnostic_allowed: true,
    use_ml_stubs: false,
    totals: {
      total: 12,
      finance_relevant: 9,
    },
    guards: {
      ok: true,
    },
    diagnostic_count: 0,
    asset_class_distribution: {},
    reason_code_distribution: {},
    sentiment_distribution: {},
    recent_top: [],
    dlq: 0,
    model_versions: {},
    generated_at: "2026-05-28T12:00:00+00:00",
  };
}

function renderShell() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: <Shell />,
        children: [
          { index: true, element: <div>home</div> },
          { path: "feed", element: <div>feed</div> },
        ],
      },
    ],
    { initialEntries: ["/"] },
  );

  return {
    router,
    ...render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    ),
  };
}

  beforeEach(async () => {
  installLocalStorage();
  const { api } = await import("@/lib/api");
  vi.mocked(api.summary).mockResolvedValue(summaryMock());
});

  afterEach(() => {
  vi.clearAllMocks();
});

describe("Shell overlay behavior", () => {
  it("notification bell can close command palette before opening notification center", async () => {
    const { router } = renderShell();

    await act(async () => {
      fireEvent.click(await screen.findByLabelText("Open command palette"));
    });
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("notification-bell"));
    expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull();
    expect(screen.getByTestId("notification-center-card")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });

  it("g n opens notifications after closing an active overlay", async () => {
    const { router } = renderShell();

    act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "p", metaKey: true }),
      );
    });
    expect(screen.getByTestId("search-palette")).toBeInTheDocument();

    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "g" }));
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "n" }));
    });
    expect(screen.queryByTestId("search-palette")).toBeNull();
    expect(screen.getByTestId("notification-center-card")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });

  it("ignores random second key after dismissing overlay with g", async () => {
    const { router } = renderShell();

    act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "p", metaKey: true }),
      );
    });
    expect(screen.getByTestId("search-palette")).toBeInTheDocument();

    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "g" }));
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "f" }));
    });

    expect(screen.queryByTestId("search-palette")).toBeNull();
    expect(screen.queryByTestId("notification-center-card")).toBeNull();
    expect(router.state.location.pathname).toBe("/");
  });

  it("ignores random second key after dismissing command palette with g", () => {
    const { router } = renderShell();

    act(() => {
      window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT));
    });
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();

    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "g" }));
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "x" }));
    });

    expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull();
    expect(router.state.location.pathname).toBe("/");
  });
});
