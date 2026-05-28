"""Process-level runtime metrics for /api/stats.

The Ops cockpit needs to surface live sidecar RSS / CPU / thread-count so an
operator can spot a runaway process without dropping to ``top``. ``psutil``
is the obvious tool but it's an extra wheel — we make it optional and fall
back to :mod:`resource` (POSIX) so the endpoint never 500s on a build that
shipped without the extra.

Two code paths:

* ``psutil`` available (preferred): proper RSS + VMS + sampled CPU% + thread
  count, all under one ``oneshot()`` so we don't fork the kernel calls.
* fallback (POSIX): ``resource.getrusage(RUSAGE_SELF)`` for an approximate
  RSS reading. On macOS the kernel reports ``ru_maxrss`` in bytes; on Linux
  it's KB — we normalize to KB before converting to MB so the number the UI
  shows is right on both. CPU% and VMS are reported as 0.0 because rusage
  doesn't expose either cheaply; the ``available`` flag tells the UI to
  render an "(estimate)" badge so the user knows what they're looking at.

The ``cpu_percent(interval=0.05)`` sample blocks for 50ms — accepted, since
the endpoint is cached at 2s and only the first uncached call pays it.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

try:
    import psutil  # type: ignore[import-not-found]

    _PROCESS: object | None = psutil.Process(os.getpid())
    _AVAILABLE = True
except ImportError:  # pragma: no cover — exercised on builds without psutil
    psutil = None  # type: ignore[assignment]
    _PROCESS = None
    _AVAILABLE = False


@dataclass(frozen=True)
class ProcessMetrics:
    """Snapshot of process-level health surfaced by ``/api/stats``.

    ``available`` is the canonical signal for "did we get a real reading?".
    When ``False`` the UI should label the row as an estimate (no CPU%, RSS
    is ``ru_maxrss``-derived and may be imprecise compared to a real RSS).
    """

    rss_mb: float
    vms_mb: float
    cpu_percent: float
    num_threads: int
    available: bool


def _fallback_metrics() -> ProcessMetrics:
    """POSIX ``resource``-backed metrics for builds without psutil.

    Returns a best-effort RSS reading in MB and zeros for everything that
    rusage doesn't expose. Never raises — a totally exotic platform that
    can't even import :mod:`resource` would surface a zeroed metric with
    ``available=False``.
    """
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss_raw = usage.ru_maxrss
        # macOS reports ru_maxrss in BYTES, Linux in KILOBYTES. Normalize
        # to KB before the /1024 conversion to MB below — otherwise macOS
        # would over-report by 1024×.
        if sys.platform == "darwin":
            rss_kb = max(0, rss_raw) // 1024
        else:
            rss_kb = max(0, rss_raw)
        return ProcessMetrics(
            rss_mb=float(rss_kb) / 1024.0,
            vms_mb=0.0,
            cpu_percent=0.0,
            num_threads=0,
            available=False,
        )
    except Exception:  # pragma: no cover — defensive only
        return ProcessMetrics(
            rss_mb=0.0,
            vms_mb=0.0,
            cpu_percent=0.0,
            num_threads=0,
            available=False,
        )


def current_metrics() -> ProcessMetrics:
    """Return a fresh :class:`ProcessMetrics` snapshot.

    Uses psutil when available, the resource-rusage fallback otherwise.
    The 50ms CPU sample only runs in the psutil branch and only when the
    extra is installed — it's the right grain for an Ops dial that's
    polled every 5s and cached for 2s upstream.
    """
    if not _AVAILABLE or _PROCESS is None:
        return _fallback_metrics()

    # psutil's `oneshot()` batches the kernel calls so memory_info() +
    # cpu_percent() + num_threads() resolve from a single read where the
    # platform allows it. The cpu_percent(interval=0.05) blocks for 50ms
    # — that's the documented minimum sample window for a meaningful %.
    proc = _PROCESS  # narrow type for the checker
    with proc.oneshot():  # type: ignore[attr-defined]
        mem = proc.memory_info()  # type: ignore[attr-defined]
        cpu = proc.cpu_percent(interval=0.05)  # type: ignore[attr-defined]
        threads = proc.num_threads()  # type: ignore[attr-defined]
    return ProcessMetrics(
        rss_mb=float(mem.rss) / 1024.0 / 1024.0,
        vms_mb=float(mem.vms) / 1024.0 / 1024.0,
        cpu_percent=float(cpu),
        num_threads=int(threads),
        available=True,
    )
