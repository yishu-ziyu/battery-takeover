from __future__ import annotations

import unittest
from unittest.mock import patch

from battery_takeover.collector import CollectorError, _parse_pmset, _parse_system_profiler, collect_sample


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

    def test_collect_sample_keeps_pmset_sample_when_system_profiler_times_out(self) -> None:
        pmset_raw = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=22413411)\t71%; charging; 1:42 remaining present: true
"""

        def fake_run(cmd: list[str], timeout_sec: int) -> str:
            if cmd[0] == "pmset":
                return pmset_raw
            raise CollectorError("command failed: system_profiler SPPowerDataType (timed out)")

        with patch("battery_takeover.collector._run", side_effect=fake_run):
            sample = collect_sample(timeout_sec=8)

        self.assertEqual(sample.percent, 71)
        self.assertTrue(sample.on_ac)
        self.assertIsNone(sample.cycle_count)
        self.assertIsNone(sample.max_capacity_pct)
        self.assertIn("system_profiler_error", sample.source_raw)

    def test_collect_sample_redacts_hardware_identifiers_from_source_raw(self) -> None:
        pmset_raw = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=22413411)\t71%; charging; 1:42 remaining present: true
"""
        system_profiler_raw = """
Battery Information:
    Serial Number: BATTERY-SERIAL-123
    Cycle Count: 113
    Maximum Capacity: 99%
AC Charger Information:
    Serial Number: CHARGER-SERIAL-456
"""

        def fake_run(cmd: list[str], timeout_sec: int) -> str:
            return pmset_raw if cmd[0] == "pmset" else system_profiler_raw

        with patch("battery_takeover.collector._run", side_effect=fake_run):
            sample = collect_sample(timeout_sec=8)

        self.assertNotIn("BATTERY-SERIAL-123", sample.source_raw)
        self.assertNotIn("CHARGER-SERIAL-456", sample.source_raw)
        self.assertNotIn("id=22413411", sample.source_raw)
        self.assertIn("Serial Number: <redacted>", sample.source_raw)
        self.assertIn("id=<redacted>", sample.source_raw)


if __name__ == "__main__":
    unittest.main()
