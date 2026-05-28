"""Auto-discovered awareness source packs.

Each module in this package contributes extra feeds (and optionally new body
parsers) to the news poller by calling, at import time:

    from ..news_poller import register_feed_provider, register_parser, FeedSpec

    @register_feed_provider
    def _my_feeds() -> list[FeedSpec]:
        return [FeedSpec("gdelt-global", GDELT_URL, "gdelt.org", parser="gdelt")]

    register_parser("gdelt", _parse_gdelt)

Importing this package walks every submodule and imports it, so a new source
pack is enabled simply by dropping a `catchem/news_sources/<name>.py` file —
no shared registry file to edit, which keeps parallel authorship collision-
free. One broken pack never blocks the others: import errors are logged and
skipped so the poller still starts with whatever loaded cleanly.
"""

from __future__ import annotations

import importlib
import pkgutil

from ..logging import get_logger

logger = get_logger("catchem.news_sources")


def _discover() -> list[str]:
    """Import every submodule so its register_* side effects fire.

    Returns the list of module names that imported cleanly (handy for tests
    + the /api/news/sources telemetry). Idempotent: Python caches modules, so
    repeated calls don't re-run registration.
    """
    loaded: list[str] = []
    for mod in pkgutil.iter_modules(__path__):
        name = mod.name
        if name.startswith("_"):
            continue  # private helpers, not source packs
        full = f"{__name__}.{name}"
        try:
            importlib.import_module(full)
            loaded.append(name)
        except Exception as exc:  # one bad pack must not break the poller
            logger.warning("news_source_pack_failed", pack=name, error=str(exc))
    return loaded


# Trigger discovery on package import. assemble_feeds() does `import
# catchem.news_sources`, which runs this once.
DISCOVERED: list[str] = _discover()
