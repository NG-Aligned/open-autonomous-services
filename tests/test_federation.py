import copy
import unittest

from helpers import FakeClock, make_orch, manifest

from oas import ClaimError, Orchestrator, Store, export_bundle, import_bundle


def node():
    clock = FakeClock()
    store = Store(":memory:", clock=clock)
    return Orchestrator(store, sleep=lambda s: None), store, clock


class FederationTests(unittest.TestCase):
    def setUp(self):
        self.oa, self.sa, self.ca = node()
        self.ob, self.sb, self.cb = node()
        self.addCleanup(self.sa.close)
        self.addCleanup(self.sb.close)

        def produce(ctx):
            p = ctx.write("paper", {"id": "p1"})
            ctx.write("assessment", {"ok": True}, inputs=[p], confidence=0.8)
        self.oa.register(manifest("producer", writes=["paper", "assessment"], reads=["paper"]), produce)
        self.oa.run_service("producer")

    def test_roundtrip_preserves_ids_origin_and_provenance(self):
        bundle = export_bundle(self.sa)
        res = import_bundle(self.sb, bundle)
        self.assertEqual((res["imported"], res["duplicates"], res["rejected"]), (2, 0, []))
        ids_a = {c.id for c in self.sa.query_claims(current_only=False)}
        ids_b = {c.id for c in self.sb.query_claims(current_only=False)}
        self.assertEqual(ids_a, ids_b)
        for c in self.sb.query_claims():
            self.assertEqual(c.origin, self.sa.node_id)
        assessment = self.sb.query_claims(["assessment"])[0]
        lin = self.sb.lineage(assessment.id)
        self.assertEqual({a["type"] for a in lin["ancestors"]}, {"assessment", "paper"})
        self.assertEqual(lin["missing"], [])

    def test_import_is_idempotent(self):
        bundle = export_bundle(self.sa)
        import_bundle(self.sb, bundle)
        again = import_bundle(self.sb, bundle)
        self.assertEqual((again["imported"], again["duplicates"]), (0, 2))
        self.assertEqual(len(self.sb.query_claims(current_only=False)), 2)

    def test_tampered_claim_rejected_others_accepted(self):
        bundle = export_bundle(self.sa)
        bad = copy.deepcopy(bundle)
        for w in bad["claims"]:
            if w["type"] == "assessment":
                w["content"] = {"ok": False}
        res = import_bundle(self.sb, bad)
        self.assertEqual(res["imported"], 1)
        self.assertEqual(len(res["rejected"]), 1)
        self.assertIn("tampered", res["rejected"][0]["reason"])

    def test_partial_import_flags_missing_ancestors(self):
        bundle = export_bundle(self.sa, types=["assessment"])
        import_bundle(self.sb, bundle)
        aid = self.sb.query_claims(["assessment"])[0].id
        self.assertEqual(len(self.sb.lineage(aid)["missing"]), 1)

    def test_forged_local_origin_rejected(self):
        bundle = export_bundle(self.sa)
        forged = copy.deepcopy(bundle)
        for w in forged["claims"]:
            w["origin"] = self.sb.node_id
        res = import_bundle(self.sb, forged)
        self.assertEqual(res["imported"], 0)
        self.assertEqual(len(res["rejected"]), 2)

    def test_foreign_claim_cannot_supersede_local_claim(self):
        # node B has its own claim; a foreign bundle tries to supersede it
        def local(ctx):
            ctx.state["id"] = ctx.write("paper", {"id": "mine"})
        self.ob.register(manifest("localsvc", writes=["paper"]), local)
        self.ob.run_service("localsvc")
        mine = self.sb.get_state("localsvc")["id"]
        from oas.store import compute_claim_id
        cid = compute_claim_id("paper", {"id": "evil"}, "attacker", [], 1.0, 5.0, mine)
        wire = {"id": cid, "type": "paper", "content": {"id": "evil"}, "source_service": "attacker",
                "inputs": [], "confidence": 1.0, "created_at": 5.0, "supersedes": mine, "origin": "evilnode"}
        res = import_bundle(self.sb, {"format": "oas-bundle/1", "node_id": "evilnode", "claims": [wire]})
        self.assertEqual(res["imported"], 0)
        self.assertIn("different service/origin", res["rejected"][0]["reason"])
        self.assertEqual([c.id for c in self.sb.query_claims(["paper"]) if c.content == {"id": "mine"}], [mine])

    def test_malformed_claims_and_bundles_rejected(self):
        with self.assertRaises(ClaimError):
            import_bundle(self.sb, {"format": "wrong"})
        with self.assertRaises(ClaimError):
            import_bundle(self.sb, {"format": "oas-bundle/1", "claims": "nope"})
        res = import_bundle(self.sb, {"format": "oas-bundle/1", "node_id": "x", "claims": [{"id": "a"}, 5]})
        self.assertEqual(len(res["rejected"]), 2)

    def test_imported_claims_trigger_local_event_services_without_any_central_party(self):
        ran = []
        self.ob.register(manifest("consumer", reads=["paper"], triggers=[{"kind": "event", "claim_type": "paper"}]),
                         lambda ctx: ran.append([c.id for c in ctx.read("paper")]))
        self.assertEqual(self.ob.tick().runs, [])
        import_bundle(self.sb, export_bundle(self.sa))
        self.assertEqual([r.service_id for r in self.ob.tick().runs], ["consumer"])
        self.assertEqual(len(ran[0]), 1)

    def test_incremental_export_since_seq(self):
        first = export_bundle(self.sa)
        cut = max(c.seq for c in self.sa.query_claims(current_only=False))
        self.ca.advance(1)
        self.oa.register(manifest("later", writes=["paper"]), lambda ctx: ctx.write("paper", {"id": "p2"}))
        self.oa.run_service("later")
        delta = export_bundle(self.sa, since_seq=cut)
        self.assertEqual(len(first["claims"]), 2)
        self.assertEqual(len(delta["claims"]), 1)


if __name__ == "__main__":
    unittest.main()
