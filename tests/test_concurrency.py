"""Execution leases: multiple orchestrators sharing one database must not double-run a service."""
import os
import sqlite3
import tempfile
import threading
import time
import unittest

from helpers import FakeClock, make_orch, manifest

from oas import Orchestrator, Store

INTERVAL = [{"kind": "interval", "seconds": 3600}]


class LeaseTests(unittest.TestCase):
    def test_two_orchestrators_ticking_concurrently_run_the_service_once(self):
        for trial in range(5):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "race.db")
                s = Store(path)
                Orchestrator(s).register(manifest(writes=["t"], triggers=INTERVAL), lambda ctx: None)
                s.close()
                results = []

                def worker(i):
                    st = Store(path)
                    orch = Orchestrator(st)

                    def fn(ctx):
                        time.sleep(0.15)           # widen the race window
                        ctx.write("t", {"by": i})
                    orch.bind("svc", fn)
                    results.extend(r.status for r in orch.tick().runs)
                    st.close()
                threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
                [t.start() for t in threads]
                [t.join() for t in threads]
                st = Store(path)
                try:
                    self.assertEqual(results.count("success"), 1, msg=f"trial {trial}: {results}")
                    self.assertEqual(len(st.query_claims(["t"])), 1, msg=f"trial {trial}")
                    self.assertEqual([r["status"] for r in st.list_runs("svc")], ["success"])
                finally:
                    st.close()

    def test_held_lease_skips_then_expires(self):
        orch, store, clock, _ = make_orch()
        self.addCleanup(store.close)
        ran = []
        orch.register(manifest(), lambda ctx: ran.append(1))
        self.assertTrue(store.acquire_lease("svc", "other-orchestrator", 100))
        res = orch.run_service("svc")
        self.assertEqual((res.status, ran), ("skipped", []))
        self.assertIn("leased", res.error)
        clock.advance(101)
        self.assertEqual(orch.run_service("svc").status, "success")
        self.assertEqual(ran, [1])

    def test_lease_released_after_success_and_after_failure(self):
        orch, store, _, _ = make_orch()
        self.addCleanup(store.close)
        mode = {"fail": True}

        def fn(ctx):
            if mode["fail"]:
                raise ValueError("x")
        orch.register(manifest(circuit_breaker=10), fn)
        self.assertEqual(orch.run_service("svc").status, "failed")
        mode["fail"] = False
        self.assertEqual(orch.run_service("svc").status, "success")   # would be "skipped" if the lease leaked
        self.assertEqual(orch.run_service("svc").status, "success")

    def test_recheck_skips_service_that_stopped_being_due(self):
        orch, store, clock, _ = make_orch()
        self.addCleanup(store.close)
        orch.register(manifest(triggers=INTERVAL), lambda ctx: None)
        reasons, updates = orch.due_reasons("svc")
        self.assertTrue(reasons)
        orch.run_service("svc", reasons=reasons, cursor_updates=updates)       # "another orchestrator" got there first
        res = orch.run_service("svc", reasons=reasons, cursor_updates=updates, recheck=True)
        self.assertEqual((res.status, res.error), ("skipped", "no longer due"))

    def test_database_from_before_leases_is_upgraded(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE services(id TEXT PRIMARY KEY, manifest TEXT NOT NULL, base_dir TEXT,"
                         " enabled INTEGER NOT NULL DEFAULT 1, consecutive_failures INTEGER NOT NULL DEFAULT 0,"
                         " disabled_reason TEXT, next_due REAL NOT NULL DEFAULT 0,"
                         " cursors TEXT NOT NULL DEFAULT '{}', registered_at REAL NOT NULL)")
            conn.commit()
            conn.close()
            store = Store(path, clock=FakeClock())
            self.addCleanup(store.close)
            Orchestrator(store).register(manifest(), lambda ctx: None)
            self.assertTrue(store.acquire_lease("svc", "me", 10))
            self.assertFalse(store.acquire_lease("svc", "you", 10))


if __name__ == "__main__":
    unittest.main()
