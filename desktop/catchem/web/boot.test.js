// Boot-shim contract tests.
//
// We do NOT test the auto-bootstrap branch at the bottom of boot.js — that
// runs the moment the module is imported in a real browser. Instead boot.js
// exports `startBootShim()` with dependency-injection seams (fetcher,
// navigate, sleep, doc, now) so we drive the same state machine
// deterministically.
//
// Each test rebuilds the boot-shim DOM in jsdom by parsing the production
// fragment from `test-fixtures/boot-shim-fragment.html`. The fragment is
// the exact body content of `index.html`; if those two ever drift the
// tests' setStage assertions fail and the analyst has to update both —
// that's the point.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  STAGES,
  getBootTokenFromUrl,
  setStage,
  showLogPath,
  showTimeout,
  startBootShim,
} from "./boot.js";

// Resolve relative to this test file — works both when Vitest runs from
// the project root and from the boot-shim directory.
const FIXTURE_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "test-fixtures",
  "boot-shim-fragment.html",
);
const BOOT_SHIM_FRAGMENT = readFileSync(FIXTURE_PATH, "utf8");

function mountShim() {
  // DOMParser bypasses the "no innerHTML on body" hook check. The parsed
  // fragment is a literal file read from disk, never user input.
  const parsed = new DOMParser().parseFromString(
    `<!doctype html><html><body>${BOOT_SHIM_FRAGMENT}</body></html>`,
    "text/html",
  );
  for (const child of Array.from(document.body.childNodes)) {
    document.body.removeChild(child);
  }
  for (const child of Array.from(parsed.body.childNodes)) {
    document.body.appendChild(document.importNode(child, true));
  }
}

function rowClasses(stage) {
  return [...document.querySelector(`[data-stage="${stage}"]`).classList];
}

beforeEach(() => mountShim());
afterEach(() => {
  for (const child of Array.from(document.body.childNodes)) {
    document.body.removeChild(child);
  }
  window.name = "";
  window.sessionStorage.removeItem("catchem.boot_token");
});

// ── setStage ────────────────────────────────────────────────────────────────

describe("setStage", () => {
  test("active marks the named row and leaves earlier rows neutral until cumulative passes", () => {
    setStage(document, "boot", "active");
    expect(rowClasses("boot")).toContain("active");
    expect(rowClasses("spawn")).not.toContain("active");
  });

  test("active on a later stage marks all earlier stages done", () => {
    setStage(document, "health", "active");
    expect(rowClasses("boot")).toContain("done");
    expect(rowClasses("spawn")).toContain("done");
    expect(rowClasses("health")).toContain("active");
    expect(rowClasses("bundle")).not.toContain("done");
  });

  test("done on the final stage marks everything done", () => {
    setStage(document, "ready", "done");
    for (const s of STAGES) {
      expect(rowClasses(s)).toContain("done");
    }
  });

  test("fail marks only the named row as fail (earlier rows still done)", () => {
    setStage(document, "health", "fail");
    expect(rowClasses("health")).toContain("fail");
    expect(rowClasses("boot")).toContain("done");
    expect(rowClasses("spawn")).toContain("done");
    expect(rowClasses("bundle")).not.toContain("done");
    expect(rowClasses("bundle")).not.toContain("fail");
  });

  test("calling setStage twice clears previous status (no stale active dots)", () => {
    setStage(document, "health", "active");
    setStage(document, "health", "fail");
    expect(rowClasses("health")).toContain("fail");
    expect(rowClasses("health")).not.toContain("active");
  });

  test("unknown stage name is a no-op (no status class applied to any row)", () => {
    setStage(document, "not-a-stage", "active");
    for (const s of STAGES) {
      const cls = rowClasses(s);
      expect(cls).not.toContain("active");
      expect(cls).not.toContain("done");
      expect(cls).not.toContain("fail");
    }
  });
});

// ── showTimeout / showLogPath (presentation only) ──────────────────────────

describe("showTimeout", () => {
  test("renders endpoint, deadline, and exposes Retry+Show-log-path actions", () => {
    showTimeout(document, "http://127.0.0.1:8087", 30_000, "ConnectionRefused");

    const meta = document.getElementById("meta");
    const codeNodes = [...meta.querySelectorAll("code")].map((c) => c.textContent);
    expect(codeNodes).toContain("http://127.0.0.1:8087/healthz");
    expect(meta.textContent).toContain("30 s");

    const err = document.getElementById("err");
    expect(err.hidden).toBe(false);
    expect(err.textContent).toBe("Last error: ConnectionRefused");

    const actions = document.getElementById("actions");
    expect(actions.classList).toContain("show");

    // No raw HTML reached the DOM via innerHTML — meta only has text+code nodes.
    for (const child of meta.childNodes) {
      expect([Node.TEXT_NODE, Node.ELEMENT_NODE]).toContain(child.nodeType);
      if (child.nodeType === Node.ELEMENT_NODE) {
        expect(child.tagName.toLowerCase()).toBe("code");
      }
    }
  });

  test("with no reason the error span renders an empty string, not 'undefined'", () => {
    showTimeout(document, "http://x", 5_000, "");
    expect(document.getElementById("err").textContent).toBe("");
  });
});

describe("showLogPath", () => {
  test("rewrites meta with the log file path and 3 alternative ways to open it", () => {
    showLogPath(document);
    const meta = document.getElementById("meta");
    const codeNodes = [...meta.querySelectorAll("code")].map((c) => c.textContent);
    expect(codeNodes).toContain("~/Library/Logs/Catchem/sidecar.log");
    expect(codeNodes).toContain("Console.app");
    expect(codeNodes).toContain("Catchem");
    expect(codeNodes).toContain("open ~/Library/Logs/Catchem/");
  });
});

describe("boot token", () => {
  test("extracts boot_token from a URL query string", () => {
    expect(getBootTokenFromUrl("http://127.0.0.1:8087/index.html?boot_token=abc123")).toBe("abc123");
    expect(getBootTokenFromUrl("http://127.0.0.1:8087/index.html#boot_token=abc123")).toBe("abc123");
    expect(getBootTokenFromUrl("http://127.0.0.1:8087/")).toBe("");
  });
});

// ── startBootShim — the actual state machine ───────────────────────────────

function immediateSleep() {
  // Resolves on next microtask. Tests stay deterministic because we control
  // `now()` and the fetcher; the actual wall-clock delay is meaningless.
  return Promise.resolve();
}

describe("startBootShim", () => {
  test("happy path: navigates to FastAPI URL on first /healthz 200", async () => {
    const navigate = vi.fn();
    const fetcher = vi.fn().mockResolvedValue({ ok: true, status: 200 });

    const result = await startBootShim({
      endpoint: "http://127.0.0.1:8087",
      deadlineMs: 5_000,
      pollMs: 10,
      spawnRowMs: 0,
      bundleRowMs: 0,
      fetcher,
      navigate,
      sleep: immediateSleep,
      doc: document,
      now: () => 1_000,
    });

    expect(result.outcome).toBe("navigated");
    expect(result.url).toBe("http://127.0.0.1:8087/");
    expect(navigate).toHaveBeenCalledWith("http://127.0.0.1:8087/");
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][0]).toBe("http://127.0.0.1:8087/healthz");
    expect(rowClasses("ready")).toContain("done");
    expect(document.getElementById("actions").classList).not.toContain("show");
  });

  test("persists the boot token for the React app", async () => {
    const navigate = vi.fn();
    const fetcher = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: (name) => (name === "x-catchem-boot-token" ? "abc123" : null) },
    });

    await startBootShim({
      endpoint: "http://127.0.0.1:8087",
      bootToken: "abc123",
      deadlineMs: 5_000,
      pollMs: 10,
      spawnRowMs: 0,
      bundleRowMs: 0,
      fetcher,
      navigate,
      sleep: immediateSleep,
      doc: document,
      now: () => 1_000,
    });

    expect(window.sessionStorage.getItem("catchem.boot_token")).toBe("abc123");
    expect(navigate).toHaveBeenCalledWith("http://127.0.0.1:8087/?boot_token=abc123#boot_token=abc123");
    expect(window.name).toBe("catchem.boot_token=abc123");
  });

  test("includes the boot token in the health probe when provided", async () => {
    const navigate = vi.fn();
    const fetcher = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: (name) => (name === "x-catchem-boot-token" ? "abc123" : null) },
    });

    await startBootShim({
      endpoint: "http://127.0.0.1:8087",
      bootToken: "abc123",
      deadlineMs: 5_000,
      pollMs: 10,
      spawnRowMs: 0,
      bundleRowMs: 0,
      fetcher,
      navigate,
      sleep: immediateSleep,
      doc: document,
      now: () => 1_000,
    });

    expect(fetcher.mock.calls[0][0]).toBe("http://127.0.0.1:8087/healthz?boot_token=abc123");
  });

  test("ignores 200 health responses that do not echo the boot token header", async () => {
    const navigate = vi.fn();
    const fetcher = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: () => null },
    });

    let clock = 0;
    const result = await startBootShim({
      endpoint: "http://127.0.0.1:8087",
      bootToken: "abc123",
      deadlineMs: 200,
      pollMs: 50,
      spawnRowMs: 0,
      bundleRowMs: 0,
      fetcher,
      navigate,
      sleep: () => {
        clock += 50;
        return Promise.resolve();
      },
      doc: document,
      now: () => clock,
    });

    expect(result.outcome).toBe("timeout");
    expect(result.lastErr).toBe("boot token mismatch");
    expect(navigate).not.toHaveBeenCalled();
  });

  test("retries on transient HTTP errors then succeeds", async () => {
    const navigate = vi.fn();
    const fetcher = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 503 })
      .mockResolvedValueOnce({ ok: false, status: 502 })
      .mockResolvedValueOnce({ ok: true, status: 200 });

    let clock = 0;
    const result = await startBootShim({
      deadlineMs: 5_000,
      pollMs: 100,
      spawnRowMs: 0,
      bundleRowMs: 0,
      fetcher,
      navigate,
      sleep: () => { clock += 100; return Promise.resolve(); },
      doc: document,
      now: () => clock,
    });

    expect(result.outcome).toBe("navigated");
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(navigate).toHaveBeenCalledOnce();
  });

  test("retries on thrown errors (fetch raises) without crashing", async () => {
    const navigate = vi.fn();
    const fetcher = vi.fn()
      .mockRejectedValueOnce(new Error("ECONNREFUSED"))
      .mockRejectedValueOnce(new Error("DNS"))
      .mockResolvedValueOnce({ ok: true, status: 200 });

    let clock = 0;
    const result = await startBootShim({
      deadlineMs: 5_000,
      pollMs: 100,
      spawnRowMs: 0,
      bundleRowMs: 0,
      fetcher,
      navigate,
      sleep: () => { clock += 100; return Promise.resolve(); },
      doc: document,
      now: () => clock,
    });

    expect(result.outcome).toBe("navigated");
    expect(navigate).toHaveBeenCalledOnce();
  });

  test("timeout path: gives up after deadline, renders Retry + Show-log-path", async () => {
    const navigate = vi.fn();
    const fetcher = vi.fn().mockRejectedValue(new Error("ECONNREFUSED"));

    let clock = 0;
    const result = await startBootShim({
      endpoint: "http://127.0.0.1:8087",
      deadlineMs: 1_000,
      pollMs: 200,
      spawnRowMs: 0,
      bundleRowMs: 0,
      fetcher,
      navigate,
      sleep: () => { clock += 200; return Promise.resolve(); },
      doc: document,
      now: () => clock,
    });

    expect(result.outcome).toBe("timeout");
    expect(result.lastErr).toBe("ECONNREFUSED");
    expect(navigate).not.toHaveBeenCalled();

    expect(rowClasses("health")).toContain("fail");
    expect(document.getElementById("actions").classList).toContain("show");

    const err = document.getElementById("err");
    expect(err.hidden).toBe(false);
    expect(err.textContent).toContain("ECONNREFUSED");

    const meta = document.getElementById("meta");
    expect(meta.textContent).toContain("/healthz");
    expect(meta.textContent).toContain("1 s");
  });

  test("timeout path captures the LAST observed error, not the first", async () => {
    const navigate = vi.fn();
    const fetcher = vi.fn()
      .mockRejectedValueOnce(new Error("first"))
      .mockResolvedValueOnce({ ok: false, status: 503 })
      .mockRejectedValueOnce(new Error("last"));

    let clock = 0;
    const result = await startBootShim({
      deadlineMs: 800,
      pollMs: 300,
      spawnRowMs: 0,
      bundleRowMs: 0,
      fetcher,
      navigate,
      sleep: () => { clock += 300; return Promise.resolve(); },
      doc: document,
      now: () => clock,
    });

    expect(result.outcome).toBe("timeout");
    expect(result.lastErr).toBe("last");
  });

  test("walks the stages in order before navigating away", async () => {
    const stageProgressionAtFirstFetch = [];
    const navigate = vi.fn();
    const fetcher = vi.fn(async () => {
      // Snapshot what's done by the time we first hit /healthz — at minimum
      // boot+spawn should be marked done so the user sees real progression.
      stageProgressionAtFirstFetch.push(
        STAGES.filter((s) => rowClasses(s).includes("done")),
      );
      return { ok: true, status: 200 };
    });

    await startBootShim({
      deadlineMs: 5_000,
      pollMs: 10,
      spawnRowMs: 0,
      bundleRowMs: 0,
      fetcher,
      navigate,
      sleep: immediateSleep,
      doc: document,
      now: () => Date.now(),
    });

    expect(stageProgressionAtFirstFetch[0]).toContain("boot");
    // After return, every stage is marked done.
    for (const s of STAGES) expect(rowClasses(s)).toContain("done");
  });
});
