"""Map asset class + reason code combinations to "market channels" — coarse
buckets a trader/analyst would route to. This is intentionally simple; the goal
is to surface a routable signal rather than make a market-impact claim.
"""

from __future__ import annotations

from collections.abc import Iterable

# (asset_class, reason_code) → channel id. Wildcard "*" matches any.
_CHANNEL_RULES: list[tuple[str, str, str]] = [
    ("equities", "earnings", "equities.earnings"),
    ("equities", "guidance", "equities.guidance"),
    ("equities", "m_and_a", "equities.m_and_a"),
    ("equities", "product_launch", "equities.product"),
    ("equities", "fraud_governance", "equities.governance"),
    ("equities", "litigation", "equities.litigation"),
    ("equities", "regulation", "equities.regulation"),
    ("equities", "esg_reputation", "equities.esg"),
    # BUG-Z: every other asset class had a `*` wildcard catch-all; equities
    # was the lone exception. A record `asset=[equities]` with no recognised
    # reason code (or only reasons we don't have an equities-specific bucket
    # for) used to return `channels=[]`. Now it lands in `equities.general`,
    # matching the policy of every other asset class.
    ("equities", "*", "equities.general"),
    ("indices", "*", "indices.macro"),
    ("rates", "central_bank", "rates.policy"),
    ("rates", "inflation", "rates.inflation"),
    ("rates", "employment", "rates.labor"),
    ("rates", "growth_recession", "rates.growth"),
    ("rates", "*", "rates.general"),
    ("credit", "funding_liquidity", "credit.funding"),
    ("credit", "*", "credit.general"),
    ("fx", "central_bank", "fx.policy"),
    ("fx", "*", "fx.general"),
    ("commodities", "energy", "commodities.energy"),
    ("commodities", "metals", "commodities.metals"),
    ("commodities", "supply_chain", "commodities.logistics"),
    ("commodities", "geopolitics", "commodities.geo"),
    ("commodities", "*", "commodities.general"),
    ("crypto", "*", "crypto.general"),
    ("macro", "inflation", "macro.inflation"),
    ("macro", "central_bank", "macro.policy"),
    ("macro", "employment", "macro.labor"),
    ("macro", "growth_recession", "macro.growth"),
    ("macro", "geopolitics", "macro.geo"),
    ("macro", "sanctions_trade", "macro.trade"),
    ("macro", "*", "macro.general"),
]


def map_channels(asset_classes: Iterable[str], reason_codes: Iterable[str]) -> list[str]:
    asset_set = set(asset_classes)
    reason_set = set(reason_codes) or {"*"}
    channels: list[str] = []
    seen: set[str] = set()
    for asset, reason, channel in _CHANNEL_RULES:
        if asset not in asset_set and asset != "*":
            continue
        if reason != "*" and reason not in reason_set:
            continue
        if channel in seen:
            continue
        seen.add(channel)
        channels.append(channel)
    return channels
