# Catchem Hardening Branch — `feature/catchem-hardening`

Bug-hunt + security + runtime hardening pass on top of PR #3
(`feature/catchem-app`). All changes preserve the production-safe NewsImpact
guard and leave `final_best.pt`, `awareness/`, and `merged_news/` untouched.

## Commits in order

| SHA       | Subject                                                                              | Layer       |
|-----------|--------------------------------------------------------------------------------------|-------------|
| `fba20d5` | fix(catchem): bug-hunt round — token-boundary aliases, stale filter, text_excerpt    | pipeline    |
| `954ae89` | fix(catchem-security): tighten webview CSP, navigation guard, withGlobalTauri off    | desktop     |
| `21dfd7f` | fix(catchem-runtime): release sidecar writes under Application Support, not bundle   | desktop     |
| `b01d80f` | feat(catchem-ux): wire boot shim back in with 5-stage startup state machine          | desktop+ux  |

## What changed by severity

### P0 — security

1. **CSP was not enforced.** `tauri.conf.json` had `csp: null` plus
   `dangerousDisableAssetCspModification: true`, contradicting the PR #3
   description ("CSP locked to 127.0.0.1:8087"). Replaced with an explicit
   default-deny policy that only allows the local sidecar origin plus
   Tauri's ipc: scheme. `frame-ancestors 'none'`, `object-src 'none'`,
   `base-uri 'self'` close the usual SVG / PDF / `<base>` escapes.

2. **No webview navigation guard.** `is_allowed_internal_url` existed and
   had a unit test, but lib.rs never called it. Now wired via
   `WebviewWindowBuilder::on_navigation()` + a new pure-function classifier
   in `security.rs` (`classify_navigation` → `NavigationDecision` enum).
   External http/https → system browser; everything else (javascript:,
   file:, vbscript:, data:, ipc forgeries) → dropped + logged.

3. **`withGlobalTauri: true` wasted attack surface.** The React UI
   doesn't import `@tauri-apps/api` anywhere; the global was only used by
   future code that doesn't exist yet. Flipped to `false`.

### P0 — runtime

4. **Release sidecar wrote inside the .app bundle.** Original
   `lib.rs:56` used `std::env::current_dir()` when `dev_repo_root()`
   returned `None`. In a packaged .app, that resolves to `/` or the
   user's home, so the sidecar either failed to find `configs/` or, on
   unsigned dev builds, tried to write SQLite + parquet under
   `Catchem.app/Contents/MacOS/` — Gatekeeper read-only space.
   - `paths::app_data_dir()` returns `~/Library/Application Support/Catchem/`
   - `SidecarConfig.release_mode` (defaulted via `#[serde(default)]`)
     drives env-var injection: `FUSION_PATHS__FUSION_OUTPUT_DIR` and
     `FUSION_PATHS__AWARENESS_DATA_DIR` both land under AppSupport
     when the flag is set.
   - cwd in release falls back to `app_data_dir()`, not `current_dir()`.

### P0 — UX

5. **Boot screen was dead code.** `lib.rs:109` built the window with
   `WebviewUrl::External(http://127.0.0.1:8087/)`, bypassing the
   `desktop/catchem/web/` boot shim entirely. If the sidecar was slow,
   the user got a Chromium "this site can't be reached" page — no
   Retry, no log path, no hint at what failed.
   - Switched to `WebviewUrl::App("index.html".into())`.
   - Rebuilt the shim with a 5-stage state machine (checking → spawning
     → waiting → bundle → ready) matching the prompt's spec.
   - Timeout panel includes Retry + Show log path buttons.
   - All dynamic content goes through safe DOM helpers — no `innerHTML`.

### P1 — pipeline correctness

6. **`unaffordable` was being classified as Ford.** Plain
   `alias.lower() in joined.lower()` matched company aliases as
   substrings, so "interface" became ICE, "stockholm" became STOCK, etc.
   - Both `entity_linker.py` and `symbol_mapper.py` now use a precompiled
     `(?<![A-Za-z0-9]){alias}(?![A-Za-z0-9])` regex per alias.
   - New `_TICKER_DENYLIST` (CEO, CFO, IPO, ETF, FOMC, ECB, …) so the
     paren-ticker detector doesn't turn abbreviations into trade
     candidates.
   - Crypto shorthand resolves: `BTC`, `ETH`, `SOL`, … now map to the
     same Yahoo symbol as their long names.
   - Fuzzy fallback now skips alias keys shorter than 5 chars to
     prevent partial_ratio false-positives.

7. **Stale items spam the Live Feed.** A few feeds occasionally
   republish week-old links with fresh pubDates. `news_poller.NewsPoller`
   gained a `max_item_age_seconds` filter (default 14 days);
   `last_stale_skipped` is now part of the diagnostics surface.

8. **Per-feed health is opaque.** Replaced the bare `list[ParsedItem]`
   per-fetch return with a typed `FeedFetchResult` (status_code,
   elapsed_ms, error, item_count). `NewsPoller.feed_health_snapshot()`
   exposes per-source consecutive_errors, last_success_at,
   last_failure_at. The Ops surface can now answer "which feed is
   silently 403-ing us?" instead of "the poller looks fine".

9. **Google News titles included publisher suffix and wrong domain.**
   `_strip_source_suffix` removes the trailing " - Source" that
   Google News injects; `<source url="…">` is preferred over
   `news.google.com` when resolving the domain field.

### P1 — storage + evidence

10. **SQLite handle leak.** `with self._connect() as conn` only committed
    the transaction; it didn't close the connection. Refactored to a
    `_connection()` context manager that closes the handle in a
    `finally`.
11. **`text_excerpt` column added** with `ALTER TABLE` migration so
    existing SQLite files keep working. Surfaces through the typed
    `FinancialImpactDetail` contract.
12. **Boilerplate sentences leaked entity hits.** A new `clean_boilerplate_text`
    (cookies, privacy policy, subscribe, sponsored, …) runs before
    entity extraction and evidence sentence selection. Stops the
    linker from "discovering" central banks inside footer CTAs.

### P3 — lint

13. `commands.rs` removed unused `SidecarConfig` / `SidecarState`
    imports surfaced as a cargo warning on every build.

## Test counts

| Suite                                 | Before     | After       | Delta            |
|---------------------------------------|------------|-------------|------------------|
| `pytest tests -q`                     | 192 passed | 222 passed  | +30 (with 1 skip — final_best.pt) |
| `cargo test --lib --quiet`            | 3 passed   | 8 passed    | +5               |
| `(cd frontend && npm test)`           | 13 passed  | 31 passed   | +18              |

(Frontend +18 came from PR #3's prior session and ships unchanged in
this branch; pytest +30 and cargo +5 are this branch's own additions.)

## What this branch does NOT change

- `final_best.pt` — untouched.
- `awareness/` — untouched (separate repo).
- `merged_news/` — untouched (separate repo).
- NewsImpact governance index — read-only, three-check gate intact.
- `production_safe` mode — still the only mode the shell ever spawns.
- The premium UI (PR #1, PR #2 work) — unchanged; this branch only
  edits the boot shim and the desktop shell around it.
- Notarization workflow — script-ready, but needs Apple credentials
  the analyst's box doesn't have, so this branch ships unsigned.
