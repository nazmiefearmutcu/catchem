import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import { TagsPage, TagCloud } from "@/features/tags/TagsPage";

/**
 * Pin the /tags analyst page contract (v39 task #149):
 *  - hero renders the headline + 4 stat tiles when tags exist
 *  - empty state with "How to tag records" link when none exist
 *  - tag cloud renders one chip per tag with a size class so a future
 *    rebrand can't silently flatten the frequency ramp
 *  - every chip + every detail row links to `/feed?tag=<name>` so the
 *    drill-down stays one click away
 *  - hero subtitle calls out the top 3 tags (verifies sort + slice)
 */

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return createElement(
    QueryClientProvider,
    { client: qc },
    createElement(MemoryRouter, { initialEntries: ["/tags"] }, children),
  );
}

const fetchMock = vi.fn();

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const SAMPLE_TAGS = [
  { tag: "earnings", count: 12 },
  { tag: "watch", count: 7 },
  { tag: "fade", count: 5 },
  { tag: "macro", count: 2 },
  { tag: "noise", count: 1 },
];

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation((url: string) => {
    if (url.startsWith("/api/tags")) {
      return Promise.resolve(jsonResponse({ items: SAMPLE_TAGS }));
    }
    return Promise.resolve(new Response("unhandled", { status: 500 }));
  });
  (globalThis as { fetch?: typeof fetch }).fetch =
    fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  delete (globalThis as { fetch?: typeof fetch }).fetch;
});

describe("TagsPage", () => {
  it("renders the hero with stat tiles when tags exist", async () => {
    render(createElement(TagsPage), { wrapper });
    await waitFor(() =>
      expect(screen.getByTestId("tags-headline")).toHaveTextContent(
        /5 tags covering 27 records/i,
      ),
    );
    // 4 stat tiles. We assert the values land in the hero so a future
    // refactor that turns the hero into something cheaper still has to
    // surface the same numbers.
    const hero = screen.getByTestId("tags-hero");
    expect(within(hero).getByText("total tags")).toBeInTheDocument();
    expect(within(hero).getByText("tagged records")).toBeInTheDocument();
    expect(within(hero).getByText("top tag")).toBeInTheDocument();
    expect(within(hero).getByText(/avg records.*tag/i)).toBeInTheDocument();
    expect(within(hero).getByText("earnings")).toBeInTheDocument();
    // Subtitle lists top-3 tags in count-descending order.
    expect(within(hero).getByText(/earnings · watch · fade/i)).toBeInTheDocument();
  });

  it("renders the empty state when no tags exist", async () => {
    fetchMock.mockReset();
    fetchMock.mockImplementation(() =>
      Promise.resolve(jsonResponse({ items: [] })),
    );
    render(createElement(TagsPage), { wrapper });
    await waitFor(() =>
      expect(
        screen.getByText(/No tags have been added yet/i),
      ).toBeInTheDocument(),
    );
    // "How to tag records" link points to /help.
    const helpLink = screen.getByTestId("tags-help-link") as HTMLAnchorElement;
    expect(helpLink.getAttribute("href")).toBe("/help");
    // Headline collapses to the no-tags variant.
    expect(screen.getByTestId("tags-headline")).toHaveTextContent(/no tags yet/i);
    // Open Feed chip should NOT appear on the empty state — it's a
    // dead link until at least one tag exists.
    expect(screen.queryByTestId("tags-open-feed")).not.toBeInTheDocument();
  });

  it("renders the tag cloud with a chip per tag and a size class", async () => {
    render(createElement(TagsPage), { wrapper });
    const cloud = await screen.findByTestId("tag-cloud");
    // One chip per tag.
    const chips = within(cloud).getAllByTestId(/^tag-cloud-chip-/);
    expect(chips).toHaveLength(SAMPLE_TAGS.length);
    // Highest-count tag gets the --lg size class; lowest gets --sm.
    const earnings = within(cloud).getByTestId("tag-cloud-chip-earnings");
    expect(earnings.className).toMatch(/tag-cloud-chip--lg/);
    const noise = within(cloud).getByTestId("tag-cloud-chip-noise");
    expect(noise.className).toMatch(/tag-cloud-chip--sm/);
  });

  it("every tag chip + row links to the filtered feed", async () => {
    render(createElement(TagsPage), { wrapper });
    const cloud = await screen.findByTestId("tag-cloud");
    const earningsChip = within(cloud).getByTestId(
      "tag-cloud-chip-earnings",
    ) as HTMLAnchorElement;
    expect(earningsChip.getAttribute("href")).toBe("/feed?tag=earnings");
    // Detail-table row links to the same target.
    const earningsRow = screen.getByTestId(
      "tags-row-earnings",
    ) as HTMLAnchorElement;
    expect(earningsRow.getAttribute("href")).toBe("/feed?tag=earnings");
    // Tag names that need URL encoding (e.g. dots / hyphens are allowed by
    // the regex but spaces are not — using a hyphenated tag here pins the
    // encodeURIComponent path even though the input wouldn't need escaping.
    const fadeChip = within(cloud).getByTestId(
      "tag-cloud-chip-fade",
    ) as HTMLAnchorElement;
    expect(fadeChip.getAttribute("href")).toBe("/feed?tag=fade");
  });

  it("hero exposes the Open Feed chip pointing at /feed", async () => {
    render(createElement(TagsPage), { wrapper });
    const open = await screen.findByTestId("tags-open-feed");
    expect((open as HTMLAnchorElement).getAttribute("href")).toBe("/feed");
  });

  it("TagCloud sorts unsorted input defensively so size ramp doesn't flip", () => {
    // Pre-fix bug: when items came in unsorted, the `minCount` reducer
    // seeded itself from `items[0]?.count` — which was *not* the maximum
    // any more — so Math.min produced a wrong floor and the largest tag
    // could end up rendered with `--sm` and the smallest with `--lg`.
    // After the fix TagCloud sorts internally; the result must place the
    // highest-count tag in the --lg bucket regardless of input order.
    const unsorted = [
      { tag: "tiny", count: 1 },
      { tag: "huge", count: 100 },
      { tag: "mid", count: 50 },
    ];
    render(
      createElement(MemoryRouter, null, createElement(TagCloud, { items: unsorted })),
    );
    const cloud = screen.getByTestId("tag-cloud");
    const huge = within(cloud).getByTestId("tag-cloud-chip-huge");
    expect(huge.className).toMatch(/tag-cloud-chip--lg/);
    const tiny = within(cloud).getByTestId("tag-cloud-chip-tiny");
    expect(tiny.className).toMatch(/tag-cloud-chip--sm/);
  });

  it("detail list renders one row per tag, sorted by count descending", async () => {
    render(createElement(TagsPage), { wrapper });
    const list = await screen.findByTestId("tags-detail-list");
    const rows = within(list).getAllByRole("link");
    // 5 tags = 5 rows. We assert the first row is the highest-count tag
    // ('earnings') so a future API change that returns ascending order
    // can't silently flip the cloud + table.
    expect(rows).toHaveLength(SAMPLE_TAGS.length);
    expect(rows[0].textContent).toMatch(/earnings/);
    expect(rows[rows.length - 1].textContent).toMatch(/noise/);
  });
});
