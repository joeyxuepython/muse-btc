import unittest

import numpy as np
from baseline import DAY, HOUR, independent_returns, signals, simulate, validate_rows


class ResearchTests(unittest.TestCase):
    def test_hand_calculated_roundtrip(self):
        result = simulate(np.array([100.0, 110.0, 120.0]), [1, 1], 0.001, 0.0005)
        expected = 10000 / (100 * 1.0005 * 1.001) * 120 * 0.9995 * 0.999
        self.assertAlmostEqual(result["equity"][-1], expected, places=8)
        self.assertEqual(len(result["events"]), 2)
        self.assertTrue(result["events"][-1]["terminal_liquidation"])

    def test_flat_market_cost_and_cash(self):
        prices = np.ones(5) * 100
        gross = simulate(prices, [1, 0, 1, 0], 0, 0)
        net = simulate(prices, [1, 0, 1, 0], 0.001, 0.0005)
        cash = simulate(prices, [0, 0, 0, 0], 0.001, 0.0005)
        self.assertAlmostEqual(gross["equity"][-1], 10000)
        self.assertLess(net["equity"][-1], 10000)
        self.assertAlmostEqual(cash["equity"][-1], 10000)

    def test_sell_before_down_interval(self):
        result = simulate(np.array([100.0, 110.0, 55.0]), [1, 0], 0, 0)
        self.assertAlmostEqual(result["equity"][-1], 11000)

    def test_first_interval_and_final_costs(self):
        r = simulate(np.array([100.0, 100.0]), [1], 0.01, 0.02)
        expected_factor = 0.98 * 0.99 / (1.02 * 1.01)
        self.assertAlmostEqual(r["returns"][0], expected_factor - 1)

    def test_fractional_rebalance_independent_ledger(self):
        rng = np.random.default_rng(47)
        prices = np.exp(np.cumsum(rng.normal(0, 0.08, 100))) * 100
        weights = rng.uniform(0, 1, 99)
        a = simulate(prices, weights, 0.001, 0.0005)
        b = independent_returns(prices, weights, 0.001, 0.0005)
        np.testing.assert_allclose(a["returns"], b, atol=1e-12)
        self.assertTrue(all(e["cash_after"] >= -1e-8 for e in a["events"]))

    def test_future_data_cannot_change_signal(self):
        dd = {i * DAY: [i * DAY, 0, 0, 0, 100 + i] for i in range(220)}
        times = [i * DAY + HOUR for i in range(200, 210)]
        before, obs = signals(dd, times, 200)
        for i in range(210, 220):
            dd[i * DAY][4] = 1e9
        after, _ = signals(dd, times, 200)
        self.assertEqual(before, after)
        self.assertTrue(
            all(
                o["last_input_close_utc"] < o["signal_available_utc"] < o["scheduled_execution_utc"]
                for o in obs
            )
        )

    def test_signal_never_uses_same_day_close(self):
        dd = {i * DAY: [i * DAY, 0, 0, 0, 100] for i in range(201)}
        dd[200 * DAY][4] = 100000
        weights, _ = signals(dd, [200 * DAY + HOUR], 200)
        self.assertEqual(weights, [0])

    def test_missing_signal_fails(self):
        dd = {i * DAY: [i * DAY, 0, 0, 0, 100] for i in range(200)}
        del dd[10 * DAY]
        with self.assertRaises(ValueError):
            signals(dd, [200 * DAY + HOUR], 200)

    def test_duplicate_and_bad_ohlc(self):
        r = [0, 100, 110, 90, 105, 1, DAY - 1, 100, 1, 1, 100]
        with self.assertRaises(ValueError):
            validate_rows([r, r], DAY, 0, 0)
        bad = r.copy()
        bad[3] = 120
        self.assertEqual(len(validate_rows([bad], DAY, 0, 0)["malformed"]), 1)

    def test_shortened_source_bar_not_future_or_missing(self):
        r = [0, 100, 110, 90, 105, 1, 2000, 100, 1, 1, 100]
        result = validate_rows([r], DAY, 0, 0)
        self.assertEqual(len(result["shortened_bars"]), 1)
        self.assertEqual(result["malformed"], [])
        r[6] = DAY + 1
        self.assertEqual(len(validate_rows([r], DAY, 0, 0)["malformed"]), 1)


if __name__ == "__main__":
    unittest.main()
