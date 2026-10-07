import copy
import tempfile
import unittest
from pathlib import Path

import baseline as b
import numpy as np
import run


class FilterTests(unittest.TestCase):
    def test_strict_threshold_and_no_late_entry(self):
        rows = [dict(close=c, sma=100, atr=2) for c in [101, 120, 99, 102, 100.1, 100]]
        weights, observations = run.choose(rows, 0.5)
        self.assertEqual(weights.tolist(), [0, 0, 0, 1, 1, 0])
        self.assertFalse(observations[0]["filter_pass_at_entry"])
        self.assertIsNone(observations[1]["filter_pass_at_entry"])

    def test_split_reset_and_boundary_disclosure(self):
        rows = [dict(close=c, sma=100, atr=2) for c in [101, 120]]
        self.assertEqual(run.choose(rows, 0.5)[0].tolist(), [0, 0])
        weights, obs = run.choose(rows[1:], 0.5)
        self.assertEqual(weights.tolist(), [1])
        self.assertTrue(obs[0]["boundary_entry"])

    def test_true_range_includes_previous_close(self):
        daily = {i * b.DAY: [i * b.DAY, 100, 102, 98, 100] for i in range(201)}
        daily[198 * b.DAY][4] = 110
        row = run.features(daily, [200 * b.DAY + b.HOUR])[0]
        self.assertAlmostEqual(row["atr"], (13 * 4 + 12) / 14)

    def test_future_and_current_day_do_not_change_features_or_decisions(self):
        daily = {i * b.DAY: [i * b.DAY, 100 + i, 102 + i, 98 + i, 100 + i] for i in range(220)}
        times = [i * b.DAY + b.HOUR for i in range(200, 210)]
        before = run.features(daily, times)
        changed = copy.deepcopy(daily)
        for i in range(209, 220):
            changed[i * b.DAY][1:5] = [1e8] * 4
        after = run.features(changed, times)
        self.assertEqual(before, after)
        np.testing.assert_equal(run.choose(before, 0.5)[0], run.choose(after, 0.5)[0])

    def test_missing_feature_bar_fails(self):
        daily = {i * b.DAY: [i * b.DAY, 100, 102, 98, 100] for i in range(200)}
        del daily[199 * b.DAY]
        with self.assertRaises(ValueError):
            run.features(daily, [200 * b.DAY + b.HOUR])

    def test_random_control_exact_count_and_reproducible(self):
        base = np.array([1, 1, 0, 1, 0, 1, 1, 0])
        selected = np.array([1, 1, 0, 0, 0, 0, 0, 0])
        prices = np.array([100.0, 101, 102, 100, 99, 103, 106, 101, 100])
        args = (prices, base, selected, 0.001, 0.0005, 20, 17)
        a, c = run.random_control(*args), run.random_control(*args)
        self.assertEqual(a, c)
        self.assertTrue(all(len(t["episode_ids"]) == 1 for t in a["trials"]))

    def test_zero_accepted_control_and_undefined_bootstrap(self):
        prices = np.array([100.0, 101, 99, 105])
        result = run.random_control(prices, np.ones(3), np.zeros(3), 0.001, 0.0005, 5, 17)
        self.assertEqual(result["p_ge_actual"], 1)
        stats, _, _ = run.bootstrap(np.zeros(3), np.array([0.01, -0.02, 0.04]), 17)
        self.assertIsNone(stats["ci95"])
        self.assertEqual(stats["undefined_draws"], 5000)

    def test_wrong_input_hash_fails_before_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            (source / "protocol.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "modified frozen input"):
                run.prepare_input(source, Path(temp) / "output", run.load_protocol())

    def test_protocol_is_frozen(self):
        self.assertEqual(run.load_protocol()["parameter_trials"], 1)


if __name__ == "__main__":
    unittest.main()
