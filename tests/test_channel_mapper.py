from __future__ import annotations

from catchem.channel_mapper import map_channels


def test_equities_earnings_maps_to_specific_then_general() -> None:
    # A wildcard ("*") rule always fires for a matching asset, so a recognised
    # reason yields BOTH its specific bucket and the asset's general bucket.
    assert map_channels(["equities"], ["earnings"]) == [
        "equities.earnings",
        "equities.general",
    ]


def test_equities_unknown_reason_falls_into_general_bucket() -> None:
    """BUG-Z regression: equities used to be the lone asset class without a
    `*` wildcard, so an unrecognised reason returned []. It must now land in
    equities.general like every other asset class."""
    assert map_channels(["equities"], ["totally_unknown_reason"]) == ["equities.general"]


def test_equities_no_reasons_defaults_to_general() -> None:
    # Empty reason set is treated as `{"*"}`, so the wildcard rule fires.
    assert map_channels(["equities"], []) == ["equities.general"]


def test_unknown_asset_class_yields_no_channels() -> None:
    assert map_channels(["pokemon_cards"], ["earnings"]) == []


def test_empty_asset_classes_yields_no_channels() -> None:
    assert map_channels([], ["earnings"]) == []


def test_empty_inputs_yield_no_channels() -> None:
    assert map_channels([], []) == []


def test_specific_reason_also_emits_general_via_wildcard() -> None:
    """A recognised reason matches its specific rule; the trailing `*` rule
    for the same asset ALSO fires (wildcard rules ignore the reason set), so
    the general bucket appears after the specific one in rule order."""
    out = map_channels(["rates"], ["central_bank"])
    assert out == ["rates.policy", "rates.general"]


def test_unrecognised_reason_for_wildcarded_asset_hits_general() -> None:
    assert map_channels(["rates"], ["mystery"]) == ["rates.general"]


def test_indices_only_have_macro_wildcard() -> None:
    assert map_channels(["indices"], ["anything"]) == ["indices.macro"]
    assert map_channels(["indices"], []) == ["indices.macro"]


def test_crypto_only_has_general_wildcard() -> None:
    assert map_channels(["crypto"], ["earnings"]) == ["crypto.general"]


def test_multiple_assets_accumulate_channels_in_rule_order() -> None:
    out = map_channels(["fx", "commodities"], ["central_bank", "energy"])
    # Rule order: fx.policy + fx.general (fx rules) precede commodities rules,
    # and each asset's wildcard general bucket also fires.
    assert out == [
        "fx.policy",
        "fx.general",
        "commodities.energy",
        "commodities.general",
    ]


def test_multiple_reasons_for_one_asset_emit_each_specific_bucket() -> None:
    out = map_channels(["macro"], ["inflation", "central_bank"])
    assert "macro.inflation" in out
    assert "macro.policy" in out
    # The wildcard rule always fires too, so general is present and last.
    assert out[-1] == "macro.general"


def test_result_has_no_duplicate_channels() -> None:
    # Two specific buckets + the always-firing wildcard general; the `seen`
    # set must keep every channel unique even with duplicate asset entries.
    out = map_channels(["commodities", "commodities"], ["energy", "metals"])
    assert out == [
        "commodities.energy",
        "commodities.metals",
        "commodities.general",
    ]
    assert len(out) == len(set(out))


def test_credit_funding_specific_then_general() -> None:
    assert map_channels(["credit"], ["funding_liquidity"]) == [
        "credit.funding",
        "credit.general",
    ]
    assert map_channels(["credit"], ["weird"]) == ["credit.general"]


def test_iterables_other_than_lists_are_accepted() -> None:
    # Generators / sets are valid Iterables for the public signature.
    out = map_channels((a for a in ["equities"]), {"earnings"})
    assert out == ["equities.earnings", "equities.general"]


def test_explicit_wildcard_reason_token_is_harmless() -> None:
    """Passing a literal '*' reason changes nothing — the wildcard rule fires
    regardless — so the output equals the recognised-reason case."""
    assert map_channels(["equities"], ["earnings", "*"]) == map_channels(
        ["equities"], ["earnings"]
    )
