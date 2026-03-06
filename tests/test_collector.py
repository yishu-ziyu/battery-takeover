from __future__ import annotations

import unittest

from battery_takeover.collector import _parse_pmset, _parse_system_profiler


class CollectorParseTests(unittest.TestCase):
    def test_parse_pmset_ac_charging(self) -> None:
        raw = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=22413411)\t71%; charging; 1:42 remaining present: true
"""
        on_ac, percent, charging, remaining = _parse_pmset(raw)
        self.assertTrue(on_ac)
        self.assertEqual(percent, 71)
        self.assertTrue(charging)
        self.assertEqual(remaining, 102)

    def test_parse_pmset_battery(self) -> None:
        raw = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=22413411)\t60%; discharging; 2:00 remaining present: true
"""
        on_ac, percent, charging, remaining = _parse_pmset(raw)
        self.assertFalse(on_ac)
        self.assertEqual(percent, 60)
        self.assertFalse(charging)
        self.assertEqual(remaining, 120)

    def test_parse_system_profiler(self) -> None:
        raw = """
Health Information:
    Cycle Count: 113
    Maximum Capacity: 99%
"""
        cycle_count, max_capacity = _parse_system_profiler(raw)
        self.assertEqual(cycle_count, 113)
        self.assertEqual(max_capacity, 99)


if __name__ == "__main__":
    unittest.main()
