from __future__ import annotations

from app.models import AssetType, MarketplaceListing


DEFAULT_MULTIPLIERS: dict[str, tuple[float, float]] = {
    AssetType.WEBSITE.value: (30.0, 40.0),
    AssetType.APP.value: (24.0, 36.0),
    AssetType.YOUTUBE.value: (24.0, 30.0),
    AssetType.ECOMMERCE.value: (24.0, 36.0),
    AssetType.SAAS.value: (36.0, 60.0),
    AssetType.NEWSLETTER.value: (20.0, 35.0),
    AssetType.OTHER.value: (20.0, 30.0),
}


class RevenueMultiplier:
    def __init__(self, multipliers: dict[str, tuple[float, float]] | None = None) -> None:
        self._multipliers = dict(DEFAULT_MULTIPLIERS)
        if multipliers:
            self._multipliers.update({key.lower(): value for key, value in multipliers.items()})

    def for_listing(self, listing: MarketplaceListing) -> tuple[float, float]:
        asset_type = listing.asset_type.value.lower()
        if asset_type in self._multipliers:
            return self._multipliers[asset_type]

        text = f"{listing.name} {listing.niche}".lower()
        if "youtube" in text or "channel" in text:
            return self._multipliers[AssetType.YOUTUBE.value]
        if "ios" in text or "android" in text or "app" in text:
            return self._multipliers[AssetType.APP.value]
        if "saas" in text or "software" in text:
            return self._multipliers[AssetType.SAAS.value]
        return self._multipliers[AssetType.WEBSITE.value]

