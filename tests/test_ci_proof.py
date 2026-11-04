import unittest


class Proof(unittest.TestCase):
    def test_fails_on_purpose(self):
        self.fail("Deliberate failure to prove ci-ok goes red. Removed before merge.")
