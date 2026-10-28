"""The end-of-run check: no test attempted a real outbound request.

Its name sorts last, so in a full `python -m unittest discover -s tests` run it executes after every other test.
A real attempt already raises `RealNetworkAttempted` where it happens, but code with a bare `except:` or a
background thread could hide that; every attempt is also recorded, and this test fails if there is one.
(The guard's own tests make attempts on purpose and remove them with `expected_attempts()`.)"""

import unittest

from tests import network_guard


class NoRealRequestWasAttempted(unittest.TestCase):
    def test_no_real_request_was_attempted_during_the_run(self):
        self.assertEqual(network_guard.ATTEMPTS, [], "a test tried a real outbound request (method host)")


if __name__ == "__main__":
    unittest.main()
