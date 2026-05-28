"""Load and validate the finance taxonomy. The pipeline asks `Taxonomy` for
zero-shot hypotheses and thresholds — never hardcodes labels."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


@dataclass(frozen=True)
class LabelDef:
    id: str
    hypothesis: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Taxonomy:
    asset_classes: tuple[LabelDef, ...]
    impact_reason_codes: tuple[LabelDef, ...]
    negative_class: tuple[LabelDef, ...]
    horizons: tuple[LabelDef, ...]
    thresholds: Mapping[str, float]
    domain_priors: Mapping[str, float]
    source_type_priors: Mapping[str, float]

    @property
    def asset_class_ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.asset_classes)

    @property
    def reason_code_ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.impact_reason_codes)

    @property
    def negative_class_ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.negative_class)

    def all_hypotheses(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for group in (self.asset_classes, self.impact_reason_codes, self.negative_class):
            for d in group:
                out[d.id] = d.hypothesis
        return out

    def domain_prior(self, domain: str | None) -> float:
        if not domain:
            return float(self.domain_priors.get("default", 0.4))
        return float(self.domain_priors.get(domain.lower(), self.domain_priors.get("default", 0.4)))

    def source_type_prior(self, source_type: str | None) -> float:
        if not source_type:
            return float(self.source_type_priors.get("default", 0.4))
        return float(self.source_type_priors.get(source_type.lower(), self.source_type_priors.get("default", 0.4)))

    def threshold(self, key: str, default: float = 0.30) -> float:
        return float(self.thresholds.get(key, default))


def _build_labels(raw: list[dict[str, object]] | None) -> tuple[LabelDef, ...]:
    if not raw:
        return ()
    out: list[LabelDef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            continue
        hypothesis = str(item.get("hypothesis") or f"This text is about {item_id}.")
        aliases_raw = item.get("aliases") or ()
        aliases = tuple(str(a) for a in aliases_raw) if isinstance(aliases_raw, (list, tuple)) else ()
        out.append(LabelDef(id=item_id, hypothesis=hypothesis, aliases=aliases))
    return tuple(out)


@lru_cache(maxsize=4)
def load_taxonomy(path: str | Path) -> Taxonomy:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"taxonomy not found at {p}")
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"taxonomy at {p} did not parse to a mapping")
    return Taxonomy(
        asset_classes=_build_labels(data.get("asset_classes")),
        impact_reason_codes=_build_labels(data.get("impact_reason_codes")),
        negative_class=_build_labels(data.get("negative_class")),
        horizons=_build_labels(data.get("horizons")),
        thresholds=dict(data.get("thresholds") or {}),
        domain_priors=dict(data.get("domain_priors") or {}),
        source_type_priors=dict(data.get("source_type_priors") or {}),
    )


def default_taxonomy_path() -> Path:
    """Default location: <project>/configs/taxonomy.yaml."""
    return Path(__file__).resolve().parents[2] / "configs" / "taxonomy.yaml"
