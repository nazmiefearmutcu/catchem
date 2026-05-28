import "@testing-library/jest-dom/vitest";

// jsdom does not implement Element.prototype.scrollIntoView. Components
// that call it for UX polish (e.g. CommandPalette keeping the selected
// row in view) throw "scrollIntoView is not a function" inside a render
// effect, which React surfaces as an uncaught error and fails the test.
// The behavior is meaningless in a headless DOM anyway, so stub it as a
// no-op globally. Real browsers provide the genuine implementation.
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {
    /* no-op in jsdom */
  };
}

// ── jsdom ↔ undici AbortSignal realm bridge ──────────────────────────────
// jsdom installs its OWN AbortController/AbortSignal on the global object.
// Node's fetch/Request (undici) validate `signal` against node's NATIVE
// AbortSignal class, which is a *different* class than jsdom's. So a signal
// produced by `new AbortController()` (the global = jsdom's) passes
// `instanceof AbortSignal` yet is rejected by `new Request(url,{signal})`
// with: "Expected signal to be an instance of AbortSignal".
//
// This silently breaks react-router: @remix-run/router builds a Request
// per navigation (createClientSideRequest) with an AbortController.signal,
// so EVERY programmatic navigation throws and the router state never
// updates — making all route-change / nav tests fail with the location
// stuck at "/". It also breaks any unmocked fetch in a component test.
//
// Fix at the test-infra layer: wrap Request + fetch so a foreign signal
// the native implementation would reject is dropped instead of throwing.
// Aborting is a no-op in these headless tests, so losing it is harmless;
// real browsers share one realm and never hit this path.
{
  const NativeRequest = globalThis.Request;
  const nativeFetch = globalThis.fetch?.bind(globalThis);

  // True when the native Request accepts this signal (same realm as undici).
  const signalIsNativelyAccepted = (signal: unknown): boolean => {
    try {
      // eslint-disable-next-line no-new
      new NativeRequest("http://realm.probe.invalid/", {
        signal: signal as AbortSignal,
      });
      return true;
    } catch {
      return false;
    }
  };

  const stripForeignSignal = <T extends { signal?: unknown }>(
    init: T | undefined,
  ): T | undefined => {
    if (init && init.signal != null && !signalIsNativelyAccepted(init.signal)) {
      const { signal: _drop, ...rest } = init;
      return rest as T;
    }
    return init;
  };

  if (typeof NativeRequest === "function") {
    class RealmSafeRequest extends NativeRequest {
      constructor(input: RequestInfo | URL, init?: RequestInit) {
        super(input, stripForeignSignal(init));
      }
    }
    globalThis.Request = RealmSafeRequest as unknown as typeof Request;
  }

  if (typeof nativeFetch === "function") {
    globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
      nativeFetch(input, stripForeignSignal(init))) as typeof fetch;
  }
}
