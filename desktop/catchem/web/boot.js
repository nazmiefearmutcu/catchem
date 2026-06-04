// Boot shim runtime, extracted from index.html so it's importable + testable.
//
// Public API:
//   * startBootShim(options) — drives the 5-stage state machine, returns a
//     Promise that resolves when navigation away succeeds or when the
//     timeout panel is rendered.
//   * setStage / showTimeout / showLogPath — pure DOM mutators, exported
//     so a future Tauri event listener can drive them from Rust if
//     navigation policy changes.
//
// Dependency injection points (`options`):
//   * fetcher  — defaults to `globalThis.fetch`. Tests override with a
//                stub. Must return `{ ok, status }` like a Response.
//   * navigate — defaults to `(url) => window.location.replace(url)`.
//                Tests override to a spy.
//   * sleep    — defaults to setTimeout-based. Tests override to
//                resolve immediately so polling loops finish fast.
//   * doc      — defaults to `document`. Tests pass a jsdom-built one.
//
// Why dependency injection here at all? The boot shim is the single
// trust-boundary between the user's window and the FastAPI sidecar.
// Every transition is observable, but the SOURCE of those observations
// (a real fetch vs. a mock) shouldn't matter to the state machine. Pure
// inputs → pure outputs makes the timeout/success/retry paths all
// testable from Vitest in ~10 ms each.

export const STAGES = Object.freeze(["boot", "spawn", "health", "bundle", "ready"]);

const BOOT_TOKEN_STORAGE_KEY = "catchem.boot_token";
const BOOT_TOKEN_WINDOW_NAME_PREFIX = "catchem.boot_token=";
const BOOT_TOKEN_HASH_PREFIX = "#boot_token=";

export const DEFAULTS = Object.freeze({
  endpoint: "http://127.0.0.1:8087",
  deadlineMs: 30_000,
  pollMs: 350,
  spawnRowMs: 250,
  bundleRowMs: 120,
});

/**
 * Apply one stage's status to the DOM. Pure DOM mutation — no side effects
 * outside the document. Idempotent: calling with the same args twice is a
 * no-op.
 *
 * @param {Document} doc
 * @param {string} name  — one of STAGES
 * @param {"active"|"done"|"fail"} status
 */
export function setStage(doc, name, status) {
  for (const s of STAGES) {
    const row = doc.querySelector(`[data-stage="${s}"]`);
    if (!row) continue;
    row.classList.remove("active", "done", "fail");
  }
  const idx = STAGES.indexOf(name);
  if (idx < 0) return;
  for (let i = 0; i < idx; i++) {
    const el = doc.querySelector(`[data-stage="${STAGES[i]}"]`);
    if (el) el.classList.add("done");
  }
  const target = doc.querySelector(`[data-stage="${name}"]`);
  if (!target) return;
  if (status === "fail") {
    target.classList.add("fail");
  } else if (status === "done") {
    target.classList.add("done");
    for (let i = idx + 1; i < STAGES.length; i++) {
      const el = doc.querySelector(`[data-stage="${STAGES[i]}"]`);
      if (el) el.classList.add("done");
    }
  } else {
    target.classList.add("active");
  }
}

// Safe DOM helpers — no innerHTML, no untrusted HTML strings ever reach the
// DOM tree. The XSS pre-commit hook enforces this; the Catchem code review
// guidelines call this out as a frozen contract.
export function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}
export function appendText(node, t) {
  node.appendChild(node.ownerDocument.createTextNode(t));
}
export function appendCode(node, t) {
  const c = node.ownerDocument.createElement("code");
  c.textContent = t;
  node.appendChild(c);
}

/**
 * Extract the boot token from a URL's query string. The Tauri shell
 * appends `?boot_token=...` to the boot page URL so the shim only trusts
 * the sidecar process that launched in the same session.
 */
export function getBootTokenFromUrl(href) {
  try {
    const url = new URL(href);
    const queryToken = url.searchParams.get("boot_token") || "";
    if (queryToken) return queryToken;
    const hash = url.hash || "";
    if (hash.startsWith(BOOT_TOKEN_HASH_PREFIX)) {
      return hash.slice(BOOT_TOKEN_HASH_PREFIX.length) || "";
    }
    return "";
  } catch {
    return "";
  }
}

export function persistBootToken(token) {
  if (!token || typeof sessionStorage === "undefined") return;
  try {
    sessionStorage.setItem(BOOT_TOKEN_STORAGE_KEY, token);
  } catch {
    // Best-effort only. The boot shim still works because the token is
    // already in hand for this navigation.
  }
  try {
    if (typeof window !== "undefined") {
      window.name = `${BOOT_TOKEN_WINDOW_NAME_PREFIX}${token}`;
    }
  } catch {
    // window.name is a best-effort fallback for the cross-origin hop.
  }
}

/**
 * Render the timeout panel: fail the `health` row, show retry + show-log-path
 * buttons, and write a friendly meta line that names the endpoint we gave up
 * on. The `reason` (if provided) goes into a hidden-by-default error span
 * so analysts can read it but it doesn't fight the layout.
 */
export function showTimeout(doc, endpoint, deadlineMs, reason) {
  setStage(doc, "health", "fail");
  const meta = doc.getElementById("meta");
  if (meta) {
    clearChildren(meta);
    appendText(meta, "Sidecar didn't answer at ");
    appendCode(meta, endpoint + "/healthz");
    appendText(meta, ` within ${Math.round(deadlineMs / 1000)} s.`);
  }
  const err = doc.getElementById("err");
  if (err) {
    err.hidden = false;
    err.textContent = reason ? `Last error: ${reason}` : "";
  }
  const actions = doc.getElementById("actions");
  if (actions) actions.classList.add("show");
}

/**
 * Replace the meta line with the absolute log path + a one-line guide on
 * how to view it (Console.app, Terminal, Finder). Triggered by the
 * "Show log path" button in the timeout panel.
 */
export function showLogPath(doc) {
  const meta = doc.getElementById("meta");
  if (!meta) return;
  clearChildren(meta);
  appendText(meta, "Logs live at ");
  appendCode(meta, "~/Library/Logs/Catchem/sidecar.log");
  appendText(meta, ". Open ");
  appendCode(meta, "Console.app");
  appendText(meta, " and filter for ");
  appendCode(meta, "Catchem");
  appendText(meta, ", or run ");
  appendCode(meta, "open ~/Library/Logs/Catchem/");
  appendText(meta, " in Terminal.");
}

/**
 * Drive the 5-stage poll loop. Returns a Promise that:
 *  • resolves with `{outcome: "navigated", url}` when /healthz answers and
 *    we've called `navigate(<sidecar>/)`,
 *  • resolves with `{outcome: "timeout", lastErr}` when the deadline
 *    expires and the timeout panel is rendered,
 *  • rejects if a non-recoverable error occurs (e.g. missing #meta in the
 *    DOM, which means the HTML was rendered incorrectly).
 *
 * The promise resolution is exposed so tests can assert the final outcome
 * without scraping the DOM.
 */
export async function startBootShim(options = {}) {
  const {
    endpoint = DEFAULTS.endpoint,
    deadlineMs = DEFAULTS.deadlineMs,
    pollMs = DEFAULTS.pollMs,
    spawnRowMs = DEFAULTS.spawnRowMs,
    bundleRowMs = DEFAULTS.bundleRowMs,
    bootToken = null,
    fetcher = globalThis.fetch.bind(globalThis),
    navigate = (url) => globalThis.window.location.replace(url),
    sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
    doc = globalThis.document,
    now = () => Date.now(),
  } = options;

  const resolvedBootToken =
    bootToken ?? getBootTokenFromUrl(
      doc?.defaultView?.location?.href ??
      globalThis.window?.location?.href ??
      globalThis.location?.href ??
      "",
    );
  persistBootToken(resolvedBootToken);
  const healthzUrl = resolvedBootToken
    ? `${endpoint}/healthz?boot_token=${encodeURIComponent(resolvedBootToken)}`
    : `${endpoint}/healthz`;

  setStage(doc, "boot", "done");
  setStage(doc, "spawn", "active");
  // The Rust shell spawns the sidecar synchronously in setup() before
  // the window opens, so by the time we render the spawn phase is
  // effectively over. Show the spawn row for a beat so the analyst sees
  // the state machine progressing, then move on.
  await sleep(spawnRowMs);
  setStage(doc, "health", "active");

  const deadline = now() + deadlineMs;
  let lastErr = "";
  while (now() < deadline) {
    try {
      const r = await fetcher(healthzUrl, { cache: "no-store" });
      const bootHeader = r?.headers?.get?.("x-catchem-boot-token") || "";
      const bootHeaderOk =
        !resolvedBootToken || bootHeader === resolvedBootToken;
      if (r && r.ok && bootHeaderOk) {
        setStage(doc, "bundle", "active");
        await sleep(bundleRowMs);
        setStage(doc, "ready", "done");
        const url = resolvedBootToken
          ? `${endpoint}/?boot_token=${encodeURIComponent(resolvedBootToken)}#boot_token=${encodeURIComponent(resolvedBootToken)}`
          : `${endpoint}/`;
        navigate(url);
        return { outcome: "navigated", url };
      }
      if (r && r.ok && resolvedBootToken && !bootHeaderOk) {
        lastErr = "boot token mismatch";
      } else {
        lastErr = "HTTP " + (r ? r.status : "??");
      }
    } catch (e) {
      lastErr = (e && e.message) || String(e);
    }
    await sleep(pollMs);
  }
  showTimeout(doc, endpoint, deadlineMs, lastErr);
  return { outcome: "timeout", lastErr };
}

// When loaded from index.html the script runs immediately. Tests import
// startBootShim and call it explicitly; they never trigger this path.
// `import.meta.env.MODE` is undefined under plain Node but defined under
// Vite/Vitest — we use the presence of a `document` + `window` and the
// `data-stage="boot"` row as the signal that we're in the real browser.
if (
  typeof window !== "undefined" &&
  typeof document !== "undefined" &&
  document.querySelector?.('[data-stage="boot"]') !== null
) {
  const retry = document.getElementById("retry");
  const logsBtn = document.getElementById("logs");
  if (retry) retry.addEventListener("click", () => window.location.reload());
  if (logsBtn) logsBtn.addEventListener("click", () => showLogPath(document));
  // Fire-and-forget — any rejection is reported through the DOM via
  // showTimeout(); this script doesn't need to surface it to a parent.
  startBootShim();
}
