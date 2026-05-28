/**
 * Single source of truth for first-run onboarding state.
 *
 * Before this module the onboarding storage key was a string literal that
 * lived inside `components/OnboardingModal.tsx` and was re-imported by the
 * command palette, the snapshot exporter, and three test files. The
 * "seen" flag, the read/write helpers, and the imperative re-open path
 * now all live here so they can never drift apart.
 *
 * Two ways the modal opens:
 *   1. First run — `hasSeenOnboarding()` is false on mount, so the modal
 *      renders itself.
 *   2. On demand — any surface (Help page button, command palette, a
 *      future menu item) calls `requestOpenOnboarding()`, which dispatches
 *      a window event the mounted modal listens for. This re-opens the
 *      tour INSTANTLY, with no `window.location.reload()` — important both
 *      for UX (no white flash, no lost query cache) and for live-testing
 *      the re-open flow in a browser without a navigation.
 *
 * The flag is intentionally NOT cleared when we re-open on demand: a user
 * replaying the tour from Help has already "seen" it, so closing the
 * replayed tour shouldn't make it pop again on the next launch.
 */

export const ONBOARDING_STORAGE_KEY = "catchem.onboarding.completed";

/**
 * Window event the mounted OnboardingModal listens for. Dispatched by
 * `requestOpenOnboarding()` so callers don't need a ref to the modal.
 */
export const OPEN_ONBOARDING_EVENT = "catchem:open-onboarding";

/**
 * Has the user already completed or dismissed the first-run tour?
 *
 * Storage can be unavailable (private mode, disabled cookies, quota). In
 * that case we treat onboarding as *seen* so we never pester the user
 * with a modal we can't remember dismissing.
 */
export function hasSeenOnboarding(): boolean {
  try {
    return localStorage.getItem(ONBOARDING_STORAGE_KEY) === "true";
  } catch {
    return true;
  }
}

/** Persist the "seen" flag. Silent no-op if storage is unavailable. */
export function markOnboardingSeen(): void {
  try {
    localStorage.setItem(ONBOARDING_STORAGE_KEY, "true");
  } catch {
    /* ignore quota / disabled storage — flag stays in-memory only */
  }
}

/**
 * Clear the "seen" flag so the tour shows again on the next mount/reload.
 * Used by the command-palette "Restart onboarding" action, which pairs
 * this with a reload. On-demand replays from the Help page use
 * `requestOpenOnboarding()` instead and leave the flag untouched.
 */
export function resetOnboarding(): void {
  try {
    localStorage.removeItem(ONBOARDING_STORAGE_KEY);
  } catch {
    /* ignore storage errors */
  }
}

/**
 * Re-open the tour immediately without a page reload. Safe to call even
 * if the modal is already open (the modal de-dupes). No-ops outside a
 * browser (SSR / test environments without `window`).
 */
export function requestOpenOnboarding(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(OPEN_ONBOARDING_EVENT));
}
