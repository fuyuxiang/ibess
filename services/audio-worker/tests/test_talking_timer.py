import unittest

from worker.talking_timer import TalkingTimer


class TalkingTimerTest(unittest.TestCase):
    def test_file_round_resets_duration_and_report_cooldown(self):
        timer = TalkingTimer(threshold=5, allowed=False)
        self.assertIsNone(timer.observe(0.0, True))
        self.assertEqual(timer.observe(5.1, True), 5.1)
        self.assertIsNone(timer.observe(6.0, True))
        timer.end_round()
        self.assertIsNone(timer.observe(6.1, True))
        self.assertEqual(timer.observe(11.2, True), 5.1)

    def test_silence_ends_current_talking_interval(self):
        timer = TalkingTimer(threshold=5, allowed=False)
        timer.observe(0.0, True)
        timer.observe(5.1, True)
        timer.observe(6.0, False)
        self.assertIsNone(timer.observe(6.1, True))


if __name__ == "__main__":
    unittest.main()
