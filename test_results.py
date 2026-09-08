"""Unit tests for the Resultados tab's pure functions -- no DB, no
network, no Flask app context needed for any of these."""

import unittest

from app.results import build_result_series, render_line_svg


def _trade(symbol, entry, exit_, direction="long", at="2026-09-01"):
    return {"symbol": symbol, "entry": entry, "exit": exit_, "direction": direction, "at": at}


class TestBuildResultSeries(unittest.TestCase):
    def test_empty_trades_gives_empty_series(self):
        series = build_result_series([])
        self.assertEqual(series["points"], [])
        self.assertEqual(series["rows"], [])

    def test_single_trade_pct_and_cumulative_match(self):
        series = build_result_series([_trade("BTCUSDT", 100, 110)])
        self.assertAlmostEqual(series["points"][0]["cum_pct"], 10.0)
        self.assertAlmostEqual(series["rows"][0]["pct"], 10.0)

    def test_cumulative_runs_across_trades_in_order(self):
        trades = [_trade("BTCUSDT", 100, 110), _trade("ETHUSDT", 100, 95)]
        series = build_result_series(trades)
        self.assertAlmostEqual(series["points"][0]["cum_pct"], 10.0)
        self.assertAlmostEqual(series["points"][1]["cum_pct"], 5.0)  # 10% - 5%

    def test_direction_aware_short_gain(self):
        series = build_result_series([_trade("BTCUSDT", 100, 90, direction="short")])
        self.assertAlmostEqual(series["points"][0]["cum_pct"], 10.0)

    def test_rows_are_newest_first(self):
        trades = [_trade("BTCUSDT", 100, 110, at="2026-09-01"), _trade("ETHUSDT", 100, 95, at="2026-09-02")]
        series = build_result_series(trades)
        self.assertEqual(series["rows"][0]["symbol"], "ETHUSDT")
        self.assertEqual(series["rows"][1]["symbol"], "BTCUSDT")

    def test_points_stay_in_chronological_order(self):
        trades = [_trade("BTCUSDT", 100, 110, at="2026-09-01"), _trade("ETHUSDT", 100, 95, at="2026-09-02")]
        series = build_result_series(trades)
        self.assertEqual(series["points"][0]["t"], "2026-09-01")
        self.assertEqual(series["points"][1]["t"], "2026-09-02")


class TestRenderLineSvg(unittest.TestCase):
    def test_empty_points_returns_valid_svg(self):
        svg = render_line_svg([])
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("</svg>", svg)
        self.assertNotIn("polyline", svg)  # no data line, just the zero baseline

    def test_single_point_returns_valid_svg(self):
        svg = render_line_svg([{"t": "2026-09-01", "cum_pct": 5.0}])
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("polyline", svg)

    def test_many_points_returns_valid_svg(self):
        points = [{"t": f"2026-09-0{i}", "cum_pct": float(i)} for i in range(1, 6)]
        svg = render_line_svg(points)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("polyline", svg)

    def test_positive_final_value_uses_gain_color(self):
        svg = render_line_svg([{"t": "x", "cum_pct": -5.0}, {"t": "y", "cum_pct": 5.0}])
        self.assertIn("#22C08E", svg)

    def test_negative_final_value_uses_loss_color(self):
        svg = render_line_svg([{"t": "x", "cum_pct": 5.0}, {"t": "y", "cum_pct": -5.0}])
        self.assertIn("#F2555C", svg)

    def test_all_identical_values_does_not_raise(self):
        # would divide by zero (hi == lo) without the span fallback
        points = [{"t": "x", "cum_pct": 0.0}, {"t": "y", "cum_pct": 0.0}]
        svg = render_line_svg(points)
        self.assertTrue(svg.startswith("<svg"))


if __name__ == "__main__":
    unittest.main()
