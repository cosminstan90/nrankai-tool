"""Pasul 9 of docs/superpowers/plans/2026-09-30-next-steps.md."""
import unittest

from core.wilson import wilson_interval, intervals_overlap


class TestWilsonInterval(unittest.TestCase):
    def test_zero_of_three(self):
        low, high = wilson_interval(0, 3)
        self.assertAlmostEqual(low, 0.0, places=2)
        self.assertAlmostEqual(high, 56.15, places=1)

    def test_three_of_three(self):
        low, high = wilson_interval(3, 3)
        self.assertAlmostEqual(low, 43.85, places=1)
        self.assertAlmostEqual(high, 100.0, places=2)

    def test_five_of_ten(self):
        low, high = wilson_interval(5, 10)
        self.assertAlmostEqual(low, 23.66, places=1)
        self.assertAlmostEqual(high, 76.34, places=1)

    def test_zero_observations_is_no_interval(self):
        self.assertEqual(wilson_interval(0, 0), (None, None))

    def test_interval_always_stays_within_0_100(self):
        for successes, n in [(0, 1), (1, 1), (1, 2), (99, 100)]:
            low, high = wilson_interval(successes, n)
            self.assertGreaterEqual(low, 0.0)
            self.assertLessEqual(high, 100.0)

    def test_more_samples_narrows_the_interval_for_the_same_rate(self):
        """The whole point of sampling more: 1/3 is much less certain than 10/30."""
        low3, high3 = wilson_interval(1, 3)
        low30, high30 = wilson_interval(10, 30)
        self.assertLess(low3, low30)
        self.assertGreater(high3, high30)


class TestIntervalsOverlap(unittest.TestCase):
    def test_overlapping_intervals(self):
        self.assertTrue(intervals_overlap(10, 50, 40, 80))

    def test_non_overlapping_intervals_is_a_real_change(self):
        self.assertFalse(intervals_overlap(0, 20, 60, 100))

    def test_touching_boundaries_count_as_overlapping(self):
        self.assertTrue(intervals_overlap(0, 50, 50, 100))

    def test_missing_interval_on_either_side_is_unknown(self):
        self.assertIsNone(intervals_overlap(None, None, 40, 80))
        self.assertIsNone(intervals_overlap(10, 50, None, None))


if __name__ == "__main__":
    unittest.main()
