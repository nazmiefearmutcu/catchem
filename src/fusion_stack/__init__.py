"""fusion_stack — sidecar workspace that fuses Awareness ingestion with a guarded
finance-relevance layer.

Architectural invariant: Awareness is the upstream system of record. fusion_stack
consumes JSONL captures after they are durably committed. NewsImpact is treated
as a quarantined research artifact and is only loaded in research_diagnostic mode.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
