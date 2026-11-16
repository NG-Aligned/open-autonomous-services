import os
import tempfile
import unittest

from helpers import FakeClock, make_orch, manifest

from oas import Orchestrator, Store, StoreError
from test_cli import oas as run_cli


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.orch, self.store, self.clock, _ = make_orch()
        self.addCleanup(self.store.close)

    def test_prune_runs_keeps_newest_n_and_latest_success_per_service(self):
        mode = {"fail": False}

        def fn(ctx):
            if mode["fail"]:
                raise ValueError("x")
        self.orch.register(manifest("a", circuit_breaker=100), fn)
        self.orch.register(manifest("b"), lambda ctx: None)
        for _ in range(3):
            self.orch.run_service("a")
        mode["fail"] = True
        for _ in range(5):
            self.orch.run_service("a")                      # 5 failures after the last success
        for _ in range(4):
            self.orch.run_service("b")
        latest_success_a = max(r["seq"] for r in self.store.list_runs("a") if r["status"] == "success")
        deleted = self.store.prune_runs(2)
        a = self.store.list_runs("a")
        self.assertEqual(len(a), 3)                         # newest 2 failures + the latest success
        self.assertIn(latest_success_a, [r["seq"] for r in a])
        self.assertEqual(len(self.store.list_runs("b")), 2)
        self.assertEqual(deleted, (8 - 3) + (4 - 2))
        with self.assertRaises(StoreError):
            self.store.prune_runs(0)

    def test_dependency_trigger_still_works_after_pruning_runs(self):
        self.orch.register(manifest("up"), lambda ctx: None)
        self.orch.register(manifest("down", triggers=[{"kind": "dependency", "service": "up"}]), lambda ctx: None)
        for _ in range(4):
            self.orch.run_service("up")
            self.clock.advance(1)
        self.store.prune_runs(1)
        self.assertEqual([r.service_id for r in self.orch.tick().runs], ["down"])
        self.assertEqual(self.orch.tick().runs, [])

    def test_prune_audit_and_messages_respect_age_and_consumption(self):
        self.orch.register(manifest("rx", triggers=[{"kind": "message"}]), lambda ctx: ctx.inbox())
        self.orch.register(manifest("tx", can_message=["rx"]),
                           lambda ctx: (ctx.send("rx", "t", 1), ctx.send("rx", "t", 2)))
        self.orch.run_service("tx")
        self.orch.run_service("rx")                          # consumes both
        self.orch.run_service("tx")                          # two new, unconsumed
        self.clock.advance(1000)
        self.assertEqual(self.store.prune_messages(500), 2)  # only the consumed ones
        self.assertEqual(len(self.store.pending_messages("rx")), 2)
        before = len(self.store.list_audit())
        self.store.audit("x", "late", "", True)
        self.assertEqual(self.store.prune_audit(500), before)
        self.assertEqual([a["action"] for a in self.store.list_audit()], ["late"])

    def test_prune_superseded_claims_keeps_current_and_lineage_reports_missing(self):
        def v(ctx):
            prev = ctx.state.get("prev")
            ctx.state["prev"] = ctx.write("t", {"n": ctx.state.get("n", 0)}, supersedes=prev)
            ctx.state["n"] = ctx.state.get("n", 0) + 1
        self.orch.register(manifest("s", writes=["t"]), v)
        for _ in range(4):
            self.orch.run_service("s")
            self.clock.advance(100)
        current = self.store.query_claims(["t"])[0]
        self.assertEqual(len(self.store.query_claims(["t"], current_only=False)), 4)
        # now = t0+400, claims created at t0, +100, +200 (superseded) and +300 (current); cutoff = t0+150
        self.assertEqual(self.store.prune_superseded_claims(250), 2)       # only the two old enough
        remaining = self.store.query_claims(["t"], current_only=False)
        self.assertEqual(len(remaining), 2)
        self.assertEqual([c.id for c in self.store.query_claims(["t"])], [current.id])
        self.assertEqual(len(self.store.lineage(current.id)["missing"]), 1)
        self.assertEqual(self.store.prune_superseded_claims(0), 1)         # never deletes the current claim
        self.assertEqual([c.id for c in self.store.query_claims(["t"])], [current.id])

    def test_signing_can_be_disabled_for_throughput(self):
        s2 = Store(":memory:", clock=FakeClock(), sign=False)
        self.addCleanup(s2.close)
        o2 = Orchestrator(s2)
        o2.register(manifest("w", writes=["t"]), lambda ctx: ctx.write("t", 1))
        o2.run_service("w")
        c = s2.query_claims()[0]
        self.assertEqual((c.signer, c.signature, s2.verify_claim(c.id)), (None, None, "unsigned"))

    def test_prune_cli(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "x.db")
            from helpers import EXAMPLES
            run_cli(db, "discover", EXAMPLES)
            run_cli(db, "tick")
            import json
            out = json.loads(run_cli(db, "prune", "--keep-runs", "1", "--audit-older-than", "0",
                                     "--messages-older-than", "0", "--superseded-older-than", "0").stdout)
            self.assertEqual(set(out), {"runs_deleted", "audit_deleted", "messages_deleted",
                                        "superseded_claims_deleted"})
            self.assertGreaterEqual(out["audit_deleted"], 3)
            self.assertEqual(len(json.loads(run_cli(db, "claims", "--type", "report").stdout)), 1)


if __name__ == "__main__":
    unittest.main()
