import os
import tempfile
import unittest

from helpers import FakeClock

from oas import Claim, ClaimError, Store, compute_claim_id


def mk(store, type="t", content=None, source="s", inputs=(), supersedes=None, t=1.0, conf=1.0):
    content = {"v": 1} if content is None else content
    cid = compute_claim_id(type, content, source, inputs, conf, t, supersedes)
    c = Claim(cid, type, content, source, tuple(inputs), conf, t, supersedes, store.node_id)
    store.insert_claim(c)
    return c


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", clock=FakeClock())
        self.addCleanup(self.store.close)

    def test_claim_id_deterministic_and_content_sensitive(self):
        a = compute_claim_id("t", {"v": 1}, "s", [], 1.0, 1.0, None)
        b = compute_claim_id("t", {"v": 1}, "s", [], 1.0, 1.0, None)
        c = compute_claim_id("t", {"v": 2}, "s", [], 1.0, 1.0, None)
        d = compute_claim_id("t", {"v": 1}, "s", [], 0.9, 1.0, None)
        self.assertEqual(a, b)
        self.assertEqual(len({a, c, d}), 3)
        self.assertEqual(len(a), 64)

    def test_non_json_content_rejected(self):
        with self.assertRaises(ClaimError):
            compute_claim_id("t", {"x": object()}, "s", [], 1.0, 1.0, None)
        with self.assertRaises(ClaimError):
            compute_claim_id("t", {"x": float("nan")}, "s", [], 1.0, 1.0, None)

    def test_insert_get_and_idempotent_insert(self):
        c = mk(self.store)
        self.assertEqual(self.store.get_claim(c.id).content, {"v": 1})
        self.assertFalse(self.store.insert_claim(c))  # duplicate ignored
        self.assertEqual(len(self.store.query_claims(["t"])), 1)

    def test_supersedes_hides_old_from_current(self):
        a = mk(self.store, content={"v": 1}, t=1.0)
        b = mk(self.store, content={"v": 2}, supersedes=a.id, t=2.0)
        current = self.store.query_claims(["t"])
        self.assertEqual([c.id for c in current], [b.id])
        self.assertEqual(len(self.store.query_claims(["t"], current_only=False)), 2)
        self.assertTrue(self.store.is_superseded(a.id))
        self.assertFalse(self.store.is_superseded(b.id))

    def test_query_filters(self):
        mk(self.store, type="a.x", source="s1", t=1.0)
        mk(self.store, type="a.y", source="s2", t=2.0)
        mk(self.store, type="b", source="s1", t=3.0)
        self.assertEqual(len(self.store.query_claims(["a.*"])), 2)
        self.assertEqual(len(self.store.query_claims(source_service="s1")), 2)
        self.assertEqual(len(self.store.query_claims(after_seq=1)), 2)
        self.assertEqual(len(self.store.query_claims(limit=1)), 1)

    def test_lineage_inputs_supersedes_and_missing(self):
        p = mk(self.store, type="paper", content={"id": 1}, t=1.0)
        r1 = mk(self.store, type="report", content={"n": 1}, inputs=[p.id], t=2.0)
        r2 = mk(self.store, type="report", content={"n": 2}, inputs=[p.id, "f" * 64], supersedes=r1.id, t=3.0)
        lin = self.store.lineage(r2.id)
        ids = {a["id"]: a for a in lin["ancestors"]}
        self.assertEqual(set(ids), {r2.id, r1.id, p.id})
        self.assertEqual(ids[r1.id]["relation"], "supersedes")
        self.assertEqual(ids[p.id]["relation"], "input")
        self.assertEqual(lin["missing"], ["f" * 64])
        with self.assertRaises(KeyError):
            self.store.lineage("0" * 64)

    def test_transaction_rollback_and_no_nesting(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                mk(self.store)
                raise RuntimeError("boom")
        self.assertEqual(self.store.query_claims(), [])
        with self.store.transaction():
            with self.assertRaises(Exception):
                with self.store.transaction():
                    pass

    def test_state_and_persistence_across_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.db")
            s1 = Store(path, clock=FakeClock())
            node = s1.node_id
            c = mk(s1)
            s1.set_state("svc", {"cursor": 7})
            s1.close()
            s2 = Store(path, clock=FakeClock())
            self.assertEqual(s2.node_id, node)
            self.assertEqual(s2.get_state("svc"), {"cursor": 7})
            self.assertIsNotNone(s2.get_claim(c.id))
            s2.close()

    def test_messages_and_audit(self):
        mid = self.store.add_message("a", "b", "hi", {"k": 1})
        self.assertEqual(self.store.pending_messages("b")[0]["payload"], {"k": 1})
        self.store.mark_consumed([mid])
        self.assertEqual(self.store.pending_messages("b"), [])
        self.store.audit("a", "read", "x", False)
        self.assertEqual(self.store.list_audit("a")[0]["allowed"], 0)


if __name__ == "__main__":
    unittest.main()
