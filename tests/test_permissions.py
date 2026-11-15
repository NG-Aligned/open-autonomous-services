import unittest

from helpers import make_orch, manifest

from oas import ClaimError, MessageError


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.orch, self.store, self.clock, self.sleep = make_orch()
        self.addCleanup(self.store.close)

    def run_fn(self, fn, **mk):
        self.orch.register(manifest("actor", **mk), fn)
        return self.orch.run_service("actor")

    def seed(self, source="producer", type="data", writes=None):
        def produce(ctx):
            ctx._seeded = ctx.write(type, {"x": 1})
            ctx.state["id"] = ctx._seeded
        self.orch.register(manifest(source, writes=writes or [type]), produce)
        self.orch.run_service(source)
        return self.store.get_state(source)["id"]

    def test_write_undeclared_type_denied_and_audited(self):
        res = self.run_fn(lambda ctx: ctx.write("secret", 1), writes=["public"])
        self.assertEqual(res.status, "failed")
        self.assertIn("PermissionDenied", res.error)
        self.assertEqual(self.store.query_claims(), [])
        denied = [a for a in self.store.list_audit("actor") if not a["allowed"]]
        self.assertEqual(denied[0]["action"], "write")

    def test_read_undeclared_type_denied(self):
        self.seed()
        res = self.run_fn(lambda ctx: ctx.read("data"), reads=["other"])
        self.assertEqual(res.status, "failed")
        self.assertIn("PermissionDenied", res.error)

    def test_read_declared_and_wildcard(self):
        self.seed(type="paper.new", writes=["paper.*"])
        seen = []
        res = self.run_fn(lambda ctx: seen.extend(ctx.read("paper.new")), reads=["paper.*"])
        self.assertEqual(res.status, "success")
        self.assertEqual(len(seen), 1)

    def test_wildcard_in_claim_type_rejected(self):
        res = self.run_fn(lambda ctx: ctx.write("a.*", 1), writes=["a.*"])
        self.assertIn("ClaimError", res.error)

    def test_wildcard_write_permission_allows_matching_types(self):
        res = self.run_fn(lambda ctx: ctx.write("a.b", 1), writes=["a.*"])
        self.assertEqual(res.status, "success")

    def test_cannot_cite_unreadable_claim(self):
        cid = self.seed()
        res = self.run_fn(lambda ctx: ctx.write("out", 1, inputs=[cid]), writes=["out"], reads=["other"])
        self.assertIn("PermissionDenied", res.error)

    def test_can_cite_readable_claim(self):
        cid = self.seed()
        res = self.run_fn(lambda ctx: ctx.write("out", 1, inputs=[cid]), writes=["out"], reads=["data"])
        self.assertEqual(res.status, "success")
        out = self.store.query_claims(["out"])[0]
        self.assertEqual(out.inputs, (cid,))

    def test_citing_nonexistent_claim_fails(self):
        res = self.run_fn(lambda ctx: ctx.write("out", 1, inputs=["a" * 64]), writes=["out"])
        self.assertIn("ClaimError", res.error)

    def test_confidence_range_enforced(self):
        for bad in (-0.1, 1.5, True, "high"):
            res = self.run_fn(lambda ctx, b=bad: ctx.write("out", 1, confidence=b), writes=["out"])
            self.assertIn("ClaimError", res.error, msg=str(bad))

    def test_non_serialisable_content_fails(self):
        res = self.run_fn(lambda ctx: ctx.write("out", {"o": object()}), writes=["out"])
        self.assertIn("ClaimError", res.error)

    def test_cannot_supersede_another_services_claim(self):
        cid = self.seed(type="data")
        res = self.run_fn(lambda ctx: ctx.write("data", 2, supersedes=cid), writes=["data"], reads=["data"])
        self.assertIn("PermissionDenied", res.error)

    def test_cannot_supersede_different_type(self):
        def first(ctx):
            a = ctx.write("one", 1)
            ctx.write("two", 2, supersedes=a)
        res = self.run_fn(first, writes=["one", "two"])
        self.assertIn("ClaimError", res.error)

    def test_cannot_supersede_twice(self):
        def fn(ctx):
            first = ctx.write("t", 1)
            ctx.state["first"] = first
        self.orch.register(manifest("actor", writes=["t"]), fn)
        self.orch.run_service("actor")
        first = self.store.get_state("actor")["first"]

        def second(ctx):
            ctx.write("t", 2, supersedes=first)
            ctx.write("t", 3, supersedes=first)
        self.orch.register(manifest("actor", writes=["t"]), second)
        res = self.orch.run_service("actor")
        self.assertIn("already superseded", res.error)
        self.assertEqual(len(self.store.query_claims(["t"])), 1)  # nothing committed

    def test_supersede_own_claim_works(self):
        def v1(ctx):
            ctx.state["id"] = ctx.write("t", 1)
        self.orch.register(manifest("actor", writes=["t"]), v1)
        self.orch.run_service("actor")
        old = self.store.get_state("actor")["id"]
        self.clock.advance(1)

        def v2(ctx):
            ctx.write("t", 2, supersedes=old)
        self.orch.register(manifest("actor", writes=["t"]), v2)
        self.assertEqual(self.orch.run_service("actor").status, "success")
        self.assertEqual([c.content for c in self.store.query_claims(["t"])], [2])

    def test_message_permissions(self):
        self.orch.register(manifest("target"), lambda ctx: None)
        res = self.run_fn(lambda ctx: ctx.send("target", "hi", {}), can_message=[])
        self.assertIn("PermissionDenied", res.error)
        res = self.run_fn(lambda ctx: ctx.send("ghost", "hi", {}), can_message=["*"])
        self.assertIn("MessageError", res.error)
        res = self.run_fn(lambda ctx: ctx.send("target", "hi", {"a": 1}), can_message=["target"])
        self.assertEqual(res.status, "success")
        self.assertEqual(len(self.store.pending_messages("target")), 1)


if __name__ == "__main__":
    unittest.main()
