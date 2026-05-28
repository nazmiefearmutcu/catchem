import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { TagsSection } from "@/features/record-detail/RecordDrawer";

/**
 * Pin the user-tag editor contract (RecordDrawer > TagsSection):
 *  - initial GET hydrates the pill list
 *  - typing a valid tag + Enter / clicking add fires the POST and
 *    refreshes the list
 *  - removing a pill fires the DELETE
 *  - the client-side regex blocks whitespace / disallowed punctuation
 *    BEFORE the fetch — the user sees the error message, no network call.
 */

const fetchMock = vi.fn();

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return createElement(QueryClientProvider, { client: qc }, children);
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  (globalThis as { fetch?: typeof fetch }).fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  delete (globalThis as { fetch?: typeof fetch }).fetch;
});

describe("<TagsSection>", () => {
  it("renders existing tags from the initial GET", async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/records/cap-1/tags") && !url.includes("undefined")) {
        return Promise.resolve(
          jsonResponse({ capture_id: "cap-1", tags: ["earnings", "watch"] }),
        );
      }
      return Promise.resolve(new Response("no", { status: 500 }));
    });
    render(createElement(wrapper, null, createElement(TagsSection, { captureId: "cap-1" })));
    await waitFor(() => expect(screen.getByTestId("tag-pill-earnings")).toBeInTheDocument());
    expect(screen.getByTestId("tag-pill-watch")).toBeInTheDocument();
  });

  it("adds a tag via POST and refreshes the list on Enter", async () => {
    fetchMock
      .mockImplementationOnce(() =>
        Promise.resolve(jsonResponse({ capture_id: "cap-1", tags: [] })),
      )
      .mockImplementationOnce(() =>
        Promise.resolve(
          jsonResponse({ ok: true, added: true, tags: ["earnings"] }),
        ),
      );

    render(createElement(wrapper, null, createElement(TagsSection, { captureId: "cap-1" })));
    await waitFor(() =>
      expect(screen.getByText(/no tags yet/i)).toBeInTheDocument(),
    );
    const input = screen.getByTestId("tag-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "earnings" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(screen.getByTestId("tag-pill-earnings")).toBeInTheDocument());
    // The second call must be the POST.
    const postCall = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === "POST",
    );
    expect(postCall).toBeTruthy();
    expect(postCall?.[0]).toBe("/api/records/cap-1/tags");
    const body = JSON.parse(String((postCall?.[1] as RequestInit).body));
    expect(body).toEqual({ tag: "earnings" });
  });

  it("removes a tag via DELETE when the pill is clicked", async () => {
    fetchMock
      .mockImplementationOnce(() =>
        Promise.resolve(jsonResponse({ capture_id: "cap-1", tags: ["watch"] })),
      )
      .mockImplementationOnce(() =>
        Promise.resolve(jsonResponse({ ok: true, removed: true, tags: [] })),
      );

    render(createElement(wrapper, null, createElement(TagsSection, { captureId: "cap-1" })));
    const pill = await screen.findByTestId("tag-pill-watch");
    fireEvent.click(pill);

    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls.find(
        ([, init]) => (init as RequestInit | undefined)?.method === "DELETE",
      );
      expect(deleteCall).toBeTruthy();
      expect(deleteCall?.[0]).toBe("/api/records/cap-1/tags/watch");
    });
  });

  it("rejects whitespace input client-side without hitting the API", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(jsonResponse({ capture_id: "cap-1", tags: [] })),
    );
    render(createElement(wrapper, null, createElement(TagsSection, { captureId: "cap-1" })));
    await waitFor(() =>
      expect(screen.getByText(/no tags yet/i)).toBeInTheDocument(),
    );
    const callsBefore = fetchMock.mock.calls.length;
    const input = screen.getByTestId("tag-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "has space" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(screen.getByTestId("tag-error")).toBeInTheDocument());
    expect(screen.getByTestId("tag-error").textContent).toMatch(/no whitespace/i);
    // No POST should have fired — same call count as before.
    expect(fetchMock.mock.calls.length).toBe(callsBefore);
  });

  it("rejects tags > 50 chars client-side", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(jsonResponse({ capture_id: "cap-1", tags: [] })),
    );
    render(createElement(wrapper, null, createElement(TagsSection, { captureId: "cap-1" })));
    await waitFor(() =>
      expect(screen.getByText(/no tags yet/i)).toBeInTheDocument(),
    );
    const input = screen.getByTestId("tag-input") as HTMLInputElement;
    // Bypass maxLength by setting the value directly via the change event.
    Object.defineProperty(input, "maxLength", { value: 200, configurable: true });
    fireEvent.change(input, { target: { value: "x".repeat(51) } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(screen.getByTestId("tag-error")).toBeInTheDocument());
    expect(screen.getByTestId("tag-error").textContent).toMatch(/50/);
  });
});
