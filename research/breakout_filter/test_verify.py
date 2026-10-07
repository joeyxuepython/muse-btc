import unittest

from verify import compare


class VerificationTests(unittest.TestCase):
    def test_small_float_rounding_only(self):
        compare({"r": [0.01]}, {"r": [0.010000000001]})
        with self.assertRaises(ValueError):
            compare({"r": [0.01]}, {"r": [0.02]})

    def test_boolean_nan_and_structure_fail_closed(self):
        for expected, actual in [
            (True, 1),
            (1.0, float("nan")),
            (1.0, True),
            ({"a": 1}, {}),
            ([1], []),
        ]:
            with self.assertRaises(ValueError):
                compare(expected, actual)
