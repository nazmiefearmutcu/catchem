"""Auto-discovery resilience + determinism (`news_sources._discover`).

The pluggable-pack design rests on two guarantees that had no direct test:
  * **one broken pack never blocks the others** — an import error is logged and
    skipped so the poller still starts with whatever loaded cleanly,
  * **deterministic order** — discovery walks modules name-sorted (v80 fix) so
    which spec wins a name/URL collision in `assemble_feeds` is reproducible.
"""

from __future__ import annotations

import importlib

import catchem.news_sources as ns


def test_discover_skips_a_broken_pack_without_failing(monkeypatch) -> None:
    real_import = importlib.import_module
    victim = "catchem.news_sources.gdelt"

    def _flaky_import(name, *args, **kwargs):
        if name == victim:
            raise RuntimeError("simulated bad pack")
        return real_import(name, *args, **kwargs)

    # Patch the symbol `_discover` actually calls (importlib.import_module).
    monkeypatch.setattr(importlib, "import_module", _flaky_import)

    loaded = ns._discover()  # must NOT raise

    assert "gdelt" not in loaded, "a pack that raises on import must be skipped"
    assert "reddit" in loaded, "other packs must still load despite one failure"
    assert len(loaded) > 5, "discovery should still surface the bulk of the packs"


def test_discover_returns_name_sorted_order(monkeypatch) -> None:
    loaded = ns._discover()
    assert loaded == sorted(loaded), "discovery order must be deterministic (name-sorted)"
    # Private/underscore modules are not source packs and must be excluded.
    assert not any(name.startswith("_") for name in loaded)
