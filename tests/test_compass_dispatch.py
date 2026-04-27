from __future__ import annotations

import unittest

from compass import app as compass_app


class CompassDispatchTests(unittest.TestCase):
    def test_should_launch_fresh_instance_for_per_task_agents(self):
        self.assertTrue(
            compass_app._should_launch_fresh_instance({"execution_mode": "per-task"})
        )
        self.assertFalse(
            compass_app._should_launch_fresh_instance({"execution_mode": "persistent"})
        )


if __name__ == "__main__":
    unittest.main()