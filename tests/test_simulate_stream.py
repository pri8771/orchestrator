"""simulate_stream.py's lock file must be per-app: two concurrent `--app` demo
runs must not delete each other's lock in the finally-block cleanup."""
import os
import unittest

import simulate_stream as sim


class TestLockPath(unittest.TestCase):
    def test_lock_path_is_per_app(self):
        a = sim.lock_path("taskquill")
        b = sim.lock_path("goeasy")
        self.assertNotEqual(a, b)
        self.assertIn("taskquill", a)
        self.assertIn("goeasy", b)

    def test_lock_path_lives_next_to_engine(self):
        p = sim.lock_path("demo")
        self.assertEqual(os.path.dirname(p), sim.HERE)
        self.assertTrue(os.path.basename(p).startswith(".simulate_stream."))

    def test_touch_lock_writes_the_given_path_only(self):
        a = sim.lock_path("app-a")
        b = sim.lock_path("app-b")
        try:
            sim.touch_lock(a)
            self.assertTrue(os.path.exists(a))
            self.assertFalse(os.path.exists(b))
        finally:
            for p in (a, b):
                if os.path.exists(p):
                    os.remove(p)


if __name__ == "__main__":
    unittest.main()
