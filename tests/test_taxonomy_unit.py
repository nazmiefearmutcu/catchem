from __future__ import annotations

from pathlib import Path

import pytest

from catchem.taxonomy import (
    LabelDef,
    Taxonomy,
    _build_labels,
    default_taxonomy_path,
    load_taxonomy,
)


def test_taxonomy_properties_and_priors() -> None:
    # Construct a Taxonomy manually to test priors, thresholds, and properties
    t = Taxonomy(
        asset_classes=(LabelDef(id="equities", hypothesis="Equities hypothesis"),),
        impact_reason_codes=(LabelDef(id="inflation", hypothesis="Inflation hypothesis"),),
        negative_class=(LabelDef(id="sports", hypothesis="Sports hypothesis"),),
        horizons=(),
        thresholds={"equities": 0.45},
        domain_priors={"bloomberg.com": 0.8, "default": 0.35},
        source_type_priors={"rss": 0.6, "default": 0.25},
    )

    # Test properties
    assert t.asset_class_ids == ("equities",)
    assert t.reason_code_ids == ("inflation",)
    assert t.negative_class_ids == ("sports",)

    # Test all_hypotheses
    hyps = t.all_hypotheses()
    assert hyps == {
        "equities": "Equities hypothesis",
        "inflation": "Inflation hypothesis",
        "sports": "Sports hypothesis",
    }

    # Test domain_prior
    assert t.domain_prior(None) == 0.35
    assert t.domain_prior("") == 0.35
    assert t.domain_prior("BLOOMBERG.COM") == 0.8
    assert t.domain_prior("unknown.com") == 0.35

    # Test source_type_prior
    assert t.source_type_prior(None) == 0.25
    assert t.source_type_prior("") == 0.25
    assert t.source_type_prior("Rss") == 0.6
    assert t.source_type_prior("unknown") == 0.25

    # Test threshold
    assert t.threshold("equities") == 0.45
    assert t.threshold("rates", default=0.2) == 0.2


def test_build_labels_edge_cases() -> None:
    # Test None raw input
    assert _build_labels(None) == ()

    # Test item list with invalid structure, empty/missing IDs
    raw = [
        None,  # Not a dict
        "string",  # Not a dict
        {"hypothesis": "Missing ID"},
        {"id": "   ", "hypothesis": "Empty ID"},
        {"id": "valid_id", "aliases": "not_a_list_or_tuple"},
        {"id": "equities", "aliases": ["stock", "equity"]},
    ]
    res = _build_labels(raw)
    assert len(res) == 2
    assert res[0].id == "valid_id"
    assert res[0].aliases == ()
    assert res[1].id == "equities"
    assert res[1].aliases == ("stock", "equity")


def test_load_taxonomy_exceptions(tmp_path: Path) -> None:
    # Test loading a valid taxonomy
    tax = load_taxonomy(default_taxonomy_path())
    assert isinstance(tax, Taxonomy)
    assert len(tax.asset_classes) > 0

    # Test non-existent path
    with pytest.raises(FileNotFoundError):
        load_taxonomy(tmp_path / "does_not_exist.yaml")

    # Test taxonomy that does not parse to a dictionary
    invalid_file = tmp_path / "invalid.yaml"
    invalid_file.write_text("- not\n- a\n- dict", encoding="utf-8")
    with pytest.raises(ValueError, match="did not parse to a mapping"):
        load_taxonomy(invalid_file)



def test_default_taxonomy_path() -> None:
    p = default_taxonomy_path()
    assert p.is_file()
    assert p.name == "taxonomy.yaml"
