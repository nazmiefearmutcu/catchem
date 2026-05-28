import { act, render } from "@testing-library/react";
import { flushSync } from "react-dom";
import { useRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  closeAllOverlays,
  hasOpenOverlays,
  __resetOverlayStateForTests,
  useOverlaySurface,
} from "@/context/overlayCoordinator";

function LayeredOverlayHarness({ onClose }: { onClose: (id: string) => void }) {
  const [aOpen, setAOpen] = useState(true);
  const [bOpen, setBOpen] = useState(true);
  const [cOpen, setCOpen] = useState(true);

  useOverlaySurface({
    id: "overlay-a",
    open: aOpen,
    onClose: () => {
      onClose("overlay-a");
      setAOpen(false);
    },
    lockBody: true,
  });
  useOverlaySurface({
    id: "overlay-b",
    open: bOpen,
    onClose: () => {
      onClose("overlay-b");
      setBOpen(false);
    },
    lockBody: true,
  });
  useOverlaySurface({
    id: "overlay-c",
    open: cOpen,
    onClose: () => {
      onClose("overlay-c");
      setCOpen(false);
    },
    lockBody: true,
  });

  return null;
}

beforeEach(() => {
  __resetOverlayStateForTests();
  document.body.style.overflow = "";
  document.body.style.paddingRight = "";
});

afterEach(() => {
  vi.clearAllMocks();
  __resetOverlayStateForTests();
});

describe("overlayCoordinator", () => {
  it("closes stacked overlays in reverse registration order and clears body lock", () => {
    const closeOrder: string[] = [];
    render(<LayeredOverlayHarness onClose={(id) => closeOrder.push(id)} />);

    expect(document.body.style.overflow).toBe("hidden");
    expect(hasOpenOverlays()).toBe(true);

    let closed = 0;
    act(() => {
      closed = closeAllOverlays();
    });

    expect(closed).toBe(3);
    expect(closeOrder).toEqual(["overlay-c", "overlay-b", "overlay-a"]);
    expect(hasOpenOverlays()).toBe(false);
    expect(document.body.style.overflow).not.toBe("hidden");
  });

  it("does nothing when nothing is open", () => {
    let closed = 0;
    act(() => {
      closed = closeAllOverlays();
    });
    expect(closed).toBe(0);
    expect(hasOpenOverlays()).toBe(false);
  });

  it("forces a stalled overlay shut down when close callbacks reopen immediately", () => {
    const closeHistory: string[] = [];
    let closeCalls = 0;

    function StickyOverlayHarness() {
      const [tick, setTick] = useState(0);
      const open = tick % 2 === 0;
      const reopenAttempts = useRef(0);
      useOverlaySurface({
        id: "overlay-sticky-close-loop",
        open,
        onClose: () => {
          closeCalls += 1;
          closeHistory.push(`close-${closeCalls}`);
          reopenAttempts.current += 1;
          // Reopen synchronously to model a modal that immediately
          // re-activates itself in its own close callback.
          flushSync(() => {
            setTick((current) => current + 1);
          });
          if (reopenAttempts.current < 3) {
            flushSync(() => {
              setTick((current) => current + 1);
            });
          }
        },
        lockBody: true,
      });
      return null;
    }

    render(<StickyOverlayHarness />);

    expect(document.body.style.overflow).toBe("hidden");
    expect(hasOpenOverlays()).toBe(true);

    let closed = 0;
    act(() => {
      closed = closeAllOverlays();
    });

    expect(closeCalls).toBeGreaterThanOrEqual(2);
    expect(closed).toBeGreaterThanOrEqual(1);
    expect(closeHistory.length).toBe(closeCalls);
    expect(hasOpenOverlays()).toBe(false);
    expect(document.body.style.overflow).not.toBe("hidden");
  });
});
