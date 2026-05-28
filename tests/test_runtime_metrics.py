"""Contract pins for :mod:`catchem.runtime_metrics`.

Two-headed coverage:

* the live ``current_metrics()`` path — must always return a
  :class:`ProcessMetrics` with non-negative RSS / threads / CPU% regardless
  of whether psutil is on the path.
* the explicit ``_fallback_metrics()`` path — exercised even on builds
  that DO have psutil so we don't regress the rusage code-path the moment
  the optional extra disappears from a wheel.

The numerical lower bounds are intentionally loose (e.g. RSS must be > 0,
but no upper-bound assertion) because the absolute size of the test
runner process is host-dependent. We only pin the invariants the wire
contract requires the UI to be able to trust.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

import catchem.runtime_metrics as rm
from catchem.runtime_metrics import (
    ProcessMetrics,
    _fallback_metrics,
    current_metrics,
)


def test_current_metrics_returns_process_metrics_instance() -> None:
    m = current_metrics()
    assert isinstance(m, ProcessMetrics)


def test_current_metrics_rss_is_positive() -> None:
    # The Python interpreter itself always has a non-trivial RSS, whether
    # we get the value from psutil OR from ru_maxrss. The wire contract
    # promises a float; the UI renders it with `.toFixed(1)` directly.
    m = current_metrics()
    assert isinstance(m.rss_mb, float)
    assert m.rss_mb > 0.0


def test_current_metrics_non_negative_floor() -> None:
    # All four numeric fields must read as non-negative — a negative would
    # confuse the UI which renders CPU% with `.toFixed(1)` and assumes a
    # display-clean value (and number formatters that hand back "-0.0").
    m = current_metrics()
    assert m.rss_mb >= 0.0
    assert m.vms_mb >= 0.0
    assert m.cpu_percent >= 0.0
    assert m.num_threads >= 0


def test_current_metrics_threads_minimum_when_available() -> None:
    # On the psutil branch we have the real thread count — must be ≥ 1
    # because *this* code is running. On the fallback branch threads is
    # zero (rusage doesn't expose it); guard the assertion so we don't
    # fail on a wheel that intentionally omitted psutil.
    m = current_metrics()
    if m.available:
        assert m.num_threads >= 1


def test_fallback_metrics_runs_without_psutil() -> None:
    # The explicit rusage code path must never raise and must always come
    # back with available=False so the UI can render the "(estimate)" label.
    m = _fallback_metrics()
    assert isinstance(m, ProcessMetrics)
    assert m.available is False
    assert m.cpu_percent == 0.0
    assert m.vms_mb == 0.0
    assert m.num_threads == 0
    assert m.rss_mb >= 0.0


def test_available_flag_matches_module_capability() -> None:
    # ``rm._AVAILABLE`` is the truth flag for "psutil imported clean";
    # the rendered ``ProcessMetrics.available`` field MUST agree with it
    # so the UI's branching ("did we get a real reading?") stays honest.
    m = current_metrics()
    assert m.available is bool(rm._AVAILABLE)


# ── Deterministic psutil branch (fake process, no real sampling) ──────────────
#
# In CI the test runner has psutil, so `current_metrics()` always hits the
# psutil path with host-dependent numbers. These tests pin the byte→MB maths
# and field mapping with a fake process so the values are exact and the 50ms
# cpu_percent sample never actually blocks.


class _FakeMemInfo:
    def __init__(self, rss: int, vms: int) -> None:
        self.rss = rss
        self.vms = vms


class _FakeProcess:
    """Stand-in for ``psutil.Process`` with deterministic readings.

    ``oneshot()`` is the context manager the real API uses to batch kernel
    reads; we make it a no-op. ``cpu_percent(interval=...)`` returns a fixed
    value WITHOUT sleeping, so the test is instant.
    """

    def __init__(self, rss: int, vms: int, cpu: float, threads: int) -> None:
        self._mem = _FakeMemInfo(rss, vms)
        self._cpu = cpu
        self._threads = threads
        self.cpu_interval_seen: float | None = None

    @contextmanager
    def oneshot(self):
        yield

    def memory_info(self) -> _FakeMemInfo:
        return self._mem

    def cpu_percent(self, interval: float = 0.0) -> float:
        self.cpu_interval_seen = interval
        return self._cpu

    def num_threads(self) -> int:
        return self._threads


def test_psutil_branch_maps_fields_and_converts_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    # 100 MiB RSS / 250 MiB VMS in bytes → exact MB after /1024/1024.
    fake = _FakeProcess(rss=100 * 1024 * 1024, vms=250 * 1024 * 1024, cpu=12.5, threads=7)
    monkeypatch.setattr(rm, "_AVAILABLE", True)
    monkeypatch.setattr(rm, "_PROCESS", fake)

    m = current_metrics()
    assert m.available is True
    assert m.rss_mb == pytest.approx(100.0)
    assert m.vms_mb == pytest.approx(250.0)
    assert m.cpu_percent == pytest.approx(12.5)
    assert m.num_threads == 7
    # The documented 50ms sample window must be the value passed through.
    assert fake.cpu_interval_seen == pytest.approx(0.05)


def test_current_metrics_falls_back_when_psutil_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the "no psutil" dispatch (module-level _AVAILABLE False / _PROCESS
    # None). This is the branch a wheel shipped without the extra would hit;
    # it must route to the rusage fallback and report available=False.
    monkeypatch.setattr(rm, "_AVAILABLE", False)
    monkeypatch.setattr(rm, "_PROCESS", None)
    m = current_metrics()
    assert m.available is False
    assert m.cpu_percent == 0.0
    assert m.vms_mb == 0.0
    assert m.rss_mb >= 0.0


def test_current_metrics_falls_back_when_process_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defensive twin: _AVAILABLE True but _PROCESS somehow None must still
    # take the fallback rather than dereferencing None.
    monkeypatch.setattr(rm, "_AVAILABLE", True)
    monkeypatch.setattr(rm, "_PROCESS", None)
    m = current_metrics()
    assert m.available is False


# ── rusage normalization: macOS reports ru_maxrss in BYTES, Linux in KB ───────


def test_fallback_normalizes_macos_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    # On darwin ru_maxrss is BYTES. 200 MiB in bytes must come back as 200 MB,
    # i.e. the //1024 (bytes→KB) step before /1024 (KB→MB) must run.
    import resource

    class _Usage:
        ru_maxrss = 200 * 1024 * 1024  # 200 MiB in bytes

    monkeypatch.setattr(rm.sys, "platform", "darwin")
    monkeypatch.setattr(resource, "getrusage", lambda _who: _Usage())
    m = _fallback_metrics()
    assert m.rss_mb == pytest.approx(200.0)
    assert m.available is False


def test_fallback_normalizes_linux_kilobytes(monkeypatch: pytest.MonkeyPatch) -> None:
    # On linux ru_maxrss is already KB. 200 MiB expressed as KB (204800) must
    # come back as 200 MB WITHOUT the extra //1024 the darwin branch applies.
    import resource

    class _Usage:
        ru_maxrss = 200 * 1024  # 200 MiB expressed in KB

    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(resource, "getrusage", lambda _who: _Usage())
    m = _fallback_metrics()
    assert m.rss_mb == pytest.approx(200.0)
    assert m.available is False


def test_fallback_clamps_negative_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bogus negative ru_maxrss must clamp to 0 (max(0, ...)), never a
    # negative RSS the UI would render as a nonsense value.
    import resource

    class _Usage:
        ru_maxrss = -123456

    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(resource, "getrusage", lambda _who: _Usage())
    m = _fallback_metrics()
    assert m.rss_mb == 0.0
