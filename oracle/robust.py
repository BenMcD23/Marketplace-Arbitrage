"""Robust statistics for pricing from noisy marketplace comps.

Marketplace comp sets are dirty in a very specific way: a search for
"iPhone 12 128GB" returns a handful of genuine handsets plus cases, cracked
screens, "empty box" listings and the occasional £3,000 typo. A plain median
survives a few outliers but still drifts once the junk is a meaningful share of
the sample, and it tells you nothing about how much to trust the answer.

Everything here is a pure function over a list of prices so it can be tested
offline and reasoned about without a network.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

# Below this many comps, dispersion estimates are too unstable to act on and we
# fall back to the plain median.
_MIN_FOR_OUTLIER_REJECTION = 4

# Modified z-score cutoff. 3.5 is the conventional Iglewicz-Hoaglin threshold.
_MAD_Z_CUTOFF = 3.5

# 1 / 0.6745 — scales MAD to be a consistent estimator of sigma for normal data.
_MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True)
class PriceEstimate:
    """A price point plus everything needed to judge how much to trust it."""

    value: float
    #: Comps that survived outlier rejection.
    n_used: int
    #: Comps supplied before outlier rejection.
    n_total: int
    #: Robust spread (MAD-derived sigma), in currency units.
    spread: float
    #: spread / value — unitless dispersion. High = the market disagrees.
    cv: float
    #: 10th/90th percentile of the retained comps, for a "realistic range".
    p10: float
    p90: float

    @property
    def n_rejected(self) -> int:
        return self.n_total - self.n_used


def median(values: list[float]) -> float | None:
    """Plain median of positive values. Kept for callers that only need a number."""
    clean = [v for v in values if v is not None and v > 0]
    if not clean:
        return None
    return round(statistics.median(clean), 2)


def mad(values: list[float], centre: float | None = None) -> float:
    """Median absolute deviation — the outlier-resistant answer to stdev."""
    if not values:
        return 0.0
    centre = statistics.median(values) if centre is None else centre
    return statistics.median([abs(v - centre) for v in values])


def reject_outliers(values: list[float]) -> tuple[list[float], list[float]]:
    """Split values into (kept, rejected) using a modified z-score.

    Uses MAD rather than stdev because stdev is itself dragged around by the
    outliers we are trying to find. When MAD is zero — which happens when more
    than half the comps share an identical price, common for new/sealed goods —
    we fall back to a proportional band around the median so a single wild value
    is still caught.
    """
    clean = sorted(v for v in values if v is not None and v > 0)
    if len(clean) < _MIN_FOR_OUTLIER_REJECTION:
        return clean, []

    centre = statistics.median(clean)
    deviation = mad(clean, centre)

    if deviation == 0:
        # Degenerate spread: keep anything within ±40% of the median.
        kept = [v for v in clean if abs(v - centre) <= 0.4 * centre]
        rejected = [v for v in clean if abs(v - centre) > 0.4 * centre]
        return kept, rejected

    kept: list[float] = []
    rejected: list[float] = []
    for v in clean:
        z = 0.6745 * abs(v - centre) / deviation
        (kept if z <= _MAD_Z_CUTOFF else rejected).append(v)
    return kept, rejected


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list. q in [0, 1]."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def robust_price(values: list[float]) -> PriceEstimate | None:
    """Outlier-rejected median plus dispersion, from a list of comp prices.

    Returns None when there is nothing usable to price against.
    """
    kept, rejected = reject_outliers(values)
    if not kept:
        return None

    kept_sorted = sorted(kept)
    centre = statistics.median(kept_sorted)
    sigma = mad(kept_sorted, centre) * _MAD_TO_SIGMA
    cv = (sigma / centre) if centre > 0 else 0.0

    return PriceEstimate(
        value=round(centre, 2),
        n_used=len(kept),
        n_total=len(kept) + len(rejected),
        spread=round(sigma, 2),
        cv=round(cv, 3),
        p10=round(percentile(kept_sorted, 0.10), 2),
        p90=round(percentile(kept_sorted, 0.90), 2),
    )
