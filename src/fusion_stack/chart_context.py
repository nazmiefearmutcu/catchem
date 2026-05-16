"""Stage G: read-only chart context. Looks up a symbol's recent price/return/vol
in any chart artifacts NewsImpact may ship under read-only paths.

**Critical rule:** the values produced here are *metadata only*. In production_safe
mode the consumer must NOT treat them as causal market impact.

If no chart resources are available the function returns an empty dict and
processing continues. We never block on missing data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .logging import get_logger

logger = get_logger("fusion.chart_context")


@dataclass(frozen=True)
class ChartContext:
    symbol: str
    available: bool
    last_return_1d: float | None = None
    last_return_5d: float | None = None
    realized_vol_20d: float | None = None
    last_price: float | None = None
    source_path: str | None = None
    note: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "available": self.available,
            "last_return_1d": self.last_return_1d,
            "last_return_5d": self.last_return_5d,
            "realized_vol_20d": self.realized_vol_20d,
            "last_price": self.last_price,
            "source_path": self.source_path,
            "note": self.note,
        }


class ChartContextReader:
    """Optional reader. If newsimpact_root is None or empty, every lookup is empty."""

    def __init__(self, newsimpact_root: Path | None) -> None:
        self.root = newsimpact_root
        self._index: dict[str, Path] | None = None  # lazy

    def _build_index(self) -> dict[str, Path]:
        idx: dict[str, Path] = {}
        if self.root is None or not self.root.exists():
            return idx
        # Scan likely chart artifact locations. Tolerant of missing trees.
        for sub in ("chunks", "rescued", "models", "manifests"):
            for p in (self.root / sub).rglob("*.json") if (self.root / sub).exists() else ():
                # Heuristic: skip large governance files.
                if any(seg in p.parts for seg in ("governance",)):
                    continue
                try:
                    name = p.name.lower()
                    if any(k in name for k in ("chart", "ohlcv", "price", "returns")):
                        # Try to extract a symbol from filename
                        sym = name.replace(".json", "").upper()
                        idx.setdefault(sym, p)
                except Exception:
                    continue
        return idx

    def lookup(self, symbol: str) -> ChartContext:
        if not symbol:
            return ChartContext(symbol="", available=False, note="empty_symbol")
        if self._index is None:
            self._index = self._build_index()
        path = self._index.get(symbol.upper())
        if path is None:
            return ChartContext(symbol=symbol, available=False, note="no_chart_artifact_for_symbol")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("chart_context_parse_failed", path=str(path), err=str(exc))
            return ChartContext(symbol=symbol, available=False, source_path=str(path), note="parse_failed")
        ctx = _extract_features(data)
        return ChartContext(
            symbol=symbol,
            available=True,
            last_return_1d=ctx.get("last_return_1d"),
            last_return_5d=ctx.get("last_return_5d"),
            realized_vol_20d=ctx.get("realized_vol_20d"),
            last_price=ctx.get("last_price"),
            source_path=str(path),
            note="context_only_not_causal",
        )


def _extract_features(data: Any) -> dict[str, float | None]:
    """Best-effort feature extraction from heterogeneous chart artifacts."""
    out: dict[str, float | None] = {
        "last_return_1d": None,
        "last_return_5d": None,
        "realized_vol_20d": None,
        "last_price": None,
    }
    if isinstance(data, dict):
        for key in ("last_return_1d", "ret_1d", "r1"):
            v = data.get(key)
            if isinstance(v, (int, float)):
                out["last_return_1d"] = float(v); break
        for key in ("last_return_5d", "ret_5d", "r5"):
            v = data.get(key)
            if isinstance(v, (int, float)):
                out["last_return_5d"] = float(v); break
        for key in ("realized_vol_20d", "rv_20d", "vol20"):
            v = data.get(key)
            if isinstance(v, (int, float)):
                out["realized_vol_20d"] = float(v); break
        for key in ("last_price", "close", "px"):
            v = data.get(key)
            if isinstance(v, (int, float)):
                out["last_price"] = float(v); break
        # Try to derive from a series
        series = data.get("close") if isinstance(data.get("close"), list) else data.get("prices")
        if isinstance(series, list) and len(series) >= 21 and out["last_price"] is None:
            try:
                closes = [float(x) for x in series[-21:]]
                out["last_price"] = closes[-1]
                if closes[-2]:
                    out["last_return_1d"] = (closes[-1] - closes[-2]) / closes[-2]
                if closes[0]:
                    out["last_return_5d"] = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else None
                # naive realized vol of log-returns
                import math
                lr = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1]]
                if lr:
                    m = sum(lr) / len(lr)
                    var = sum((x - m) ** 2 for x in lr) / max(1, len(lr) - 1)
                    out["realized_vol_20d"] = math.sqrt(var) * math.sqrt(252)
            except Exception:
                pass
    return out
