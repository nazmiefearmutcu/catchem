# Catchem Architecture

Catchem is a local-first macOS desktop cockpit that ingests financial-news streams (Awareness JSONL captures, a ~375-source awareness ingestion engine, an optional real-time push channel, paste/upload), runs a deterministic finance-relevance pipeline, layers a quant-analytics engine on top, and surfaces every signal through a premium React UI served by a co-located FastAPI sidecar. Everything — captures, models, SQLite, logs — lives on the analyst's laptop; nothing leaves the machine unless the user clicks an article link or wires the optional DeepSeek reviewer / webhook outputs. Beyond raw ingestion the sidecar runs awareness telemetry (live-window estimate + blind-spot detection), a GDELT global-tone macro signal, and a read-only portfolio subsystem that joins analyst holdings to the awareness layer. This document reflects the codebase after the awareness-engine + quant + portfolio expansion (Sections 3a–3d below).

> Companion docs: [`CATCHEM_ARCHITECTURE.md`](CATCHEM_ARCHITECTURE.md) (mermaid diagrams, threat model details), [`FRONTEND_ARCHITECTURE.md`](FRONTEND_ARCHITECTURE.md), [`CATCHEM_HARDENING.md`](CATCHEM_HARDENING.md), [`KEYBOARD_SHORTCUTS.md`](KEYBOARD_SHORTCUTS.md).

---

## 1. System architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Catchem.app — Tauri 2 native macOS shell                               │
│   ├─ Rust binary: lifecycle, native menu bar, multi-window, sidecar mgr │
│   ├─ Boot shim (Vite mini-SPA): polls /healthz, replaces location once  │
│   │  ready — surfaces stage transitions during cold start               │
│   ├─ WebKit webview: React 18 + TypeScript SPA served from sidecar      │
│   └─ Sidecar manager: spawn / wait_for_health(30s) / restart / kill     │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                ┌──────────────────┴───────────────────┐
                │  FastAPI sidecar  (Python 3.11+)     │
                │  ─────────────────────────────────   │
                │  - 100+ HTTP routes (/api, /ui, /)   │
                │  - SQLite WAL store + parquet sink   │
                │  - Awareness engine (~375 feeds,     │
                │    6 parsers, breaker + adaptive)    │
                │  - Real-time PUSH channel (WS+SSE,off)│
                │  - Awareness telemetry + blind-spot  │
                │  - QuantEngine + global-tone signal  │
                │  - Portfolio subsystem (read-only)   │
                │  - DeepSeek reviewer (opt-in)        │
                │  - Drive archiver (CSV export)       │
                │  - Webhook fan-out (SSRF-guarded)    │
                │  - SSE stream `/ui/stream`           │
                │  - psutil-backed runtime telemetry   │
                └──────────────────────────────────────┘
                                   │
                                   ▼
                ┌──────────────────────────────────────┐
                │  Local storage                       │
                │  ~/Library/Application Support/      │
                │   Catchem/data/db/catchem.sqlite3    │
                │   (WAL, PRAGMA user_version=2)       │
                │  + parquet rotation, JSONL DLQ, logs │
                └──────────────────────────────────────┘
```

| Layer | Tech | Source |
|---|---|---|
| Native shell | Rust + Tauri 2 | `desktop/catchem/src-tauri/src/*.rs` |
| Boot shim | Vite + vanilla TS | `desktop/catchem/web/` |
| Premium UI | React 18 + Vite 5 + TanStack Query + cmdk + ECharts | `frontend/src/**` |
| Sidecar | FastAPI 0.111 + uvicorn + sse-starlette | `src/catchem/api.py` |
| Storage | SQLite (WAL) + pyarrow parquet + JSONL DLQ | `src/catchem/storage.py` |
| Pipeline | pure-Python finance filter / zero-shot / sentiment / evidence / entity | `src/catchem/{pipeline,finance_filter,zero_shot_classifier,sentiment,evidence,entity_linker,symbol_mapper}.py` |
| Awareness engine | pluggable parsers + feed-provider registry + auto-discovered source packs | `src/catchem/news_poller.py`, `src/catchem/news_sources/**` |
| Real-time push | lazy-imported `websockets`/`httpx_ws` + httpx SSE | `src/catchem/ws_push.py` |
| Awareness intelligence | pure blind-spot detector + live-window telemetry | `src/catchem/awareness_gaps.py` |
| Quant | pandas + numpy + scikit-learn + rapidfuzz (+ GDELT global-tone) | `src/catchem/quant/**` |
| Portfolio | pure read-only holdings→awareness join | `src/catchem/portfolio.py` |

---

## 2. Data flow

```
              ┌──── Awareness JSONL inbox (post-commit hook upstream)
              │      env: CATCHEM_PATHS__AWARENESS_DATA_DIR
   captures ─┼──── Awareness ingestion engine (~375 feeds, 10s tick)
              │      news_poller.assemble_feeds() → 6 pluggable parsers
              │      (DEFAULT_FEEDS + auto-discovered news_sources/* packs)
              ├──── Real-time PUSH channel (ws_push.py, OFF by default)
              │      WebSocket + Wikimedia SSE → same ingest path
              └──── UI paste / upload  (/ui/demo/paste, /ui/demo/upload)
                     │
                     ▼
              Supervisor.process_capture()
                     │
   ┌─────────────────┼─────────────────────┐
   ▼                 ▼                     ▼
  text_extract   finance_filter      zero_shot_classifier
  (5 MB cap,     (deterministic      (asset_class / reason_code
   stdlib         keyword + score)    multi-label)
   HTMLParser)         │                     │
                       ▼                     ▼
                  sentiment.py        evidence.py + entity_linker
                  (lex polarity)      (sentence-level + KB lookup)
                       │                     │
                       └────────┬────────────┘
                                ▼
                  FinancialImpactRecord (pydantic, redaction-safe)
                                │
            ┌───────────────────┼───────────────────────┐
            ▼                   ▼                       ▼
       SQLite                 webhook.py              QuantEngine
       (records,              (high-relevance         (lazy build,
        record_labels,         fan-out)               16 signal modules,
        reviews, dlq,                                  ThreadPoolExecutor
        record_tags,                                   fan-out + 30s cache)
        offsets,                                         │
        model_versions)                                  ▼
            │                                       /api/quant/* routes
            ▼                                       hero narratives via
   archive.py → CSV → ~/Drive    DeepSeek reviewer (opt-in, async sampled)
   (every 30s, 17 cols)            │
                                   ▼
                                reviews table
                                (composite PK: capture_id, reviewer_id)
```

### Two redaction layers (defense in depth)

1. `pipeline.py` writes `diagnostic_multimodal_*` as `False / None` whenever `production_safe` mode is active.
2. `redaction.redact_record_for_mode()` re-scrubs every payload at the API boundary regardless of how the row was written.

### Reviewer dual-track

| Reviewer | Mode | Cost | Latency | Wire path |
|---|---|---|---|---|
| Stub (`reviewers/stub.py`) | Sync, deterministic | Free | <1 ms | Runs inline during `process_capture` |
| DeepSeek (`reviewers/deepseek.py`) | Async, sampled, budget-capped | USD/token | ~1–3 s | `/api/reviews/{capture_id}/run`, narrative endpoints |

Agreement scoring lives in `_compute_agreement()` (api.py:238): Jaccard over asset_classes / reason_codes / candidate_symbols, plus relevance/sentiment match and inverted score delta — surfaced on `/reviews`.

---

## 2a. Awareness ingestion engine

The original "53-feed RSS poller" has grown into a self-extending **awareness ingestion engine**: ~375 configured feeds across six parser types, assembled from a hand-curated core plus auto-discovered source packs. The loop is unchanged (`fetch → parse → dedup → finance_filter → ingest`); what scaled is the *plug-in surface* around it.

```
catchem/news_sources/*.py  (25 packs, auto-discovered)
   │  each calls at import time:
   │    register_feed_provider(fn -> [FeedSpec(..., parser="gdelt")])
   │    register_parser("gdelt", _parse_gdelt)
   ▼
news_poller.assemble_feeds()                      ← DEFAULT_FEEDS (53) + every provider
   │  de-dupe by name (DEFAULT_FEEDS wins collisions)
   ▼
AsyncNewsPoller loop (10s tick, interval floored to 10s)
   │  per-feed: _adaptive_cadence(consecutive_empty) → skip-multiplier ladder
   │            circuit breaker (5 fails → 60/300/900/1800/3600s backoff)
   ▼
get_parser(spec.parser)(body, fallback_domain) → list[ParsedItem]   (6 parsers)
   │  rss · gdelt · gdelt_gkg · hn_algolia · reddit · twitter
   ▼
dedup:  _canonical_url() LRU (_SeenCache)   +   _normalize_title() cross-source title window
   │      (strips www./trailing-slash/utm_*)     (strips " - Source" suffix; skips <5-char titles)
   ▼
finance_filter (deterministic keyword + score)  →  supervisor.process_capture()  →  SQLite / quant / UI
```

### Pluggable registries (`news_poller.py`)

| Mechanism | Symbol | Behavior |
|---|---|---|
| Parser registry | `register_parser(name, fn)` / `get_parser(name)` (`_PARSERS` dict) | Maps a `FeedSpec.parser` key to a `bytes -> list[ParsedItem]` function. `"rss"` is registered at import; packs add `"gdelt"`, `"gdelt_gkg"`, `"hn_algolia"`, `"reddit"`, `"twitter"`. Unknown key falls back to the rss parser. |
| Feed-provider registry | `register_feed_provider(fn)` (decorator; `_FEED_PROVIDERS` list) | Zero-arg callable returning extra `FeedSpec`s. `assemble_feeds()` merges `DEFAULT_FEEDS` + every provider, de-duped by name — a pack only ever *adds* a file, never edits the shared tuple, so parallel authorship stays collision-free. |
| `FeedSpec` | dataclass: `name`, `url`, `fallback_domain`, `parser="rss"` | The `parser` field is the join key to the parser registry. |
| Auto-discovery | `news_sources/__init__.py::_discover()` | `pkgutil.iter_modules` imports every non-`_` submodule so its `register_*` side effects fire. One broken pack is logged + skipped (`news_source_pack_failed`), never blocks the poller. `DISCOVERED` lists packs that loaded — surfaced in telemetry/tests. |
| Adaptive cadence | `_adaptive_cadence(consecutive_empty)` | Pure ladder mapping a feed's consecutive-empty count to a poll-cycle multiplier, so a chronically-quiet feed is fetched less often. Floor 10s tick is preserved. |
| Cross-source title dedup | `_normalize_title(title)` + a time-windowed `_seen_titles` `OrderedDict` | Lowercase, strip ONE trailing `" - Source"`/`" | Source"` attribution suffix, strip punctuation, collapse whitespace. Returns `""` (bypass) for titles under `_TITLE_DEDUP_MIN_CHARS` (5). Collapses the SAME story carried by multiple outlets within `dedup_title_window` seconds. |
| Canonical-URL dedup | `_canonical_url(url)` + `_SeenCache` LRU | Strips `www.`, trailing slash, and tracking params (`utm_*`/`gclid`/`fbclid`/`mc_*`) before keying, so the same article surfaced via multiple feeds dedups once. Backstopped by the storage PRIMARY KEY. |

### Source packs (`news_sources/`, 25 auto-discovered)

`gdelt`, `gdelt_gkg`, `hn_algolia`, `reddit`, `x_twitter`, `gnews_global`, `gnews_sectors`, `gnews_tickers`, `global_wires`, `regulators`, `crypto_depth`, `tech_depth`, `macro`, `regional_em`, `earnings_ir`, `healthcare`, `govdata`, `sectors_defense_travel`, `sectors_realestate_industrials`, `intl_filings`, `commodities_energy`, `watchlist_dynamic`, `podcasts`, `youtube`, `specialist`.

Live composition (`assemble_feeds()`): **375 total feeds** — by parser: `rss` 341 · `twitter` 12 · `reddit` 7 · `gdelt_gkg` 6 · `hn_algolia` 6 · `gdelt` 3. The poller can be disabled with `CATCHEM_NEWS__POLLER_ENABLED=false`.

---

## 2b. Real-time PUSH channel

`src/catchem/ws_push.py` adds a latency-optimized complement to the poll loop: long-lived WebSocket (and Server-Sent-Events) readers that ingest a frame the instant a source emits it, instead of waiting for the next 10s tick. It is **OFF by default** and adds **no hard dependency**.

| Aspect | Detail |
|---|---|
| Opt-in | `settings.news.websocket_enabled` is `False` and `websocket_sources` is empty; the channel constructs but connects to nothing until the operator flips it. |
| Transports | `WsSourceSpec.kind` ∈ `{"ws", "sse"}`. WS needs an importable client lib (`websockets` / `httpx_ws`, lazy-imported); if neither is present every WS-kind source is marked `disabled` while SSE sources (httpx, always present) still run. |
| Frame parsers | `WsSourceSpec.parser` ∈ `{"generic", "wikimedia"}`. `parse_ws_message` is the tolerant squawk/news-frame probe; `parse_wikimedia_event` filters the Wikimedia recentchange firehose to finance/company-relevant edits. |
| Default source | `DEFAULT_WS_SOURCES` = one entry: Wikimedia EventStreams SSE (`stream.wikimedia.org/v2/stream/recentchange`) — a genuine public, auth-free stream that proves the SSE path end-to-end. Used only when enabled with no explicit sources. |
| Same ingest path | Each parsed frame goes through `build_capture → write_jsonl → supervisor.process_capture` — identical to the poller — reusing `_canonical_url` LRU + deterministic `capture_id`, so a WS arrival dedups against a polled one within the process. |
| Resilience | Per-source exponential backoff (`WS_BACKOFF_LADDER_SECONDS` 1/2/5/15/30/60s); a malformed frame, dead source, or parse error never tears down the channel or another source's task; clean cancel on `stop()`. |
| Telemetry | `WebSocketNewsChannel.stats()` → `{schema_version, enabled, library, running, sources_total, connected_count, messages_received, ingested, last_message_at, sources[...]}` exposed at `GET /api/news/ws-status` (`{"enabled": false}` when off). |

---

## 2c. Awareness telemetry + blind-spot detection

Two awareness-intelligence surfaces invert the firehose: instead of "what arrived?", they answer "how far back does *now* reach?" and "what am I **not** seeing?".

**Live awareness window** — `GET /api/news/awareness` tallies the configured feeds two ways (`sources_by_parser` from `FeedSpec.parser`; `sources_by_category` via `_classify_feed_category()` into watchlist / tickers / wire / regulator / crypto / macro / social / google_news / global_firehose / regional / specialist / podcast / video) and estimates the effective awareness span as `window_estimate_seconds ≈ poll_interval + median_publisher_lag`. Degrades to a 200 with `sources_total: 0` and null lags when the poller is not built. The `catchem awareness` CLI prints the breadth offline (sources × parsers × cadence); `catchem awareness --live` queries the running sidecar's endpoint for the live window.

**Blind-spot detector** — `src/catchem/awareness_gaps.py::find_coverage_gaps()` is a pure, deterministic, I/O-free function. Given storage records + a watchlist of terms (tickers / keywords / sectors) and an injected `now`, it classifies each watched term as **covered** (with `last_seen_age_seconds` freshness + `mention_count`, sorted freshest-first) or a **gap** (zero in-window mentions). Matching is case-insensitive: substring over text fields (`title`/`text`/`text_excerpt`/`summary`/`body`) OR exact token over symbol-list fields (`symbols`/`candidate_symbols`/`tickers`/`candidate_entities`). Tolerant of malformed records (never raises). Surfaced at `GET /api/news/coverage-gaps` (watchlist from `settings.news.priority_tickers`, fallback mega-cap set) and the `catchem coverage-gaps` CLI.

---

## 2d. Global-tone signal & portfolio subsystem

**Global-tone macro signal** — `src/catchem/quant/global_tone.py` reads GDELT DOC 2.0 `mode=TimelineTone` (the free, no-auth average-article-tone timeline) as the macro-sentiment complement to the corpus-local sentiment signals: where `sentiment_momentum` reads catchem's own ingested records, `global_tone` reads the entire global press firehose. Three entry points: `summarize_tone()` (pure — latest/mean/min/max + recent-window trend slope + `tone_state` ∈ improving/deteriorating/stable, threshold `_STATE_THRESHOLD=0.5`), `fetch_tone()` (async GET, fail-soft → `[]`), and `compute_global_tone()` (orchestrator fanning out over `DEFAULT_THEMES` = markets / economy / crypto / fed, rolling per-theme latests into `overall_tone` + `overall_state`). Surfaced at `GET /api/quant/global-tone` with a ~120s in-process TTL cache (comfortably inside GDELT's ~15-min re-index) and a `degraded: true` flag when every theme's fetch fails — it never 500s on an upstream outage. Unlike the dashboard signals it is called directly by its endpoint, not through the `QuantEngine` fan-out.

**Portfolio subsystem** — a **read-only** holdings tracker: analyst-entered positions joined to the awareness/quant layers for context. **No order execution, no money movement** — purely a watchlist with cost-basis bookkeeping.

- **Storage**: migration #3 (`add_portfolio_table`) creates `portfolio` (`id` PK AUTOINCREMENT, `symbol` NOT NULL, nullable `label`/`shares`/`weight`/`cost_basis`/`notes`, `added_at`) + `idx_portfolio_symbol`. CRUD via `storage.{list_holdings,add_holding,delete_holding}`.
- **Enrichment**: `src/catchem/portfolio.py::enrich_holdings()` is pure + deterministic (injected `now` + `quote_fn`). Each holding is annotated with `recent_news_count`, `recent_top` (up to 3 highest-relevance matching records), `coverage` (reusing `awareness_gaps` match rules so news count and coverage flag never disagree), and `quote` (`{last, prev_close, change_pct}`, failure-tolerant — a flaky provider collapses to `None`).
- **Surfaces**: `GET /api/portfolio` (list), `POST /api/portfolio` (add), `DELETE /api/portfolio/{holding_id}`, `GET /api/portfolio/enriched` (join over recent records + fixture quote). Frontend route `/portfolio` (`features/portfolio/PortfolioPage`). CLI Typer sub-app `catchem portfolio {list,add,remove,show}`.

---

## 3. Quant signal catalog

`src/catchem/quant/` ships 16 signal modules (plus the `engine.py` facade); a 17th calibration signal lives in top-level `backtest.py`. `QuantEngine.dashboard_snapshot()` fans the dashboard subset out in parallel via `ThreadPoolExecutor(max_workers=6)`; per-request signals warm a 30 s in-memory cache keyed by call site (lock-guarded since v45).

| Signal | Module | Method |
|---|---|---|
| `event_clustering` | `quant/event_clustering.py` | Min-hash + cosine over title/text tokens; gated by `min_distinct_domains` to suppress single-publisher echoes. |
| `market_reaction` | `quant/market_reaction.py` | Per-record horizon-return lookup against `market_data.LocalFixtureMarketDataProvider`; n-hour deltas. |
| `source_reliability` | `quant/source_reliability.py` | Per-domain leaderboard combining volume, freshness, and historical relevance hit-rate. |
| `novelty` | `quant/novelty.py` | Inverse cosine to nearest-N corpus neighbors; corpus + per-record scores. |
| `lead_lag` | `quant/lead_lag.py` | Per-event timestamp ordering across publishers; surfaces who broke a story first. |
| `topic_regime` | `quant/topic_regime.py` | Bucketed asset-class distributions + KL-divergence vs prior bucket; adaptive threshold. |
| `sentiment_momentum` | `quant/sentiment_momentum.py` | Per-ticker rolling sentiment slope across timed buckets. |
| `co_occurrence` | `quant/co_occurrence.py` | Symbol↔symbol edges, asset-class concentration, asset↔reason heatmap cells. |
| `anomaly` | `quant/anomaly.py` | Z-score over volume + sentiment series with explicit `SymbolBurst` callouts. |
| `spillover` | `quant/spillover.py` | Cross-asset transmission edges weighted by lead/lag + co-occurrence. |
| `sentiment_dispersion` | `quant/sentiment_dispersion.py` | Within-cluster sentiment disagreement (entropy + dominant label) — v35. |
| `symbol_correlation` | `quant/symbol_correlation.py` | Pearson over time-bucketed sentiment vectors; pair table — v36. |
| `news_velocity` | `quant/news_velocity.py` | Bucketed arrival rate with z-score regime classification — v37. |
| `market_time` | `quant/market_time.py` | Session-of-day clustering (pre-open / RTH / post / overnight) — v32. |
| `intensity` | `quant/intensity.py` | Sentiment-weighted finance-relevance intensity per record; scope aggregations — v40. |
| `arrival_heatmap` | `quant/arrival_heatmap.py` | 24h × 7-day cell grid of news arrival volume; weekend dips and session-open peaks — v41. |
| `backtest` | `backtest.py` (top-level) | Score-bin calibration over historical records: hit-rate, lift, abs-error — v31. |

Each module returns a `@dataclass` payload; `engine._dataclass_to_dict` serializes for the API. Failures inside a signal are caught by `_safe_call` and logged — the dashboard never 500s because of one bad signal.

### Fail-soft observability (v72–v74)

`_safe_call` historically swallowed a failing signal into a single `logger.warning(...)` and returned `None`. That keeps the dashboard alive (one broken signal must not block the others) but made degradation invisible at runtime. The engine now records every failure into a process-wide, thread-safe ring buffer so the silent fail-soft path is observable end-to-end.

```
QuantEngine signal (e.g. spillover)
        │  raises
        ▼
_safe_call(fn, …, label="spillover")          quant/engine.py
   │  except Exception
   ├─ logger.warning("quant_signal_failed", …)  (structured log line — full traceback)
   └─ _record_failure(label, exc, elapsed_ms)
            │
            ▼
   _SIGNAL_FAILURES  (deque, maxlen=50, _SIGNAL_FAILURES_LOCK)
     entry = {signal, error_class, error, traceback_head[-600:], elapsed_ms, ts}
            │
   QuantEngine.diagnostics()  →  {total_failures, per_signal{}, recent[] (newest first), buffer_capacity}
            │
   ┌────────┼─────────────────────────────────┐
   ▼        ▼                                  ▼
GET /api/quant/diagnostics   catchem signals --diagnostics    DegradedSignalsPill.tsx
(api.py — wraps engine        (cli.py — HTTP to the running    (QuantScan hero chip)
 diagnostics() with           sidecar, NOT a fresh engine,
 schema_version + generated_at) so it reads the live buffer)
```

| Surface | Source | Behavior |
|---|---|---|
| Ring buffer | `quant/engine.py` — `_SIGNAL_FAILURES` (`deque(maxlen=50)`) + `_SIGNAL_FAILURES_LOCK`; `_record_failure()` / `_diagnostics_snapshot()` / `_diagnostics_clear()` (test-only). | Per-process, thread-safe. Each entry: `signal`, `error_class`, `error`, `traceback_head` (last ~600 chars), `elapsed_ms`, ISO `ts`. Full traceback stays in the structured log line. |
| `QuantEngine.diagnostics()` | `quant/engine.py` | Returns `total_failures`, `per_signal` (counts keyed by label), `recent` (newest-first), `buffer_capacity`. |
| HTTP | `GET /api/quant/diagnostics` (`api.py`) | Wraps `engine.diagnostics()` with `schema_version: 1` + `generated_at`. `total_failures=0` is the healthy steady state. |
| CLI | `catchem signals --diagnostics` / `-d` (`cli.py::_signals_diagnostics`) — v73 | Queries the running sidecar's `/api/quant/diagnostics` over HTTP (host/port from `settings.api.*`) rather than building a fresh engine (which would read an empty buffer). Prints "all signals nominal — failure buffer empty" when clean; otherwise per-signal counts (descending) + the 5 most recent errors. `--json` emits the raw payload. |
| UI chip | `frontend/src/features/quant/DegradedSignalsPill.tsx` — v72/v74 | Renders nothing when diagnostics are undefined or `total_failures === 0`; otherwise a warn-toned `⚠ {n} signals degraded` chip on the QuantScan hero. Tooltip lists `signal: count` highest-first (same descending order as the CLI). Narrow `DegradedDiagnostics` prop type (`total_failures` + `per_signal`). Label routes through `t("quant.degraded.label")` — v74 fixed a v72 leak that baked a Turkish string into JSX, defeating the i18n layer. |

---

## 4. Frontend routes

Routes are declared in `frontend/src/app/App.tsx` (route-based code-splitting via `React.lazy`). Chord registry: `frontend/src/lib/nav-shortcuts.ts` (canonical, consumed by Shell handler, CommandPalette, HelpPage, SettingsPage — Round 7 collapsed the four prior copies).

| Route | Component | Chord | Purpose |
|---|---|---|---|
| `/` | `OverviewPage` | `g o` | Landing dashboard: KPI tiles, narrative hero, recent rows, top symbols/reasons. |
| `/feed` | `FeedPage` | `g f` | Live record feed with bulk multi-select, filter chips, SSE-driven updates. |
| `/feed/:captureId` | `FeedPage` (drawer) | — | Deep-link to a record's detail drawer. |
| `/replay` | `ReplayUploadPage` | `g r` | Paste / upload article → demo pipeline; replay Awareness JSONL inbox. |
| `/map` | `MarketMapPage` | `g a` (alias `g m`) | Analysis map: narrative hero, cluster grid, symbol/reason heatmap. |
| `/symbols` | `SymbolsPage` | `g s` | Symbol leaderboard with top-symbol insight hero. |
| `/symbols/:symbol` | `SymbolDetailPage` | — | Per-symbol drill: reason/sentiment breakdown + record list. |
| `/tags` | `TagsPage` | `g t` | User-tag rollup (aggregation views over `record_tags`) — v38/v39. |
| `/benchmark` | `BenchmarkPage` | `g b` | Calibration vs golden dataset, per-axis trend visuals. |
| `/backtest` | `BacktestPage` | `g k` | Score-bin lift, hit-rate, abs-error charts — v31. |
| `/reviews` | `ReviewsComparePage` | `g v` | Stub ↔ DeepSeek side-by-side diff, agreement matrix. |
| `/scan` | `QuantScanPage` | `g q` | Quant cockpit: hero narrative (streamed), 4 tabs (Events / Sentiment / Sources / Network), KPI history, arrival-heatmap calendar — v41. |
| `/model-controls` | `ModelControlsPage` | `g c` | Mode + ML stub toggles, guard-status panel. |
| `/ops` | `OpsPage` | `g x` | Sidecar/news/archiver/stats panels, DLQ insight, log tail link. |
| `/logs` | `LogsPage` | `g l` | Live sidecar log tail (polled from `/ui/log-tail`). |
| `/sources` | `SourcesPage` | `g u` | Per-feed news health: status, freshness, consecutive errors, manual probe button — v42/v44. |
| `/settings` | `SettingsPage` | `g ,` | DeepSeek reviewer card, webhook config, DB backup, snapshot import/export, theme accent picker. |
| `/help` | `HelpPage` | `g h` | Chord reference, page-help dictionary, jargon lookup. |
| `/analysis` | redirect → `/map` | — | Portmanteau alias. |

Shell-level chrome: `Shell.tsx` (header, sidebar, route fade-in via `animate-page-enter`), `CommandPalette` (cmdk, fuzzy, recents), `SearchPalette` (records/symbols/clusters; stale-closure-free since v50), `HelpDrawer` (`inert` attribute applied when closed for a11y — v49), `NotificationCenter`, `ToastTray` (CSS-only reduced-motion since v50), `OnboardingModal`, `ShortcutOverlay` (`?`), `SidecarBanner` (disconnect/reconnect graceful UI), `AppErrorBoundary` + per-route `RouteErrorBoundary`. Cross-window state sync via `frontend/src/lib/storage-sync.ts::useStorageSync` — secondary windows mirror watchlist, theme, accent, saved-search edits in real time via `storage` events — v42.

`SymbolDetailPage` (v51 enrichment): sentiment trend area chart (7d slice of 30d window), reason-code distribution bar, mention-velocity sparkline (30d daily counts) — driven by `/api/symbols/{symbol}/sentiment-trend`.

### i18n layer (v31) + EN↔TR parity gate (v71)

`frontend/src/lib/i18n.ts` is a dependency-free locale layer (no react-i18next / formatjs) backing two locales: `en` (default, complete) and `tr` (Türkçe). It is intentionally not full ICU MessageFormat — scope is "translate the surface the user sees most".

- **Store**: a module-level `currentLang` + subscriber set exposed through React's `useSyncExternalStore` contract (`useLang()`), so concurrent rendering tears correctly with no Context provider — non-component code (`snapshot.ts`, `api.ts` error strings) can call `t()` at import time too.
- **API**: `t(key)` (lookup), `getLang()` / `setLang(lang)` (synchronous read / persist + broadcast), `useLang()` (re-render hook). Test-only escape hatches: `_testResetLang()`, `_testKeysForLocale(lang)`.
- **Persistence**: `localStorage` key `catchem.lang` (constant `I18N_KEY`). `setLang` mirrors the value onto `<html lang>` for screen readers and coerces unknown locales to `en` so a tampered storage value can't poison the next session. Default is `en` — explicit user choice, no auto-detect.
- **Fall-through**: missing key in the active locale → English string; missing in both → the key itself (so a typo renders `nav.typo` in the DOM, never `undefined`). Keys are namespaced `namespace.subkey` (`nav.*`, `common.*`, `settings.*`, `feed.*`, `overview.*`, `ops.*`, `benchmark.*`, `backtest.*`, `shortcuts.*`, `quant.*`).
- **Parity gate** (`frontend/src/tests/i18n.test.ts`): the EN-fallthrough that prevents `undefined` at runtime also masks translation debt at review time — an EN-only string slips into TR-mode UI without showing in the PR diff. The v71 block compares the sorted key sets of `en` and `tr` and fails CI the moment a key exists in one locale but not the other (`TR_ALLOW_MISSING` / `EN_ALLOW_MISSING` are intentionally empty). It also asserts no orphan (un-namespaced) keys, and that placeholder-bearing TR strings differ byte-wise from EN (catches copy-pasted "translations") and that `{tables}/{indexes}/{sizeMb}/{days}/{scopes}` substitution slots survive.

---

## 5. API endpoints

107 routes total (`grep "@app." src/catchem/api.py | wc -l`). Categorized by prefix:

### Health & discovery
- `GET /healthz` — light liveness (always 200 once process is alive)
- `GET /api/health/deep` — deep readiness: uptime / sqlite / news_poller / schema_version / disk; 503 on any failure
- `GET /api/_index` — programmatic route listing for the help/debug surface
- `GET /api/docs`, `GET /api/redoc`, `GET /api/openapi.json` — Swagger/ReDoc/OpenAPI (mounted under `/api/*` so they never shadow the SPA)
- `GET /config`, `GET /metrics`, `GET /api/stats` — runtime telemetry. `/api/stats` returns a `process` sub-object with `rss_bytes`, `vms_bytes`, `cpu_percent` (50ms sample), `num_threads`, `num_fds`, and `psutil_available`. Falls back to POSIX `resource` shape (matched field names, `psutil_available=false`) on builds where psutil is missing — v40/v41.

### Records
- `GET /recent`, `GET /dashboard` — list/overview
- `GET /record/{capture_id}` — typed detail
- `GET /records/by-symbol/{symbol}`, `/by-asset-class/{ac}`, `/by-reason/{rc}` — label drills
- `POST /replay` — kick off JSONL replay (bounded 1..1_000_000)
- `POST /process-one` — synchronous capture → record (validates via `AwarenessCaptureView`)

### Tags (v38)
- `GET /api/records/{capture_id}/tags`
- `POST /api/records/{capture_id}/tags`
- `DELETE /api/records/{capture_id}/tags/{tag}`
- `GET /api/tags` — top tags rollup
- `GET /api/tags/{tag}/records` — records carrying a tag

### Demo capture (UI ergonomic surfaces)
- `POST /ui/demo/paste` — title + text + url → demo pipeline
- `POST /ui/demo/upload` — `.txt`/`.md`/`.html`/`.jsonl`/`.json` upload with safe text extract (5 MB cap)

### Reviewers
- `GET /api/reviews/status`, `/api/reviews/spend-history`
- `POST /api/reviews/{capture_id}/run`
- `GET /api/reviews/compare`
- `PATCH /api/reviews/settings`

### Quant
- `GET /api/quant/dashboard` — single-call snapshot (parallel signal fan-out)
- `GET /api/quant/{clusters,sources,novelty,lead-lag,regime,sentiment-momentum,sentiment-dispersion,intensity,co-occurrence,anomalies,spillover,symbol-correlation,news-velocity,market-time,arrival-heatmap}` — per-signal
- `GET /api/quant/novelty/{capture_id}`, `/api/quant/reaction/{capture_id}`, `/api/quant/record/{capture_id}/detail`
- `GET /api/quant/cluster/{cluster_id}/members`, `/api/quant/heatmap/records`
- `GET /api/quant/live-read`, `/api/quant/live-read-stream` — narrative hero (deterministic fallback + DeepSeek streaming; rate-limited)
- `GET /api/quant/diagnostics` — fail-soft observability: last-50 signal-failure ring buffer + per-signal counts (`schema_version` + `generated_at` envelope around `engine.diagnostics()`) — v72
- `GET /api/quant/global-tone` — GDELT `TimelineTone` macro-sentiment lens: per-theme (markets/economy/crypto/fed) latest/mean/trend/state + rolled-up `overall_tone`/`overall_state`; ~120s TTL cache; `degraded: true` (never 500s) on upstream outage. Standalone — not part of the dashboard fan-out.
- `POST /api/quant/explain` — per-signal narrative explainer
- `POST /api/quant/invalidate` — cache bust

### Search & export
- `GET /api/search` — global palette (records / symbols / clusters; rate-limited)
- `GET /api/export/{records,reviews,quant}` — CSV/JSON exports (rate-limited)
- `GET /api/db/info`, `/api/db/schema_version`, `/api/db/export` — backup surface
- `POST /api/db/import` — DB replace (heavy; rate-limited, auto-backup keep-last-2)

### Webhook
- `GET /api/webhook/config`, `POST /api/webhook/config`
- Internal: `webhook.should_send()` gates by relevance + symbol allow-list; `send_webhook()` posts a Slack/Discord-shaped payload.

### Backtest
- `GET /api/backtest` — score-bin calibration

### Portfolio (read-only — no order execution)
- `GET /api/portfolio` — list analyst-entered holdings
- `POST /api/portfolio` — add a holding (`symbol` required; rest nullable) → 201
- `DELETE /api/portfolio/{holding_id}` — remove (404 if absent)
- `GET /api/portfolio/enriched` — holdings joined to recent records + coverage + live quote via pure `portfolio.enrich_holdings()`

### UI-internal aggregations (`/ui/*`) and per-feed health
- `/ui/app-info`, `/ui/sidecar-status`, `/ui/log-tail` — sidecar metadata
- `/ui/summary`, `/ui/facets`, `/ui/timeline`, `/ui/top-symbols`, `/ui/top-reasons`, `/ui/trends`, `/ui/matrix`, `/ui/guards`
- `/ui/quotes`, `/ui/quote/{symbol}` — fixture-backed market data
- `/ui/benchmark/latest`, `/ui/benchmark/history`
- `/ui/symbol/{symbol}` — per-symbol roll-up
- `/api/symbols/{symbol}/sentiment-trend` — bucketed sentiment + reason distribution + mention velocity for SymbolDetail enrichment — v51
- `/ui/news-status`, `/ui/news-poll-now` — poller diagnostics + force tick
- `/api/news/sources` — per-feed health array (status, last_ok, consecutive_errors, cooldown_until, sample_titles) — v42
- `POST /api/news/sources/probe` — single-feed manual probe (rate-limited bucket) — v44
- `GET /api/news/awareness` — live awareness window: `sources_by_parser` + `sources_by_category` breadth, `poll_interval_seconds`, median/avg publisher lag, `window_estimate_seconds` (≈ interval + median lag). Degrades to `sources_total: 0` when the poller is unbuilt.
- `GET /api/news/coverage-gaps` — blind-spot detector: watched terms classified `covered` (freshness + mention count) vs `gaps`; watchlist from `settings.news.priority_tickers`. Pure `awareness_gaps.find_coverage_gaps()` over recent records.
- `GET /api/news/ws-status` — real-time PUSH channel diagnostics (`{"enabled": false}` when off; full per-source stats envelope when on).
- `/ui/archive-status`, `/ui/archive-now` — Drive archiver diagnostics + force run
- `/ui/stream` — Server-Sent Events: `summary` (on count change or every 30s) + `tick` heartbeat every 3s

### SPA serving
- `GET /` — premium SPA with per-request CSP nonce
- `GET /assets/*` — static mount
- `GET /favicon.ico`
- `GET /legacy`, `/legacy-dashboard` — vanilla dashboard (kept until premium fully parities)
- `GET /replay` — explicit SPA shell handler (POST stays for API)
- `GET /{full_path:path}` — history-mode SPA fallback (with reserved-prefix guard: any `/api/...` 404 is honored verbatim, never papered over)

---

## 6. Storage schema

**Location**:
- Dev: `<repo>/data/db/catchem.sqlite3`
- Release: `~/Library/Application Support/Catchem/data/db/catchem.sqlite3`

**Engine**: SQLite, `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`, `PRAGMA foreign_keys=ON` (enforced per-connection since v45 so `record_tags` cascade-deletes on record removal), `isolation_level=None`. Process-wide `threading.RLock` guards all connections (`Storage._lock`).

### Tables

| Table | Purpose | Notes |
|---|---|---|
| `schema_version` | Legacy single-row bootstrap | Superseded by `PRAGMA user_version`; retained for backward compat. |
| `records` | One row per `FinancialImpactRecord` | PK `capture_id`. JSON columns for list-shaped fields. Indexed on relevance+`created_at`, domain, `published_ts`. |
| `record_labels` | Inverted index for multi-label filter | `(capture_id, kind ∈ {asset_class, reason_code, symbol, entity, horizon}, value)`. |
| `record_tags` | User-defined free-form tags | Added by migration v2. PK `(capture_id, tag)`, FK CASCADE on records. Validator: `^[a-zA-Z0-9_\-.]+$`, ≤50 chars. |
| `reviews` | Reviewer outputs (stub + DeepSeek + future) | Composite PK `(capture_id, reviewer_id)`. Tracks input/output tokens, USD cost, latency, error_code. |
| `dlq` | Failed-capture queue | Auto-increment id, payload excerpt, error string. |
| `offsets` | Awareness JSONL replay cursor | PK `source_path`; tracks `line_offset` + `last_capture_id`. |
| `model_versions` | Per-component model versions | PK `component`; surfaces under `/ui/app-info`. |
| `portfolio` | Read-only analyst holdings (watchlist + cost basis) | Added by migration v3. `id` PK AUTOINCREMENT, `symbol` NOT NULL (join key), nullable `label`/`shares`/`weight`/`cost_basis`/`notes`, `added_at`. `idx_portfolio_symbol`. No order execution. |

### Migration registry — `src/catchem/migrations.py`

Versioned migrations applied via `PRAGMA user_version`. Append-only, idempotent (each uses `IF NOT EXISTS`/`IF EXISTS`); each step runs inside its own transaction; `user_version` bumps only after script success.

| Version | Name | Effect |
|---|---|---|
| 1 | `baseline_records_table` | No-op claim; the pre-migration bootstrap already created every table. |
| 2 | `add_record_tags_table` | Creates `record_tags` + 2 indices. |
| 3 | `add_portfolio_table` | Creates read-only `portfolio` table + `idx_portfolio_symbol`. **Current max**. |

`api_health_deep()` cross-checks `current_version(conn)` vs `max_known_version()` and reports `schema_outdated` if behind.

---

## 7. Security model

- **Local-first**: sidecar binds `127.0.0.1` (configurable via `CATCHEM_API__HOST`). No outbound traffic except clicked article links (opened in system browser by Tauri's `OpenExternal`) and optional DeepSeek / webhook calls.
- **CSP**: per-request nonce on HTML responses, emitted by the root SPA handler itself (v36 fix — earlier middleware path was overwritten by FastAPI's response framing). Header is strict: `script-src 'self' 'nonce-<random>'` — `'unsafe-inline'` is deliberately omitted for scripts. Non-HTML responses fall through to a default `script-src 'self'` policy without the nonce. Style-src retains `'unsafe-inline'` only because React inlines `style={...}` JSX props.
- **SSRF defense (v45)**: every outbound webhook URL passes through `webhook._ssrf_guard` before being dispatched. Hostnames `localhost`, `ip6-localhost`, `ip6-loopback` are hard-rejected; everything else is resolved and the resulting IPs checked against `is_private` (RFC 1918 / RFC 4193), `is_loopback` (127.0.0.0/8, ::1), `is_link_local` (169.254.0.0/16 + fe80::/10, which covers the AWS metadata endpoint), `is_multicast`, and `is_unspecified`. Any hit raises before the HTTP client opens the socket.
- **Webview navigation**: `desktop/catchem/src-tauri/src/security.rs::classify_navigation` is a pure function with unit tests. Same-origin and `tauri://`/`asset://`/`ipc://` allowed in-webview; safe http(s) routed to system browser; `javascript:`/`file:`/`data:`/unknown schemes blocked.
- **Rate limiting**: token-bucket per client (`rate_limit.py`).
  - `DEFAULT_BUCKET` 60 req/min
  - `SEARCH_BUCKET` 30 req/min (cost 1)
  - `DB_IMPORT_BUCKET` 6 req/min, cost 5 → ~1 import/min
  - `live-read` endpoints cost 2 (halves DeepSeek-priced burst)
  - `db/export` cost 3 (scales per-disk-read load)
- **Snapshot import/export** (UI): `frontend/src/lib/snapshot.ts` defines a strict `SNAPSHOT_KEYS` allow-list (theme, accent, watchlist, palette recents, saved searches, KPI history, onboarding flag, alert prefs). Keys outside the list are silently dropped on import — no attacker-supplied localStorage planting. Webhook URLs, API keys, and runtime state are explicitly NOT exported.
- **DeepSeek API key**: loaded via pydantic-settings (`env_file=".env"`); never written to snapshot, log, or stats payload. `/api/stats` exposes spend only (no key).
- **Production-safe redaction**: two layers (`pipeline.py` + `redaction.redact_record_for_mode()`). Diagnostic multimodal payloads scrubbed at the API boundary regardless of how the row reached storage.
- **NewsImpact guard**: read-only governance snapshot check via `newsimpact_guarded_adapter.snapshot_guard_state`; sidecar pins `newsimpact_diagnostic_enabled=False` and `safe_guard_view` strips `governance_index_path` from UI surfaces.
- **Authentication**: none — single-user local cockpit, trust localhost. Localhost-only bind + no cross-origin = no CSRF surface for POST endpoints.

---

## 8. Build & deploy

### Bootstrap (dev)

```bash
bash scripts/catchem_bootstrap_and_run.sh
```

Creates venv → `pip install -e ".[dev]"` → `(cd frontend && npm install && npm run build)` → copies bundle to `src/catchem/static/app/` → starts sidecar.

### Release build (.app)

The repo expects two scripts when shipping a native bundle:
- `scripts/build_catchem_release.sh` — wheel + PyInstaller `--onedir` sidecar + Vite production bundle + Tauri `--release` + ad-hoc codesign + `inject_info_plist.sh` (writes `NSDesktopFolderUsageDescription` so TCC doesn't silently hang `open()` on Desktop repos).
- `scripts/install_catchem.sh --release` — copies the freshly built `.app` to `/Applications/Catchem.app` with auto-backup-rotation (keeps last 2 prior builds in `~/Library/Application Support/Catchem/backup/`).

### Data paths

| Layer | Dev | Release |
|---|---|---|
| Sidecar python | `<repo>/.venv/bin/python` | `Catchem.app/Contents/Resources/sidecar/catchem-sidecar` (PyInstaller `--onedir`) |
| Sidecar cwd | repo root | `~/Library/Application Support/Catchem/` |
| Output dir | `<repo>/data/` | `~/Library/Application Support/Catchem/data/` |
| Awareness inbox | `CATCHEM_PATHS__AWARENESS_DATA_DIR` (default off) | same env, defaults to `~/Library/Application Support/Catchem/awareness-data/` |
| Logs | `<repo>/data/logs/api.out` + `sidecar.log` | `~/Library/Logs/Catchem/sidecar.log` |
| Config (yaml) | `<repo>/configs/` | shipped inside the PyInstaller bundle |

Release-mode env vars are set by `desktop/catchem/src-tauri/src/sidecar.rs::start()` when `cfg.release_mode == true`.

### Boot diagnostics — `boot.log` breadcrumbs (v70)

`env_logger` writes to stderr, but when a release-built `.app` is launched via Finder/Spotlight/`open`, launchd silently discards stderr — it is not even piped to the unified `log show`. So `log::error!("sidecar start failed")` looks like it fires yet produces nothing observable, which made cold-start failures undebuggable in the field.

`desktop/catchem/src-tauri/src/lib.rs::boot_log(stage, msg)` fixes this by appending a `[unix_ts] stage: msg` line to `~/Library/Logs/Catchem/boot.log`. The file is opened append-mode so successive launches stack, and the helper swallows its own I/O errors so a logging failure can never crash the host. Breadcrumbs are emitted at every boot milestone:

| Stage | Breadcrumb(s) |
|---|---|
| `run` | `entered` on entry; `tauri runtime ERROR: …` from the `.run()` `unwrap_or_else` (replaces an `.expect()` panic that launchd would have eaten); `exited (normal)` on clean shutdown. |
| `setup` | `closure invoked` → `resource_dir=…` → `resolve_sidecar ERROR: …` (on failure) or `resolved python=… cwd=… release=…` → `calling sidecar.start()` → `sidecar.start() OK` / `sidecar.start() ERROR: …`. |
| `setup` (health) | `wait_for_health: HEALTHY ({ms}ms)` or `wait_for_health: NOT HEALTHY after {ms}ms (status=… err=…)` — the block-on readiness probe capped at `DEFAULT_HEALTH_TIMEOUT` (30 s). |

The window is then built pointing at the boot shim (`index.html`), which polls `/healthz` and `window.location.replace`s to the sidecar URL once it returns 200; the `on_navigation` classifier OKs that same-origin jump. The `boot.log` trail means that even if the shim never loads (e.g. bundle missing), the operator can read exactly which stage failed on the next launch.

---

## 9. Production hardening

Layers added in v40–v45 to keep the sidecar honest on long-running laptops:

| Layer | Source | Behavior |
|---|---|---|
| News-poller circuit breaker | `news_poller.py` (`CIRCUIT_BREAKER_THRESHOLD=5`, `BACKOFF_LADDER_SECONDS=(60, 300, 900, 1800, 3600)`) | After 5 consecutive failed fetches a feed enters a 5-tier exponential cooldown (1m → 5m → 15m → 30m → 1h, then sticks at 1h). Cooldown is O(1) per tick; one successful fetch clears state — v43. |
| Manual probe | `POST /api/news/sources/probe` (rate-limited `_rate_limit_probe`) | Bypasses cooldown for a single feed; failed probe re-enters the ladder, successful probe closes the breaker — v44. |
| Log rotation | `logging.py` (`LOG_ROTATION_MAX_BYTES=5*1024*1024`, `LOG_ROTATION_BACKUP_COUNT=3`) | `RotatingFileHandler` keeps `catchem.log` + 3 generations on disk (≈20 MB ceiling); reduces noise on always-on machines — v41. |
| Backup rotation | `scripts/install_catchem.sh --release` | Keeps the last 2 prior `.app` builds in `~/Library/Application Support/Catchem/backup/` so a bad release can be rolled back manually — v18. |
| FK enforcement | `storage.py` per-connection `PRAGMA foreign_keys=ON` | Makes `record_tags` ON DELETE CASCADE actually fire; without this the PRAGMA defaults off in SQLite — v45. |
| Telemetry | `runtime_metrics.py` (psutil branch + POSIX `resource` fallback) | Surfaces RSS / VMS / sampled CPU% / thread count / fd count via `/api/stats`; wire shape is stable across both branches — v40/v41. |
| Stats cache | `api.py` TTL cache around `/api/stats` DB COUNT() calls | Lock-guarded so two concurrent requests can't double-execute the heavy aggregation pass — v45. |
| Webview navigation | `desktop/catchem/src-tauri/src/security.rs::classify_navigation` | Pure function with unit tests; same-origin + `tauri://` / `asset://` / `ipc://` allowed in-webview; safe http(s) routed to system browser via `open::that_detached`; `javascript:` / `file:` / `data:` / unknown schemes blocked. |
| Rate limiting | `rate_limit.py` token buckets | `DEFAULT_BUCKET` 60/min · `SEARCH_BUCKET` 30/min · `DB_IMPORT_BUCKET` 6/min cost 5 · live-read cost 2 · db/export cost 3 · `_rate_limit_probe` per-feed. |

---

## 10. Test coverage

| Surface | Framework | Count | Notes |
|---|---|---|---|
| Backend unit + integration | pytest 8.2 + pytest-asyncio | 759 tests across 86 files (`tests/`) | Markers: `guard`, `smoke`, `integration`, `ml`, `regression`. `make test-guards` always runs. v46 closed quant-module coverage gaps with 10 targeted tests. |
| Backend coverage | pytest-cov | 80%+ overall on `src/catchem` | Branch coverage on; `htmlcov` via `make coverage`. v46 wiring added. |
| Frontend UI | Vitest 2 + @testing-library/react | 388 tests across 41 files (`frontend/src/tests/`) | jsdom env; `@vitest/coverage-v8` via `npm run cov`. |
| Frontend smoke (E2E) | Playwright | 15 release-gate journeys | v33 work; route-stubbed, fake WS. |
| Type safety | TypeScript 5.6 strict | `tsc -b --noEmit` clean | `npm run typecheck`. |

**Grand total**: 1147 owned tests (759 backend + 388 frontend) + 15 Playwright smokes.

Filterwarnings in `pyproject.toml` upgrade catchem-package `DeprecationWarning` to errors so future-removal warnings (e.g. `asyncio.get_event_loop` slated for removal in Python 3.16) fail CI loudly.

### Audit cycles

| Round | Version | Scope | Findings | Fixed |
|---|---|---|---|---|
| 1 | v33 | Backend code review (broad sweep) | 20 | 11 critical/high |
| 2 | v45 | Backend re-review | 15 | 8 critical/high (record_tags FK cascade, probe race, migration semantics, stats cache lock, SSRF guard, archive race, tags cache, cursor() deprecation) |
| 3 | v48 | Frontend code review | 12 | 12 (7 critical/high in v48, 4 medium in v50, 1 a11y in v49) |
| — | — | **Cumulative** | **47** | **31+ critical/high/medium** |

---

## 11. Threat model

| Adversary surface | Concrete attack | Mitigation |
|---|---|---|
| Malicious RSS feed item | XSS in title (`<script>` in title) | Title is rendered as text, never HTML. Curated allow-list (~375 assembled feeds, no arbitrary user URLs in the default set); RSS bodies parsed through the stdlib `xml.etree` parser (no external entities). |
| Malicious article body | XSS via JS link, drive-by via meta-refresh | Webview `on_navigation` blocks `javascript:`/`file:`/`data:` and routes external http(s) to system browser via `open::that_detached`. Links rendered with `rel="noopener noreferrer"`. |
| Snapshot JSON tampering | Plant attacker keys in localStorage | `frontend/src/lib/snapshot.ts` strictly allow-lists `SNAPSHOT_KEYS`. Unknown keys are reported under `rejected[]` on import, never written. |
| Writable `.env` file | DeepSeek key exfiltration via group/world-writable file | Pydantic-settings loads on startup; recommended posture is `chmod 600`. CSP `connect-src 'self'` prevents browser-side exfiltration channels. |
| Operator-set webhook URL | SSRF — webhook posts redirected at AWS metadata, LAN, or loopback | `webhook._ssrf_guard` rejects `localhost`/`ip6-loopback` by name and any URL whose resolved IPs are `is_private` (RFC 1918), `is_loopback`, `is_link_local` (169.254/16 + fe80::/10, covering AWS metadata), `is_multicast`, or `is_unspecified` — v45. |
| Failing RSS feed (DoS) | One slow/broken publisher stalls the poller, exhausts retries, blocks the loop | Per-feed circuit breaker (`CIRCUIT_BREAKER_THRESHOLD=5` consecutive failures → 5-tier backoff 60/300/900/1800/3600s). Single success closes the breaker. Cooldown is O(1) per tick — v43. |
| DB import abuse | Repeated heavy `/api/db/import` to fill disk / DoS | Token bucket `DB_IMPORT_BUCKET` (cost 5, 6/min refill → ~1 import/min). 200 MB hard cap. SQLite magic-bytes check before atomic rename. Pre-import backup retained. |
| Timing / CSRF on POST | Cross-origin form posting a destructive action | Localhost-only bind; CSP `frame-ancestors 'none'`; no `Access-Control-Allow-Credentials` (CORS deny by default unless `cors_origins` is set). |
| Sensitive path leakage | `/Users/<name>/...` in tooltips / screenshots | `_display_path()` rewrites `$HOME` → `~`. `safe_guard_view()` strips `governance_index_path` + error strings from `/ui/guards`. |
| Bundle write attempts (release) | Sidecar tries to write into `.app` bundle | Sidecar cwd forced to `~/Library/Application Support/Catchem/`; bundle stays read-only. PyInstaller `--onedir` makes this fail loudly if the env var slips. |
| TCC prompt fatigue | macOS asks for Desktop access on every launch | `inject_info_plist.sh` writes `NSDesktopFolderUsageDescription` idempotently (only if value differs); preserves bundle cdhash so existing TCC grants survive rebuilds. |
| NewsImpact diagnostic leakage | Quarantined research diagnostics leak into production records | `guards.newsimpact_diagnostic_enabled` pinned `False` in `production_safe` mode; `redaction.redact_record_for_mode()` re-scrubs every API surface. `tests/test_*_guard*.py` runs as a non-skippable marker. |

---

## File references

| Concern | File |
|---|---|
| HTTP surface | `src/catchem/api.py` (~4420 LOC, 107 routes) |
| Pipeline orchestration | `src/catchem/supervisor.py`, `service.py`, `pipeline.py` |
| Storage | `src/catchem/storage.py`, `migrations.py` |
| Quant facade | `src/catchem/quant/engine.py` (+ 16 signal modules in `quant/`, 1 top-level `backtest.py`) |
| Reviewer registry | `src/catchem/reviewers/{base,stub,deepseek,registry,prompts}.py` |
| Awareness engine | `src/catchem/news_poller.py` (~375 assembled feeds, 6 pluggable parsers, `register_parser`/`register_feed_provider`/`assemble_feeds`, adaptive cadence, cross-source title dedup, 5-tier breaker) |
| Source packs | `src/catchem/news_sources/**` (25 auto-discovered packs; `__init__.py::_discover()`) |
| Real-time push channel | `src/catchem/ws_push.py` (WS + Wikimedia SSE, OFF-default, `WebSocketNewsChannel.stats()`) |
| Awareness blind-spot detector | `src/catchem/awareness_gaps.py` (`find_coverage_gaps`, pure) |
| Global-tone signal | `src/catchem/quant/global_tone.py` (GDELT `TimelineTone`; `summarize_tone`/`fetch_tone`/`compute_global_tone`) |
| Portfolio subsystem | `src/catchem/portfolio.py` (`enrich_holdings`, pure) + migration #3 (`portfolio` table) |
| Drive archiver | `src/catchem/archive.py` (CSV export, 17 cols, 30 s tick) |
| Webhook | `src/catchem/webhook.py` (SSRF-guarded) |
| Rate limits | `src/catchem/rate_limit.py` |
| Redaction | `src/catchem/redaction.py` |
| Runtime telemetry | `src/catchem/runtime_metrics.py` (psutil + POSIX `resource` fallback) |
| Log rotation | `src/catchem/logging.py` (`RotatingFileHandler`, 5 MB × 3) |
| Cross-window sync | `frontend/src/lib/storage-sync.ts` (`useStorageSync`) |
| i18n layer + parity gate | `frontend/src/lib/i18n.ts` (EN/TR, `useSyncExternalStore`), `frontend/src/tests/i18n.test.ts` (key-set parity) |
| Quant fail-soft pill | `frontend/src/features/quant/DegradedSignalsPill.tsx` (consumes `/api/quant/diagnostics`) |
| Tauri shell | `desktop/catchem/src-tauri/src/{lib,sidecar,security,paths,state,menu,commands}.rs` (`lib.rs::boot_log` → `~/Library/Logs/Catchem/boot.log`) |
| Boot shim | `desktop/catchem/web/` |
| Premium UI entry | `frontend/src/app/App.tsx`, `frontend/src/layout/Shell.tsx` |
| Chord registry | `frontend/src/lib/nav-shortcuts.ts` |
| Snapshot allow-list | `frontend/src/lib/snapshot.ts` |
| Build matrix | `docs/CATCHEM_BUILD_MATRIX.md` |
| Frontend deep-dive | `docs/FRONTEND_ARCHITECTURE.md` |
| Hardening notes | `docs/CATCHEM_HARDENING.md` |
