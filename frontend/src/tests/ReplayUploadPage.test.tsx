import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import { ReplayUploadPage } from "@/features/replay-upload/ReplayUploadPage";

/**
 * Round 7 Bug R3 + R4 regressions:
 *   R3 — the /replay page advertised a Replay surface in nav + URL but
 *        had only paste + upload tabs. This file pins the third Replay
 *        tab's wiring against POST /replay.
 *   R4 — UploadForm had no Clear button while PasteForm did. We pin the
 *        symmetry + the clear-resets-state behavior.
 *
 * Round 8 redesign update:
 *   The page now also pulls /ui/sidecar-status to drive the live storage
 *   context strip + QuickStats sidebar. The fetch mock here discriminates
 *   by URL so the R3/R4 contracts can still be asserted in isolation
 *   without the sidecar-status query "stealing" mocked responses.
 */

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return createElement(
    QueryClientProvider,
    { client: qc },
    createElement(MemoryRouter, { initialEntries: ["/replay"] }, children),
  );
}

const fetchMock = vi.fn();

const SIDECAR_STUB = {
  healthy: true,
  api_host: "127.0.0.1",
  api_port: 0,
  pid: 0,
  uptime_seconds: 0,
  records: { total: 0, finance_relevant: 0 },
  dlq: 0,
  diagnostic_enabled: true,
  generated_at: "2026-01-01T00:00:00Z",
};

beforeEach(() => {
  fetchMock.mockReset();
  // URL-discriminating default: sidebar/context strips get a quiet stub.
  // Per-test mocks (replay/demo) layer on via mockImplementationOnce.
  fetchMock.mockImplementation((url: string) => {
    if (typeof url === "string" && url.startsWith("/ui/sidecar-status")) {
      return Promise.resolve(jsonResponse(SIDECAR_STUB));
    }
    return Promise.resolve(new Response("unhandled", { status: 500 }));
  });
  (globalThis as { fetch?: typeof fetch }).fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  delete (globalThis as { fetch?: typeof fetch }).fetch;
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * Push a one-shot mock for the next call to the given URL — but unlike
 * `mockImplementationOnce`, this fires when the *matching URL* is called,
 * not when any URL is called next. Sidecar-status etc. still fall through
 * to the URL-discriminating default. Pinned by 2026-05 redesign: the page
 * fires multiple GETs unrelated to the endpoint under test.
 */
function mockOnce(url: string, response: Response) {
  let consumed = false;
  fetchMock.mockImplementation((u: string) => {
    if (u === url && !consumed) {
      consumed = true;
      return Promise.resolve(response);
    }
    if (typeof u === "string" && u.startsWith("/ui/sidecar-status")) {
      return Promise.resolve(jsonResponse(SIDECAR_STUB));
    }
    return Promise.resolve(new Response("unhandled", { status: 500 }));
  });
}

/** Calls made against a specific URL. */
function callsFor(url: string): Array<[string, RequestInit | undefined]> {
  return fetchMock.mock.calls.filter(([u]) => u === url) as Array<
    [string, RequestInit | undefined]
  >;
}

describe("ReplayUploadPage", () => {
  it("renders three tabs in order: Paste, Upload, Replay", () => {
    render(createElement(ReplayUploadPage), { wrapper });
    const tablist = screen.getByRole("tablist", { name: /Replay\/Upload mode/i });
    const tabs = within(tablist).getAllByRole("tab");
    // The visible label is exactly the mode name — the subtitle renders
    // OUTSIDE the tablist, the icon is aria-hidden, so textContent is the
    // label verbatim.
    expect(tabs.map((t) => t.textContent)).toEqual([
      "Paste article",
      "Upload file",
      "Replay JSONL",
    ]);
    // Paste is the default-selected tab.
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    expect(tabs[2]).toHaveAttribute("aria-selected", "false");
  });

  it("switching to the Replay tab exposes the run-replay form", () => {
    render(createElement(ReplayUploadPage), { wrapper });
    fireEvent.click(screen.getByTestId("tab-replay"));
    expect(screen.getByTestId("replay-max-input")).toHaveValue(50);
    expect(screen.getByTestId("replay-run")).toBeEnabled();
  });

  it("Run replay posts to /replay with clamped max_records and shows the result", async () => {
    mockOnce(
      "/replay",
      jsonResponse({
        processed: 7,
        skipped: 3,
        failed: 1,
        dlq: 5,
        dlq_delta: 1,
        records_before: { total: 20, finance_relevant: 12 },
        records_after: { total: 22, finance_relevant: 13 },
        inserted: 2,
        replaced: 4,
        net_new_records: 2,
      }),
    );

    render(createElement(ReplayUploadPage), { wrapper });
    fireEvent.click(screen.getByTestId("tab-replay"));

    const maxInput = screen.getByTestId("replay-max-input") as HTMLInputElement;
    fireEvent.change(maxInput, { target: { value: "12" } });
    expect(maxInput.value).toBe("12");

    fireEvent.click(screen.getByTestId("replay-run"));

    await waitFor(() => {
      expect(screen.getByTestId("replay-result")).toBeInTheDocument();
    });

    const replayCalls = callsFor("/replay");
    expect(replayCalls).toHaveLength(1);
    const [url, init] = replayCalls[0];
    expect(url).toBe("/replay");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({ max_records: 12 });

    expect(screen.getByTestId("replay-processed")).toHaveTextContent("7");
    expect(screen.getByTestId("replay-skipped")).toHaveTextContent("3");
    expect(screen.getByTestId("replay-failed")).toHaveTextContent("1");
    expect(screen.getByTestId("replay-dlq")).toHaveTextContent("5+1");
    expect(screen.getByTestId("replay-net-new")).toHaveTextContent("2");
    expect(screen.getByTestId("replay-inserted")).toHaveTextContent("2");
    expect(screen.getByTestId("replay-replaced")).toHaveTextContent("4");
    expect(screen.getByTestId("replay-records-total")).toHaveTextContent("20 → 22");
  });

  it("Run replay clamps the max input above 5000", () => {
    render(createElement(ReplayUploadPage), { wrapper });
    fireEvent.click(screen.getByTestId("tab-replay"));

    const maxInput = screen.getByTestId("replay-max-input") as HTMLInputElement;
    fireEvent.change(maxInput, { target: { value: "9999" } });
    // The onChange clamps to 5000.
    expect(maxInput.value).toBe("5000");
  });

  it("Run replay surfaces a 5xx response as an inline alert and keeps the tab usable", async () => {
    mockOnce("/replay", new Response("supervisor crashed", { status: 500 }));

    render(createElement(ReplayUploadPage), { wrapper });
    fireEvent.click(screen.getByTestId("tab-replay"));
    fireEvent.click(screen.getByTestId("replay-run"));

    await waitFor(
      () => {
        // The mutation surfaces an inline ApiError; the message contains the
        // status code so the analyst can correlate against the sidecar log.
        const alerts = screen.getAllByRole("alert");
        const replayAlert = alerts.find((el) => /500/.test(el.textContent ?? ""));
        expect(replayAlert).toBeTruthy();
      },
      // `api.replay` POSTs through the shared client, which retries a
      // retryable 5xx (status >= 500) up to DEFAULT_RETRIES (2) times with
      // exponential backoff (~300ms + ~600ms + jitter). That resilience is
      // intentional — a sidecar mid-restart blips 5xx — but it pushes the
      // settled error state past waitFor's 1000ms default. Budget for the
      // full retry/backoff window so we assert on the real terminal state
      // (the inline `/replay → 500` alert) rather than racing it.
      { timeout: 4000 },
    );
    // The form remains operable.
    expect(screen.getByTestId("replay-run")).toBeEnabled();
  });

  it("UploadForm exposes a Clear button that resets state", () => {
    render(createElement(ReplayUploadPage), { wrapper });
    fireEvent.click(screen.getByTestId("tab-upload"));

    // Initially the form is pristine — clear should be disabled.
    const clear = screen.getByTestId("upload-clear");
    expect(clear).toBeDisabled();

    // Type into title; clear becomes enabled.
    const titleInput = screen.getByLabelText(/title \(optional/i) as HTMLInputElement;
    fireEvent.change(titleInput, { target: { value: "Fed raises rates" } });
    expect(clear).toBeEnabled();

    // Clicking clear wipes the title back to empty.
    fireEvent.click(clear);
    expect(titleInput.value).toBe("");
    expect(clear).toBeDisabled();
  });

  it("tabs swap the help card copy between demo and replay surfaces", () => {
    render(createElement(ReplayUploadPage), { wrapper });
    expect(screen.getByTestId("help-card")).toHaveTextContent(/What happens to your article/i);
    fireEvent.click(screen.getByTestId("tab-replay"));
    expect(screen.getByTestId("help-card")).toHaveTextContent(/What does Replay do/i);
  });
});
