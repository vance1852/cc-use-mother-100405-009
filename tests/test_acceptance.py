import unittest

from science_strategy_foundation.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])
        joint = result["joint"]
        self.assertEqual("ok", joint["status"])
        self.assertTrue(joint["duplicate_blocked"])
        self.assertTrue(joint["overflow_blocked"])
        self.assertTrue(joint["ready_now"])
        self.assertFalse(joint["ready_after_exit"])
        self.assertEqual(1, joint["exit_released"])
        self.assertEqual(3, joint["active_commitments"])
        self.assertEqual(1, joint["charter_version"])
        self.assertEqual(400, joint["expert_available"])


if __name__ == "__main__":
    unittest.main()
