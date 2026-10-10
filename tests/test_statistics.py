"""Tests for the pure helpers in statistics.py (Home Assistant is stubbed)."""
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "neovac"


def _load():
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    any_ = lambda *a, **k: None
    for n in ("homeassistant", "homeassistant.components", "homeassistant.components.recorder",
              "homeassistant.components.recorder.models", "homeassistant.components.recorder.statistics",
              "homeassistant.core", "homeassistant.util"):
        mod(n)
    sys.modules["homeassistant.components.recorder"].get_instance = any_
    sys.modules["homeassistant.components.recorder.models"].StatisticData = dict
    sys.modules["homeassistant.components.recorder.models"].StatisticMetaData = dict
    sys.modules["homeassistant.components.recorder.statistics"].async_add_external_statistics = any_
    sys.modules["homeassistant.components.recorder.statistics"].get_last_statistics = any_
    sys.modules["homeassistant.core"].HomeAssistant = object
    mod("homeassistant.util.dt")
    sys.modules["homeassistant.util"].dt = sys.modules["homeassistant.util.dt"]
    pkg = mod("nv_pkg")
    pkg.__path__ = [str(PKG)]
    mod("nv_pkg.api", NeoVacApiClient=object)
    spec = importlib.util.spec_from_file_location("nv_pkg.const", PKG / "const.py")
    const = importlib.util.module_from_spec(spec)
    sys.modules["nv_pkg.const"] = const
    spec.loader.exec_module(const)
    spec = importlib.util.spec_from_file_location("nv_pkg.statistics", PKG / "statistics.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["nv_pkg.statistics"] = m
    spec.loader.exec_module(m)
    return m


S = _load()
Q = timedelta(minutes=15)


def quarters(start, values):
    return {start + i * Q: v for i, v in enumerate(values)}


class StatisticsHelpers(unittest.TestCase):
    def test_parse_skips_none(self):
        out = S.parse_values([
            {"date": "2026-10-10T09:00:00", "value": 1.5},
            {"date": "2026-10-10T09:15:00", "value": None},
        ])
        self.assertEqual(out, {datetime(2026, 10, 10, 9): 1.5})

    def test_grid_and_solar(self):
        t = datetime(2026, 10, 10, 9)
        grid, solar = S.grid_and_solar({t: 1.0, t + Q: 0.5}, {t: 0.25, t + Q: 0.75})
        self.assertEqual(grid, {t: 0.75, t + Q: 0.0})
        self.assertEqual(solar, {t: 0.25, t + Q: 0.75})

    def test_only_complete_hours(self):
        start = datetime(2026, 10, 10, 8)
        # 08:00 .. 09:30 -> 7 quarters: hour 8 is complete, hour 9 is not
        hours = S.complete_hours(quarters(start, [1] * 7), Q)
        self.assertEqual(hours, {start: 4.0})

    def test_hour_completes_with_last_quarter(self):
        start = datetime(2026, 10, 10, 8)
        hours = S.complete_hours(quarters(start, [1] * 8), Q)
        self.assertEqual(set(hours), {start, start.replace(hour=9)})

    def test_hourly_data(self):
        t = datetime(2026, 10, 10, 8)
        hours = S.complete_hours({t: 2.0, t.replace(hour=9): 3.0}, timedelta(hours=1))
        self.assertEqual(hours, {t: 2.0, t.replace(hour=9): 3.0})

    def test_cumulative_continues_after_last(self):
        h8, h9, h10 = (datetime(2026, 10, 10, h) for h in (8, 9, 10))
        rows = S.cumulative({h8: 1.0, h9: 2.0, h10: 4.0}, after=h8, start_sum=100.0)
        self.assertEqual(rows, [(h9, 102.0), (h10, 106.0)])

    def test_cumulative_without_history(self):
        h8 = datetime(2026, 10, 10, 8)
        self.assertEqual(S.cumulative({h8: 1.5}, None, 0.0), [(h8, 1.5)])


if __name__ == "__main__":
    unittest.main()
