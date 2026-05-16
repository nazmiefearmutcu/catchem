# Frontend Architecture

## Stack

- **React 18** + **TypeScript 5** strict mode
- **Vite 5** for the dev server + production build
- **React Router 6** for routing with `lazy()` code splitting
- **TanStack Query 5** for data fetching, caching, invalidation
- **TanStack Table 8** (available; not yet used — feed renders a hand-written list for finer control)
- **ECharts 5** + `echarts-for-react` for visualizations (lazy-chunked)
- **cmdk** for the command palette
- **Tailwind CSS 3** with CSS-variable–backed light/dark themes
- **Vitest** + Testing Library for unit/component tests

No state library beyond React Query + URL search params. No styling library
beyond Tailwind + a handful of `@layer components` primitives in
`styles/globals.css`. This keeps the dep tree small and the bundle predictable.

## Directory layout

```
frontend/
├── index.html               # SPA shell, theme bootstrap script
├── package.json
├── vite.config.ts           # build target → ../src/fusion_stack/static/app
├── tailwind.config.js
├── postcss.config.js
├── tsconfig.json
└── src/
    ├── main.tsx             # entry, QueryClientProvider, Router
    ├── app/App.tsx          # route table with lazy() splits
    ├── layout/Shell.tsx     # nav, theme toggle, live dot, banner
    ├── components/          # StatusBanner, LiveDot, Pill, Skeleton, CommandPalette
    ├── charts/EChart.tsx    # theme-aware ECharts wrapper
    ├── features/
    │   ├── overview/        # OverviewPage
    │   ├── feed/            # FeedPage (filters, list, drawer routing)
    │   ├── record-detail/   # RecordDrawer (Esc to close, raw JSON peek)
    │   ├── market-map/      # MarketMapPage (heatmap, stacked trend)
    │   ├── symbols/         # SymbolsPage + SymbolDetailPage
    │   ├── benchmark/       # BenchmarkPage (re-run, per-item table, history)
    │   ├── ops/             # OpsPage (guard status, model versions, raw config)
    │   └── settings/        # SettingsPage (theme, shortcuts, mode docs)
    ├── hooks/
    │   ├── useLiveStream.ts # SSE first, polling fallback
    │   ├── useTheme.ts      # localStorage-backed theme
    │   └── useUrlFilters.ts # URL-as-state for feed filters
    ├── lib/api.ts           # typed fetch wrapper + safeHref + formatters
    ├── styles/globals.css   # Tailwind layers + CSS-variable tokens
    ├── types/api.ts         # backend payload types
    └── tests/               # vitest specs
```

## Routing & code splitting

`App.tsx` uses `React.lazy` for every route. Vite emits one chunk per route
plus a shared `charts` chunk and `query` chunk. Initial page weight is small
(~63KB JS gzipped for the main chunk) and other chunks load on navigation.

## Data fetching

- A single `QueryClient` lives in `main.tsx`.
- Every page uses `useQuery` from TanStack Query with a stable `queryKey`.
- Cache invalidation is centralized in `useLiveStream` — when the SSE
  `summary` event lands, we invalidate `['summary', 'facets', 'recent']`.
- The `api` object in `lib/api.ts` is the only file that knows about HTTP
  paths. Add a new endpoint there and use it from any page.

## State

| Concern | Where | Persists across |
|---|---|---|
| API cache | TanStack Query | refresh (no) |
| Theme | localStorage | refresh, tabs |
| Feed filters | URL search params | refresh, sharing |
| Drawer open | URL (`/feed/:captureId`) | refresh, sharing |
| Command palette | Local React state | nothing |

## Security

- **HTML injection is impossible.** We never assign user-controlled strings to
  any innerHTML-style API. Every record field (title, evidence, domain, URL)
  goes through React's text channel.
- **`safeHref` filter** allows only `http:` / `https:` URLs out of the
  feed/record links. `javascript:`, `data:`, `file:` schemes return
  `undefined` and we omit the link.
- **External links** get `target="_blank" rel="noopener noreferrer"`.
- No env vars or secrets are bundled into the client. Vite's `import.meta.env`
  isn't read anywhere.

## Accessibility

- `:focus-visible` outline applied globally (yellow ring on tab focus).
- `prefers-reduced-motion` honored — animations drop to near-zero duration.
- Drawer is a `role="dialog" aria-modal="true"`, focus moves to the close
  button on open, `Esc` closes.
- All interactive controls have `aria-pressed` / `aria-label` where relevant.
- Color contrast passes WCAG AA for both themes (#e7ebf0 on #0e1014 in dark;
  #0e1014 on #fafbfc in light).

## Build → ship

```bash
cd frontend
npm install                  # one-time
npm run build                # → ../src/fusion_stack/static/app/
npm test                     # vitest unit + component
npm run dev                  # vite dev server on :5173, proxies API to :8087
```

The bootstrap script does all of this for you on first run.

## Dev mode

Run two processes when working on the UI:

```bash
# Terminal 1: API
fusion-stack serve

# Terminal 2: Vite dev server
cd frontend
npm run dev    # http://localhost:5173 with HMR; proxies /ui/* to :8087
```

You can also pass `--dev-ui` to the bootstrap to get the exact command.
