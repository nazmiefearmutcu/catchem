import { useEffect, useRef } from "react";

interface OverlayEntry {
  onClose: () => void;
  lockBody: boolean;
}

interface OverlayOptions {
  lockBody?: boolean;
}

interface UseOverlaySurfaceArgs {
  id: string;
  open: boolean;
  onClose: () => void;
  lockBody?: boolean;
}

const registry = new Map<string, OverlayEntry>();
let openStack: string[] = [];
const listeners = new Set<() => void>();
let escListenerBound = false;

let bodyLocked = false;
let bodyOverflowBackup = "";
let bodyPaddingRightBackup = "";
const MAX_OVERLAY_CLEAR = 128;
const MAX_OVERLAY_STALLS = 2;

function syncBodyLock(): void {
  if (typeof document === "undefined") return;
  const shouldLock = openStack.some((id) => {
    const entry = registry.get(id);
    return Boolean(entry?.lockBody);
  });
  if (shouldLock && !bodyLocked) {
    bodyLocked = true;
    bodyOverflowBackup = document.body.style.overflow;
    bodyPaddingRightBackup = document.body.style.paddingRight;
    const scrollbarWidth = window.innerWidth - document.documentElement.clientWidth;
    if (scrollbarWidth > 0) document.body.style.paddingRight = `${scrollbarWidth}px`;
    document.body.style.overflow = "hidden";
    return;
  }
  if (!shouldLock && bodyLocked) {
    bodyLocked = false;
    document.body.style.overflow = bodyOverflowBackup;
    document.body.style.paddingRight = bodyPaddingRightBackup;
  }
}

function notify(): void {
  for (const fn of listeners) fn();
  syncBodyLock();
}

function emitMaybeLockChange(): void {
  // Keep listener behavior stable even if multiple callbacks depend on
  // `isOverlayOpen` / `isOverlayTop` in future.
  notify();
}

function forceDetachTopOverlay(topId: string): void {
  const before = openStack.length;
  openStack = openStack.filter((id) => id !== topId);
  if (openStack.length !== before) {
    emitMaybeLockChange();
  }
}

function forceDetachAllOverlays(): number {
  const before = openStack.length;
  if (before === 0) return 0;
  openStack = [];
  emitMaybeLockChange();
  return before;
}

function register(id: string, opts: OverlayOptions, onClose: () => void): () => void {
  const entry: OverlayEntry = {
    onClose,
    lockBody: opts.lockBody ?? true,
  };
  const existed = registry.has(id);
  registry.set(id, entry);

  // If a live overlay flips lock settings while already visible, re-run
  // the lock calculation immediately.
  if (existed && isOverlayOpen(id)) syncBodyLock();

  return () => {
    if (isOverlayOpen(id)) setOverlayOpen(id, false);
    registry.delete(id);
  };
}

function isOverlayOpen(id: string): boolean {
  return openStack.includes(id);
}

function isOverlayTop(id: string): boolean {
  return openStack.at(-1) === id;
}

function setOverlayOpen(id: string, open: boolean): void {
  const currentlyOpen = isOverlayOpen(id);
  if (!registry.has(id)) return;
  if (open === currentlyOpen) return;
  if (open) {
    if (!currentlyOpen) {
      openStack = [...openStack, id];
    }
  } else {
    openStack = openStack.filter((v) => v !== id);
  }
  emitMaybeLockChange();
}

export function closeOverlay(id: string): void {
  if (!isOverlayOpen(id)) return;
  const entry = registry.get(id);
  // Remove from stack first so nested open/close calls and focus handling
  // stay deterministic even if the callback triggers a slower re-render.
  setOverlayOpen(id, false);
  if (!entry) return;
  try {
    entry.onClose();
  } catch {
    /* ignore */
  }
}

export function closeTopOverlay(): boolean {
  const top = openStack.at(-1);
  if (!top) return false;
  closeOverlay(top);
  return true;
}

export function closeAllOverlays(): number {
  let closed = 0;
  const stalledByTop = new Map<string, number>();
  for (let i = 0; i < MAX_OVERLAY_CLEAR; i += 1) {
    const top = openStack.at(-1);
    if (!top) break;

    const beforeLen = openStack.length;
    closeTopOverlay();
    if (openStack.length < beforeLen) {
      closed += 1;
      stalledByTop.clear();
      continue;
    }

    const stalled = (stalledByTop.get(top) ?? 0) + 1;
    stalledByTop.set(top, stalled);
    // If callback logic re-opens the same overlay, it can get stuck in
    // a close loop. Run a bounded retry path, then force-drop it so the
    // coordinator always makes progress and cannot deadlock.
    if (stalled < MAX_OVERLAY_STALLS) {
      continue;
    }
    const beforeDetachLen = openStack.length;
    forceDetachTopOverlay(top);
    if (openStack.length < beforeDetachLen) {
      closed += 1;
    }
    stalledByTop.delete(top);
  }
  if (openStack.length > 0) {
    closed += forceDetachAllOverlays();
  }
  return closed;
}

function ensureEscListener(): void {
  if (escListenerBound) return;
  if (typeof document === "undefined") return;
  escListenerBound = true;
  const onKey = (event: KeyboardEvent) => {
    if (event.key !== "Escape") return;
    if (!closeTopOverlay()) return;
    event.preventDefault();
  };
  document.addEventListener("keydown", onKey);
}

/**
 * Register a surface with the global overlay stack and synchronize its
 * open/close lifecycle to the coordinator.
 *
 * This helper avoids each overlay owning its own global Escape handler and
 * body scroll lock logic. The overlay still owns its internal focus trap,
 * close button, route guards, and local persistence side effects.
 */
export function useOverlaySurface({
  id,
  open,
  onClose,
  lockBody = true,
}: UseOverlaySurfaceArgs): void {
  ensureEscListener();
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    return register(id, { lockBody }, () => onCloseRef.current());
  }, [id, lockBody]);
  useEffect(() => {
    setOverlayOpen(id, open);
  }, [id, open]);
}

export function useOverlayStateForTest(id: string): {
  open: boolean;
  top: boolean;
} {
  return { open: isOverlayOpen(id), top: isOverlayTop(id) };
}

export function hasOpenOverlays(): boolean {
  return openStack.length > 0;
}

export function subscribeOverlayState(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function __resetOverlayStateForTests(): void {
  listeners.clear();
  openStack = [];
  registry.clear();
  if (typeof document === "undefined") return;
  if (bodyLocked) {
    document.body.style.overflow = bodyOverflowBackup;
    document.body.style.paddingRight = bodyPaddingRightBackup;
  }
  bodyLocked = false;
  bodyOverflowBackup = "";
  bodyPaddingRightBackup = "";
  syncBodyLock();
}
