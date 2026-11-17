"""Regression tests for the findings of the independent review (2026-11-17).

Each scenario was reproduced against the published v0.2.0 code before being fixed.
"""
import http.server
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.error

from helpers import FakeClock, make_orch, manifest

from oas import Claim, ClaimError, Orchestrator, Store, compute_claim_id, ed25519, export_bundle
from oas.store import signing_message, verify_wire_signature
from oas.transport import pull, sync_dir

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def node():
    s = Store(":memory:", clock=FakeClock())
    return Orchestrator(s, sleep=lambda x: None), s


def produce(orch, service, claims):
    orch.register(manifest(service, writes=sorted({t for t, _ in claims})),
                  lambda ctx: [ctx.write(t, c) for t, c in claims])
    orch.run_service(service)


def sign_as(store, claim_id, origin):
    return ed25519.sign(store._seed, signing_message(claim_id, origin)).hex()


class ReadPermissionBypassTests(unittest.TestCase):
    """HIGH: reads ["x?"] must not admit read("x*") (the string passed the check, then acted as a pattern)."""

    def setUp(self):
        self.orch, self.store, self.clock, _ = make_orch()
        self.addCleanup(self.store.close)
        produce(self.orch, "prod", [("xa", {"p": 1}), ("xsecret.key", {"k": "TOPSECRET"})])

    def test_wildcard_read_returns_only_claims_the_manifest_allows(self):
        seen = {}

        def reader(ctx):
            seen["types"] = sorted(c.type for c in ctx.read("x*"))
            seen["limited"] = [c.type for c in ctx.read("x*", limit=1)]
        self.orch.register(manifest("r", reads=["x?"]), reader)
        self.assertEqual(self.orch.run_service("r").status, "success")
        self.assertEqual(seen["types"], ["xa"])
        self.assertEqual(seen["limited"], ["xa"])             # limit applies after filtering, not before

    def test_direct_read_of_the_secret_type_is_still_denied(self):
        self.orch.register(manifest("r", reads=["x?"]), lambda ctx: ctx.read("xsecret.key"))
        self.assertIn("PermissionDenied", self.orch.run_service("r").error)

    def test_exactly_declared_wildcard_still_works(self):
        out = {}
        self.orch.register(manifest("r", reads=["x*"]), lambda ctx: out.update(t=sorted(c.type for c in ctx.read("x*"))))
        self.orch.run_service("r")
        self.assertEqual(out["t"], ["xa", "xsecret.key"])

    def test_trigger_info_does_not_leak_ids_of_unreadable_types(self):
        self.orch.register(manifest("r", reads=["x?"], triggers=[{"kind": "event", "claim_type": "xsecret.key"}]),
                           lambda ctx: None)
        reasons, _ = self.orch.due_reasons("r")
        self.assertEqual(reasons[0]["count"], 1)
        self.assertEqual(reasons[0]["claim_ids"], [])


class OriginBindingTests(unittest.TestCase):
    """MEDIUM: a trusted key must not be able to speak for another node's origin."""

    def setUp(self):
        self.ob, self.sb = node()
        self.oa, self.sa = node()
        self.oc, self.sc = node()
        for s in (self.sa, self.sb, self.sc):
            self.addCleanup(s.close)
        produce(self.ob, "weather", [("weather", {"temp": 20})])
        self.real = self.sb.query_claims(["weather"])[0]
        self.sc.add_trusted_signer(self.sa.public_key, "a", ["weather"])
        self.sc.add_trusted_signer(self.sb.public_key, "b", ["weather"])

    def forged_bundle(self):
        cid = compute_claim_id("weather", {"temp": -99}, "weather", [], 1.0, 5.0, self.real.id)
        wire = {"id": cid, "type": "weather", "content": {"temp": -99}, "source_service": "weather", "inputs": [],
                "confidence": 1.0, "created_at": 5.0, "supersedes": self.real.id, "origin": self.sb.node_id,
                "signer": self.sa.public_key, "signature": sign_as(self.sa, cid, self.sb.node_id)}
        self.assertEqual(verify_wire_signature(wire), "valid")           # a perfectly valid signature by a trusted key
        return {"format": "oas-bundle/1", "node_id": self.sa.node_id, "claims": [wire]}

    def test_trusted_key_cannot_claim_another_nodes_origin(self):
        self.assertEqual(self.oc.import_bundle(export_bundle(self.sb), require_trusted=True)["imported"], 1)
        res = self.oc.import_bundle(self.forged_bundle(), require_trusted=True)
        self.assertEqual(res["imported"], 0)
        self.assertIn("is bound to a different signing key", res["rejected"][0]["reason"])
        self.assertEqual([c.content for c in self.sc.query_claims(["weather"])], [{"temp": 20}])

    def test_origin_binding_is_first_use_and_survives_relaying(self):
        self.oc.import_bundle(export_bundle(self.sb))
        self.assertEqual(self.sc.origin_key(self.sb.node_id), self.sb.public_key)
        self.assertEqual(self.sc.origin_key(self.sc.node_id), self.sc.public_key)

    def test_signed_copy_replaces_an_unsigned_copy_with_a_forged_origin(self):
        genuine = export_bundle(self.sb)
        lax = {**genuine, "claims": [{**w, "origin": "nodeEVIL", "signer": None, "signature": None}
                                     for w in genuine["claims"]]}
        self.oc.import_bundle(lax)                                      # lax mode: unsigned copy arrives first
        self.assertEqual(self.sc.get_claim(self.real.id).origin, "nodeEVIL")
        res = self.oc.import_bundle(genuine)
        self.assertEqual((res["imported"], res["duplicates"], res["upgraded"]), (0, 1, 1))
        got = self.sc.get_claim(self.real.id)
        self.assertEqual((got.origin, got.signer, self.sc.verify_claim(got.id)),
                         (self.sb.node_id, self.sb.public_key, "valid"))

    def test_signed_claims_are_never_overwritten_by_a_later_copy(self):
        self.oc.import_bundle(export_bundle(self.sb))
        other = export_bundle(self.sb)
        other["claims"][0]["origin"] = "nodeEVIL"                        # signature no longer matches; and it is a duplicate
        res = self.oc.import_bundle(other)
        self.assertEqual((res["imported"], res["upgraded"]), (0, 0))
        self.assertEqual(self.sc.get_claim(self.real.id).origin, self.sb.node_id)


class HostileValueTests(unittest.TestCase):
    def setUp(self):
        self.oa, self.sa = node()
        self.ob, self.sb = node()
        self.addCleanup(self.sa.close)
        self.addCleanup(self.sb.close)
        produce(self.oa, "p", [("t", {"ok": 1})])
        self.good = export_bundle(self.sa)

    def with_bad(self, **override):
        bad = {**self.good["claims"][0], **override}
        return {**self.good, "claims": [bad, *self.good["claims"]]}

    def test_each_hostile_claim_is_rejected_and_the_good_one_still_imports(self):
        for override in ({"created_at": 10 ** 400}, {"created_at": float("inf")}, {"origin": "\ud800"},
                         {"type": "\ud800"}, {"type": "a\x00b"}, {"source_service": "a\x00b"},
                         {"confidence": 1}, {"confidence": True}, {"created_at": 5}, {"signer": 7},
                         {"inputs": ["\ud800"]}):
            sb = Store(":memory:", clock=FakeClock())
            ob = Orchestrator(sb)
            try:
                res = ob.import_bundle(self.with_bad(**override))
                self.assertEqual((res["imported"], len(res["rejected"])), (1, 1), msg=str(override))
            finally:
                sb.close()

    def test_sync_dir_survives_a_hostile_file_and_a_fifo(self):
        with tempfile.TemporaryDirectory() as d:
            sync_dir(self.oa, d)
            with open(os.path.join(d, "a.bundle.json"), "w") as f:
                import json
                json.dump(self.with_bad(created_at=10 ** 400), f)
            os.mkfifo(os.path.join(d, "b.bundle.json"))
            result = {}
            t = threading.Thread(target=lambda: result.update(r=sync_dir(self.ob, d)["imported"]), daemon=True)
            t.start()
            t.join(15)
            self.assertFalse(t.is_alive(), "sync_dir hung on a FIFO")
            r = result["r"]
            self.assertIn("not a regular file", r["b.bundle.json"]["error"])
            # file order depends on the random node id; the good claim arrives via exactly one of the two files
            good = [r["a.bundle.json"], r[f"{self.sa.node_id}.bundle.json"]]
            self.assertEqual(sum(g["imported"] for g in good), 1)
            self.assertEqual(len(r["a.bundle.json"]["rejected"]), 1)
            self.assertTrue(stat.S_ISFIFO(os.stat(os.path.join(d, "b.bundle.json")).st_mode))

    def test_nul_in_claim_type_and_negative_limit(self):
        orch, store, _, _ = make_orch()
        self.addCleanup(store.close)
        orch.register(manifest("w", writes=["a*"]), lambda ctx: ctx.write("a\x00b", 1))
        self.assertIn("ClaimError", orch.run_service("w").error)
        with self.assertRaises(ValueError):
            store.query_claims(["a*"], limit=-1)

    def test_sql_pushdown_agrees_with_fnmatch_for_ordinary_types(self):
        import fnmatch
        s = Store(":memory:", clock=FakeClock())
        self.addCleanup(s.close)
        for i, t in enumerate(["abc", "ab", "abz", "a.b", "xyz"]):
            cid = compute_claim_id(t, i, "s", [], 1.0, float(i), None)
            s.insert_claim(Claim(cid, t, i, "s", (), 1.0, float(i), None, s.node_id))
        for pat in ("ab?", "a*", "a?c", "*z", "[ax]*"):
            want = sorted(t for t in ["abc", "ab", "abz", "a.b", "xyz"] if fnmatch.fnmatchcase(t, pat))
            self.assertEqual(sorted(c.type for c in s.query_claims([pat])), want, msg=pat)


class SignatureStrictnessTests(unittest.TestCase):
    def test_small_order_keys_cannot_forge_and_cannot_be_trusted(self):
        identity = bytes([1] + [0] * 31)                                  # the group identity, order 1
        sig = identity + bytes(32)
        self.assertFalse(ed25519.verify(identity, b"any message", sig))   # universal forgery attempt
        self.assertFalse(ed25519.verify(identity, b"another", sig))
        self.assertTrue(ed25519.is_small_order(identity))
        self.assertFalse(ed25519.is_small_order(ed25519.public_key(bytes(range(32)))))
        s = Store(":memory:", clock=FakeClock())
        self.addCleanup(s.close)
        with self.assertRaises(ClaimError):
            s.add_trusted_signer(identity.hex(), "evil", ["*"])

    def test_only_canonical_lowercase_hex_is_accepted(self):
        orch, s = node()
        self.addCleanup(s.close)
        produce(orch, "p", [("t", 1)])
        wire = s.query_claims()[0].to_wire()
        self.assertEqual(verify_wire_signature(wire), "valid")
        for mutate in (lambda w: w.update(signature=w["signature"].upper()),
                       lambda w: w.update(signature=" " + w["signature"]),
                       lambda w: w.update(signer=w["signer"].upper()),
                       lambda w: w.update(signature=w["signature"][:-2])):
            w = dict(wire)
            mutate(w)
            self.assertEqual(verify_wire_signature(w), "invalid")


class IsolationBoundTests(unittest.TestCase):
    def test_child_that_stops_reading_cannot_outlive_the_timeout(self):
        orch, store, _, _ = make_orch()
        self.addCleanup(store.close)
        orch.register(manifest("w", writes=["big"]),
                      lambda ctx: [ctx.write("big", {"pad": "x" * 20000, "i": i}) for i in range(300)])
        orch.run_service("w")
        orch.register(manifest("f", reads=["big"], entrypoint="iso_flood:run", isolation="process",
                               timeout_seconds=2), base_dir=TESTS_DIR)
        result = {}

        def run():
            t0 = time.monotonic()
            result["r"] = orch.run_service("f")
            result["elapsed"] = time.monotonic() - t0
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(30)
        self.assertFalse(t.is_alive(), "parent hung in sendall: timeout was not enforced")
        self.assertEqual(result["r"].status, "failed")
        self.assertIn("ServiceTimeout", result["r"].error)
        self.assertLess(result["elapsed"], 10)

    def test_isolated_services_without_a_timeout_get_a_default_bound(self):
        import oas.orchestrator as om
        orch, store, _, _ = make_orch()
        self.addCleanup(store.close)
        captured = {}
        original = om.run_in_process
        om.run_in_process = lambda ctx, ep, bd, timeout, mem: captured.update(timeout=timeout)
        self.addCleanup(setattr, om, "run_in_process", original)
        orch.register(manifest("i", entrypoint="iso_services:writer", isolation="process"), base_dir=TESTS_DIR)
        orch.run_service("i")
        self.assertEqual(captured["timeout"], om.DEFAULT_TIMEOUT)


class CronReregisterTests(unittest.TestCase):
    def test_new_cron_expression_does_not_fire_a_stale_slot_immediately(self):
        orch, store, clock, _ = make_orch()
        self.addCleanup(store.close)
        orch.register(manifest("c", triggers=[{"kind": "cron", "expr": "0 12 * * *"}]), lambda ctx: None)
        clock.advance(400 * 86400)            # a year passes; the new expression's last Jan 1 slot is long gone
        orch.register(manifest("c", version="2", triggers=[{"kind": "cron", "expr": "30 3 1 1 *"}]), lambda ctx: None)
        self.assertEqual(orch.tick().runs, [])


class PullRedirectTests(unittest.TestCase):
    def test_pull_does_not_follow_redirects(self):
        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.end_headers()

            def log_message(self, *a):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        orch, store, _, _ = make_orch()
        self.addCleanup(store.close)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            pull(orch, f"http://127.0.0.1:{server.server_address[1]}")
        self.assertEqual(cm.exception.code, 302)


if __name__ == "__main__":
    unittest.main()
