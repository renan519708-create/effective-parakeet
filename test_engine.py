"""Unit tests for the engine's pure calculation functions -- no DB, no
network, no Flask app context needed for any of these."""

import unittest

from app.engine import (
    compute_initial_slice, is_drawdown_triggered, is_stop_triggered,
    pick_redirect_leader, price_roi_pct,
)


class TestPriceRoiPct(unittest.TestCase):
    def test_long_gain(self):
        self.assertAlmostEqual(price_roi_pct("long", 100, 110, 1), 10.0)

    def test_long_loss(self):
        self.assertAlmostEqual(price_roi_pct("long", 100, 90, 1), -10.0)

    def test_short_gain_when_price_falls(self):
        self.assertAlmostEqual(price_roi_pct("short", 100, 90, 1), 10.0)

    def test_short_loss_when_price_rises(self):
        self.assertAlmostEqual(price_roi_pct("short", 100, 110, 1), -10.0)

    def test_leverage_scales_the_result(self):
        self.assertAlmostEqual(price_roi_pct("long", 100, 105, 6), 30.0)


class TestIsStopTriggered(unittest.TestCase):
    def test_not_triggered_within_tolerance(self):
        self.assertFalse(is_stop_triggered("long", 100, 99, 1, 2.5))

    def test_triggered_past_threshold(self):
        self.assertTrue(is_stop_triggered("long", 100, 97, 1, 2.5))

    def test_triggered_exactly_at_threshold(self):
        self.assertTrue(is_stop_triggered("long", 100, 97.5, 1, 2.5))

    def test_leverage_widens_the_effective_price_move_needed(self):
        # 1% price move * 6x leverage = 6% ROI -- past a 2.5% stop.
        self.assertTrue(is_stop_triggered("long", 100, 99, 6, 2.5))
        # Same 1% move at 1x leverage does NOT trigger a 2.5% stop.
        self.assertFalse(is_stop_triggered("long", 100, 99, 1, 2.5))

    def test_short_direction(self):
        self.assertTrue(is_stop_triggered("short", 100, 103, 1, 2.5))
        self.assertFalse(is_stop_triggered("short", 100, 101, 1, 2.5))


class TestPickRedirectLeader(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(pick_redirect_leader({}))

    def test_picks_highest_roi(self):
        self.assertEqual(pick_redirect_leader({"BTCUSDT": 1.0, "ETHUSDT": 5.0, "SOLUSDT": -2.0}), "ETHUSDT")

    def test_single_candidate(self):
        self.assertEqual(pick_redirect_leader({"BTCUSDT": -3.0}), "BTCUSDT")

    def test_all_negative_still_picks_the_least_bad(self):
        self.assertEqual(pick_redirect_leader({"BTCUSDT": -5.0, "ETHUSDT": -1.0}), "ETHUSDT")


class TestIsDrawdownTriggered(unittest.TestCase):
    def test_not_triggered_below_threshold(self):
        self.assertFalse(is_drawdown_triggered(1000, 900, 20))

    def test_triggered_past_threshold(self):
        self.assertTrue(is_drawdown_triggered(1000, 750, 20))

    def test_triggered_exactly_at_threshold(self):
        self.assertTrue(is_drawdown_triggered(1000, 800, 20))

    def test_zero_peak_never_triggers(self):
        self.assertFalse(is_drawdown_triggered(0, 0, 20))

    def test_new_peak_resets_the_baseline(self):
        # Portfolio above its own peak is never a drawdown, regardless of pct.
        self.assertFalse(is_drawdown_triggered(1000, 1200, 20))


class TestComputeInitialSlice(unittest.TestCase):
    def test_splits_evenly_across_symbols(self):
        self.assertAlmostEqual(compute_initial_slice(1000, 10, 4), 25.0)

    def test_zero_symbols_returns_zero(self):
        self.assertEqual(compute_initial_slice(1000, 10, 0), 0.0)

    def test_zero_balance_returns_zero(self):
        self.assertEqual(compute_initial_slice(0, 10, 4), 0.0)

    def test_single_symbol_takes_the_whole_risk_slice(self):
        self.assertAlmostEqual(compute_initial_slice(500, 20, 1), 100.0)


if __name__ == "__main__":
    unittest.main()
