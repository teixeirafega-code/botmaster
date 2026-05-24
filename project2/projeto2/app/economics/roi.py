from __future__ import annotations

from app.config.settings import Settings
from app.economics.models import AcquisitionDecision, ValuationResult


class ROIOptimizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def decide(self, valuation: ValuationResult) -> AcquisitionDecision:
        if valuation.expected_resale_probability < self.settings.economics.minimum_resale_probability:
            return AcquisitionDecision(False, "low_resale_probability", valuation)
        if valuation.time_adjusted_roi < self.settings.economics.minimum_time_adjusted_roi:
            return AcquisitionDecision(False, "low_time_adjusted_roi", valuation)
        if valuation.expected_roi < self.settings.economics.minimum_expected_roi:
            return AcquisitionDecision(False, "low_expected_roi", valuation)
        if valuation.purchase_confidence < self.settings.economics.minimum_purchase_confidence:
            return AcquisitionDecision(False, "low_purchase_confidence", valuation)
        return AcquisitionDecision(True, "positive_expected_value", valuation)

