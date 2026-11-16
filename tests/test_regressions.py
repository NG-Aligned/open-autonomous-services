"""Regression tests for defects found in self-review. Each failed before its fix (reproduced first)."""
import os
import time
import unittest

from helpers import FakeClock, make_orch, manifest

from oas import Claim, ClaimError, Orchestrator, Store, StoreError, compute_claim_id, export_bundle

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def node():
    s = Store(":memory:", clock=FakeClock())
    return Orchestrator(s, sleep=lambda x: None), s


def forged(store, type, content, service, supersedes, t=5.0):
    cid = compute_claim_id(type, content, service, [], 1.0, t, supersedes)
    return store.sign_claim(Claim(cid, type, content, service, (), 1.0, t, supersedes, store.node_id))


class SupersessionHijackTests(unittest.TestCase):
    """A peer must not be able to hide someone else's claim by naming it in `supersedes`."""

    def setUp(self):
        self.ov, self.sv = node()
        self.oa, self.sa = node()
        self.oc, self.sc = node()
        for s in (self.sv, self.sa, self.sc):
            self.addCleanup(s.close)
        self.ov.register(manifest("victim_svc", writes=["report"]), lambda ctx: ctx.write("report", {"t": "real"}))
        self.ov.run_service("victim_svc")
        self.victim = self.sv.query_claims(["report"])[0]
        self.sa.insert_claim(forged(self.sa, "report", {"t": "FAKE"}, "attacker_svc", self.victim.id))

    def contents(self, store):
        return sorted(c.content["t"] for c in store.query_claims(["report"]))

    def test_superseder_arriving_before_target_does_not_hide_it(self):
        self.oc.import_bundle(export_bundle(self.sa))
        self.oc.import_bundle(export_bundle(self.sv))
        self.assertEqual(self.contents(self.sc), ["FAKE", "real"])
        self.assertFalse(self.sc.is_superseded(self.victim.id))

    def test_same_result_when_target_arrives_first(self):
        self.oc.import_bundle(export_bundle(self.sv))
        res = self.oc.import_bundle(export_bundle(self.sa))
        self.assertEqual(res["imported"], 0)                             # rejected outright: owner mismatch
        self.assertEqual(self.contents(self.sc), ["real"])

    def test_legitimate_same_owner_supersession_still_works_for_imports(self):
        def v2(ctx):
            ctx.write("report", {"t": "real-v2"}, supersedes=self.victim.id)
        self.ov.register(manifest("victim_svc", writes=["report"]), v2)
        self.ov.run_service("victim_svc")
        self.oc.import_bundle(export_bundle(self.sv))
        self.assertEqual(self.contents(self.sc), ["real-v2"])

    def test_cross_type_supersession_ignored_and_rejected_on_import_when_target_known(self):
        self.sa.close()
        sa = Store(":memory:", clock=FakeClock())
        self.addCleanup(sa.close)
        base = forged(sa, "kind_a", {"t": "a"}, "svc", None, t=1.0)
        wrong = forged(sa, "kind_b", {"t": "b"}, "svc", base.id, t=2.0)       # same owner, different type
        sa.insert_claim(base)
        sa.insert_claim(wrong)
        self.assertFalse(sa.is_superseded(base.id))
        self.assertEqual(len(sa.query_claims(["kind_a"])), 1)
        oc, sc = node()
        self.addCleanup(sc.close)
        bundle = export_bundle(sa)
        res = oc.import_bundle(bundle)
        self.assertEqual(res["imported"], 1)
        self.assertIn("same type", res["rejected"][0]["reason"])

    def test_lineage_ignores_an_invalid_supersession_edge_and_prune_leaves_the_victim(self):
        self.oc.import_bundle(export_bundle(self.sa))
        self.oc.import_bundle(export_bundle(self.sv))
        fake = next(c for c in self.sc.query_claims(["report"]) if c.content["t"] == "FAKE")
        lin = self.sc.lineage(fake.id)
        self.assertEqual([a["id"] for a in lin["ancestors"]], [fake.id])      # victim is not an ancestor of the fake
        self.sc.clock = lambda: 1e9
        self.assertEqual(self.sc.prune_superseded_claims(0), 0)                # nothing legitimately superseded
        self.assertEqual(self.contents(self.sc), ["FAKE", "real"])


class ChattyChildTests(unittest.TestCase):
    def test_child_printing_megabytes_does_not_deadlock(self):
        orch, store, _, _ = make_orch()
        self.addCleanup(store.close)
        orch.register(manifest("chatty", writes=["out"], entrypoint="chatty_service:run", isolation="process",
                               timeout_seconds=20), base_dir=TESTS_DIR)
        t0 = time.monotonic()
        res = orch.run_service("chatty")
        self.assertEqual(res.status, "success", msg=res.error)
        self.assertLess(time.monotonic() - t0, 10)


class NodeIdTests(unittest.TestCase):
    def test_unsafe_node_ids_rejected_because_they_become_filenames(self):
        for bad in ("../evil", "a/b", ".hidden", "x" * 65, "sp ace"):
            with self.assertRaises(StoreError, msg=bad):
                Store(":memory:", node_id=bad)
        Store(":memory:", node_id="node-1_ok.v2").close()


if __name__ == "__main__":
    unittest.main()


class HostileBundleTests(unittest.TestCase):
    def test_one_pathological_file_does_not_abort_a_directory_sync(self):
        import tempfile
        from oas.transport import sync_dir
        oa, sa = node()
        ob, sb = node()
        self.addCleanup(sa.close)
        self.addCleanup(sb.close)
        oa.register(manifest("p", writes=["t"]), lambda ctx: ctx.write("t", {"ok": 1}))
        oa.run_service("p")
        with tempfile.TemporaryDirectory() as d:
            sync_dir(oa, d)
            with open(os.path.join(d, "aaa.bundle.json"), "w") as f:       # sorts BEFORE the good bundle
                f.write('{"format":"oas-bundle/1","claims":[{"id":"a","content":' + "[" * 100000 + "]" * 100000 + "}]}")
            res = sync_dir(ob, d)["imported"]
            self.assertIn("error", res["aaa.bundle.json"])
            self.assertEqual(res[f"{sa.node_id}.bundle.json"]["imported"], 1)
        self.assertEqual(len(sb.query_claims(["t"])), 1)

    def test_deeply_nested_claim_content_is_rejected_not_fatal(self):
        oa, sa = node()
        ob, sb = node()
        self.addCleanup(sa.close)
        self.addCleanup(sb.close)
        oa.register(manifest("p", writes=["t"]), lambda ctx: ctx.write("t", {"ok": 1}))
        oa.run_service("p")
        good = export_bundle(sa)
        deep = cur = []
        for _ in range(5000):
            nxt = []
            cur.append(nxt)
            cur = nxt
        bundle = {**good, "claims": [{**good["claims"][0], "content": deep}, *good["claims"]]}
        res = ob.import_bundle(bundle)
        self.assertEqual((res["imported"], len(res["rejected"])), (1, 1))


class QueryPlanGuardTests(unittest.TestCase):
    def test_current_view_uses_the_supersedes_index_not_a_per_row_type_scan(self):
        """Without INDEXED BY the planner picked idx_claims_type_seq inside the correlated subquery, making
        query_claims ~100x slower (quadratic) on 21k claims. A timing test would be flaky; the plan is not."""
        import inspect
        src = inspect.getsource(Store.query_claims)
        self.assertIn("INDEXED BY idx_claims_supersedes", src)
        s = Store(":memory:", clock=FakeClock())
        self.addCleanup(s.close)
        plan = " ".join(str(tuple(r)) for r in s._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM claims WHERE NOT EXISTS (SELECT 1 FROM claims AS c2 "
            "INDEXED BY idx_claims_supersedes WHERE c2.supersedes = claims.id AND c2.type = claims.type) AND type=?",
            ("x",)))
        self.assertIn("idx_claims_supersedes", plan)
