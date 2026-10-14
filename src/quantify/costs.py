"""HSBC Trade25 cost model: monthly free quota on cumulative turnover (buys + sells)."""

from dataclasses import dataclass, field


@dataclass
class HsbcCosts:
    quota_hkd: float
    usd_hkd: float
    commission_usd: float
    per_share_over_1000_usd: float
    monthly_fee_hkd: float
    other_fees_pct: float
    _used_hkd: dict[str, float] = field(default_factory=dict)  # "YYYY-MM" -> turnover

    @classmethod
    def from_config(cls, cfg: dict) -> "HsbcCosts":
        b = cfg["broker"]
        return cls(b["monthly_free_quota_hkd"], cfg["account"]["usd_hkd"], b["commission_usd"],
                   b["per_share_over_1000_usd"], b["monthly_fee_hkd"], b["other_fees_pct"])

    def used_hkd(self, month: str) -> float:
        return self._used_hkd.get(month, 0.0)

    def remaining_hkd(self, month: str) -> float:
        return max(self.quota_hkd - self.used_hkd(month), 0.0)

    def fits_quota(self, month: str, roundtrip_usd: float) -> bool:
        return roundtrip_usd * self.usd_hkd <= self.remaining_hkd(month)

    def fill(self, month: str, notional_usd: float, shares: int) -> float:
        """Record one fill (buy or sell); return its fees in USD.

        Conservative reading of the rule: the fill that pushes the month past the quota is charged.
        """
        self._used_hkd[month] = self.used_hkd(month) + notional_usd * self.usd_hkd
        fees = notional_usd * self.other_fees_pct
        if self._used_hkd[month] > self.quota_hkd:
            fees += self.commission_usd + max(shares - 1000, 0) * self.per_share_over_1000_usd
        return fees

    def monthly_fee_usd(self) -> float:
        return self.monthly_fee_hkd / self.usd_hkd
