import unittest
from datetime import datetime, timezone

from helpers import make_orch, manifest

from oas import ManifestError
from oas import cron


def ts(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def fmt(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M %a")


class CronParseTests(unittest.TestCase):
    def test_next_fire_examples(self):
        base = ts(2026, 11, 16, 11, 12, 30)                                   # Mon 2026-11-16 11:12:30
        cases = {
            "* * * * *": "2026-11-16 11:13 Mon",
            "*/15 * * * *": "2026-11-16 11:15 Mon",
            "0 9 * * *": "2026-11-17 09:00 Tue",
            "30 11 * * *": "2026-11-16 11:30 Mon",
            "0 0 1 * *": "2026-12-01 00:00 Tue",
            "0 8 * * 1-5": "2026-11-17 08:00 Tue",
            "0 8 * * 0": "2026-11-22 08:00 Sun",
            "0 8 * * 7": "2026-11-22 08:00 Sun",                              # 7 == Sunday
            "5,10 12 * * *": "2026-11-16 12:05 Mon",
            "10-20/5 11 * * *": "2026-11-16 11:15 Mon",
            "0 0 29 2 *": "2028-02-29 00:00 Tue",                             # leap day, skips years
            "0 0 13 * 5": "2026-11-20 00:00 Fri",                             # dom OR dow: next Friday beats the 13th
            "0 0 31 * *": "2026-12-31 00:00 Thu",                             # skips 30-day months
        }
        for expr, expected in cases.items():
            self.assertEqual(fmt(cron.next_fire(expr, base)), expected, msg=expr)

    def test_strictly_after_and_year_rollover(self):
        t = ts(2026, 12, 31, 23, 59, 0)
        self.assertEqual(fmt(cron.next_fire("* * * * *", t)), "2027-01-01 00:00 Fri")
        self.assertEqual(fmt(cron.next_fire("0 12 * * *", ts(2026, 11, 16, 12, 0, 0))), "2026-11-17 12:00 Tue")

    def test_invalid_expressions(self):
        for bad in ("", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8",
                    "a * * * *", "*/0 * * * *", "5-1 * * * *", "1- * * * *", "0 0 31 2 *", None, 5):
            with self.assertRaises(ValueError, msg=str(bad)):
                cron.next_fire(bad, 0.0)


class CronTriggerTests(unittest.TestCase):
    def setUp(self):
        self.orch, self.store, self.clock, _ = make_orch()
        self.addCleanup(self.store.close)
        self.clock.t = ts(2026, 11, 16, 8, 59, 30)

    def ids(self):
        return [r.service_id for r in self.orch.tick().runs]

    def test_manifest_validation(self):
        with self.assertRaises(ManifestError):
            self.orch.register(manifest("x", triggers=[{"kind": "cron", "expr": "nope"}]), lambda c: None)
        with self.assertRaises(ManifestError):
            self.orch.register(manifest("y", triggers=[{"kind": "cron"}]), lambda c: None)

    def test_daily_job_fires_at_the_scheduled_minute_once(self):
        seen = []
        self.orch.register(manifest("daily", triggers=[{"kind": "cron", "expr": "0 9 * * *"}]),
                           lambda ctx: seen.append(ctx.trigger[0]["scheduled_for"]))
        self.assertEqual(self.ids(), [])                       # registered at 08:59:30; not due until 09:00
        self.clock.advance(29)
        self.assertEqual(self.ids(), [])                       # 08:59:59
        self.clock.advance(1)
        self.assertEqual(self.ids(), ["daily"])                # 09:00:00
        self.assertEqual(self.ids(), [])                       # same minute: not again
        self.clock.advance(3600)
        self.assertEqual(self.ids(), [])                       # 10:00
        self.clock.advance(23 * 3600)                          # next day 09:00
        self.assertEqual(self.ids(), ["daily"])
        self.assertEqual([fmt(t) for t in seen], ["2026-11-16 09:00 Mon", "2026-11-17 09:00 Tue"])

    def test_missed_firings_coalesce_into_one_run(self):
        runs = []
        self.orch.register(manifest("hourly", triggers=[{"kind": "cron", "expr": "0 * * * *"}]),
                           lambda ctx: runs.append(1))
        self.clock.advance(5 * 3600)                           # five hourly slots missed (downtime)
        self.assertEqual(self.ids(), ["hourly"])
        self.assertEqual(self.ids(), [])
        self.assertEqual(len(runs), 1)

    def test_failed_cron_slot_is_consumed_no_hot_retry_and_fires_again_next_slot(self):
        calls = {"n": 0}

        def fn(ctx):
            calls["n"] += 1
            raise RuntimeError("x")
        self.orch.register(manifest("flaky", circuit_breaker=10, triggers=[{"kind": "cron", "expr": "0 9 * * *"}]), fn)
        self.clock.advance(30)                                  # 09:00:00
        self.assertEqual(self.ids(), ["flaky"])
        self.assertEqual(calls["n"], 1)
        for _ in range(3):
            self.assertEqual(self.ids(), [])                    # failed slot is consumed: no retry every tick
        self.assertEqual(calls["n"], 1)
        self.clock.advance(24 * 3600)
        self.assertEqual(self.ids(), ["flaky"])                 # next slot fires normally
        self.assertEqual(calls["n"], 2)

    def test_cron_cursor_is_persisted_as_the_next_slot_and_mixes_with_interval(self):
        self.orch.register(manifest("mix", triggers=[{"kind": "cron", "expr": "0 9 * * *"},
                                                     {"kind": "interval", "seconds": 86400}]), lambda c: None)
        self.assertEqual(self.ids(), ["mix"])                   # interval fires immediately on the first tick
        self.assertNotIn("cron:0 9 * * *", self.store.get_service("mix")["cursors"])   # cron slot not yet consumed
        self.clock.advance(30)                                  # 09:00:00 -> cron slot is now due
        self.assertEqual(self.ids(), ["mix"])
        self.assertEqual(fmt(self.store.get_service("mix")["cursors"]["cron:0 9 * * *"]), "2026-11-17 09:00 Tue")


if __name__ == "__main__":
    unittest.main()
