from __future__ import annotations

from oracle.robust import mad, median, percentile, reject_outliers, robust_price


def test_median_basic():
    assert median([300, 350, 400, 360, 340]) == 350.0
    assert median([]) is None
    assert median([None, -5, 100]) == 100.0


def test_mad_is_zero_for_identical_values():
    assert mad([100.0, 100.0, 100.0]) == 0.0


def test_percentile_interpolates():
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0.0) == 10.0
    assert percentile(values, 1.0) == 40.0
    assert percentile(values, 0.5) == 25.0


def test_outlier_rejection_drops_the_wild_value():
    """The £2,000 typo is exactly what a plain median fails to survive."""
    prices = [300, 310, 320, 305, 315, 2000]
    kept, rejected = reject_outliers(prices)
    assert 2000 in rejected
    assert len(kept) == 5


def test_outlier_rejection_keeps_everything_when_sample_is_tiny():
    # Below four comps there is not enough signal to call anything an outlier.
    kept, rejected = reject_outliers([100, 900])
    assert rejected == []
    assert len(kept) == 2


def test_outlier_rejection_handles_zero_spread():
    """Half the comps identical makes MAD zero; the fallback band still works."""
    prices = [200, 200, 200, 200, 900]
    kept, rejected = reject_outliers(prices)
    assert rejected == [900]
    assert kept == [200, 200, 200, 200]


def test_robust_price_reports_sample_and_spread():
    est = robust_price([300, 310, 320, 305, 315, 2000])
    assert est is not None
    assert est.value == 310.0
    assert est.n_used == 5
    assert est.n_total == 6
    assert est.n_rejected == 1
    assert est.p10 < est.value < est.p90


def test_robust_price_none_when_nothing_usable():
    assert robust_price([]) is None
    assert robust_price([0, -1, None]) is None


def test_cv_grows_as_the_market_disagrees():
    tight = robust_price([300, 302, 298, 301, 299])
    loose = robust_price([200, 300, 400, 250, 350])
    assert tight.cv < loose.cv
