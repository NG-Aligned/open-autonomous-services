import copy
import json
import os
import subprocess
import tempfile
import unittest

from helpers import EXAMPLES, FakeClock, ROOT, make_orch, manifest

from oas import ClaimError, Orchestrator, Store, ed25519, export_bundle
from test_cli import oas as run_cli

RFC8032 = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]


def node():
    store = Store(":memory:", clock=FakeClock())
    return Orchestrator(store, sleep=lambda s: None), store


def producer(orch, service="producer", types=("paper",)):
    def fn(ctx):
        for i, t in enumerate(types):
            ctx.write(t, {"id": f"{t}-{i}"})
    orch.register(manifest(service, writes=list(types)), fn)
    orch.run_service(service)


class Ed25519Tests(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for sk, pk, msg, sig in RFC8032:
            sk, pk, msg, sig = map(bytes.fromhex, (sk, pk, msg, sig))
            self.assertEqual(ed25519.public_key(sk), pk)
            self.assertEqual(ed25519.sign(sk, msg), sig)
            self.assertTrue(ed25519.verify(pk, msg, sig))

    def test_rejects_tampering_and_malformed_input(self):
        sk = bytes(range(32))
        pk = ed25519.public_key(sk)
        sig = ed25519.sign(sk, b"hello")
        self.assertTrue(ed25519.verify(pk, b"hello", sig))
        self.assertFalse(ed25519.verify(pk, b"hellp", sig))
        self.assertFalse(ed25519.verify(pk, b"hello", sig[:-1] + bytes([sig[-1] ^ 1])))
        self.assertFalse(ed25519.verify(ed25519.public_key(bytes(32)), b"hello", sig))
        for bad in (b"", b"x" * 63, b"x" * 65):
            self.assertFalse(ed25519.verify(pk, b"hello", bad))
        self.assertFalse(ed25519.verify(b"short", b"hello", sig))
        self.assertFalse(ed25519.verify(pk, b"hello", sig[:32] + (ed25519.L).to_bytes(32, "little")))  # s >= L
        with self.assertRaises(ValueError):
            ed25519.public_key(b"short")

    def test_cross_check_against_independent_library(self):
        try:
            from cryptography.hazmat.primitives import serialization as ser
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except BaseException as exc:  # a broken native build can panic (not an Exception) on import
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.skipTest(f"cryptography unavailable ({type(exc).__name__}); optional cross-check skipped")
        for i in range(25):
            seed, msg = os.urandom(32), os.urandom(i * 11)
            lib = Ed25519PrivateKey.from_private_bytes(seed)
            raw = lib.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
            self.assertEqual(ed25519.public_key(seed), raw)
            self.assertEqual(ed25519.sign(seed, msg), lib.sign(msg))
            self.assertTrue(ed25519.verify(raw, msg, lib.sign(msg)))


class SignedClaimTests(unittest.TestCase):
    def setUp(self):
        self.oa, self.sa = node()
        self.ob, self.sb = node()
        self.addCleanup(self.sa.close)
        self.addCleanup(self.sb.close)
        producer(self.oa)

    def test_local_claims_are_signed_and_verify(self):
        c = self.sa.query_claims(["paper"])[0]
        self.assertEqual(c.signer, self.sa.public_key)
        self.assertEqual(self.sa.verify_claim(c.id), "valid")
        self.assertEqual(self.sa.lineage(c.id)["ancestors"][0]["signature"], "valid")

    def test_signature_survives_export_import_and_stays_valid(self):
        res = self.ob.import_bundle(export_bundle(self.sa))
        self.assertEqual(res["imported"], 1)
        c = self.sb.query_claims(["paper"])[0]
        self.assertEqual((c.signer, self.sb.verify_claim(c.id)), (self.sa.public_key, "valid"))

    def test_altered_origin_is_caught_even_though_the_id_is_unchanged(self):
        bundle = export_bundle(self.sa)
        bundle["claims"][0]["origin"] = "someone-else"
        res = self.ob.import_bundle(bundle)
        self.assertEqual(res["imported"], 0)
        self.assertIn("signature does not verify", res["rejected"][0]["reason"])

    def test_swapped_signature_and_garbage_rejected(self):
        producer(self.ob, "other")
        other_sig = self.sb.query_claims(["paper"])[0].signature
        for mutate in (lambda w: w.update(signature=other_sig),
                       lambda w: w.update(signer="zz"),
                       lambda w: w.update(signature="00" * 64),
                       lambda w: w.update(signature=None)):                  # signer present, signature missing
            bundle = copy.deepcopy(export_bundle(self.sa))
            mutate(bundle["claims"][0])
            res = self.ob.import_bundle(bundle)
            self.assertEqual(res["imported"], 0)
            self.assertIn("signature does not verify", res["rejected"][0]["reason"])

    def test_unsigned_accepted_by_default_rejected_when_required(self):
        bundle = export_bundle(self.sa)
        bundle["claims"][0]["signer"] = bundle["claims"][0]["signature"] = None
        self.assertEqual(self.ob.import_bundle(copy.deepcopy(bundle))["imported"], 1)
        oc, sc = node()
        self.addCleanup(sc.close)
        res = oc.import_bundle(bundle, require_signatures=True)
        self.assertEqual(res["imported"], 0)
        self.assertIn("unsigned", res["rejected"][0]["reason"])
        self.assertEqual(sc.query_claims(), [])

    def test_trusted_signer_policy(self):
        bundle = export_bundle(self.sa)
        res = self.ob.import_bundle(bundle, require_trusted=True)                    # nobody trusted yet
        self.assertIn("not trusted", res["rejected"][0]["reason"])
        self.sb.add_trusted_signer(self.sa.public_key, "node-a", ["other_*"])        # wrong service pattern
        res = self.ob.import_bundle(bundle, require_trusted=True)
        self.assertIn("not trusted for service 'producer'", res["rejected"][0]["reason"])
        self.sb.add_trusted_signer(self.sa.public_key, "node-a", ["producer"])       # now allowed
        res = self.ob.import_bundle(bundle, require_trusted=True)
        self.assertEqual((res["imported"], res["rejected"]), (1, []))
        self.assertTrue(self.sb.remove_trusted_signer(self.sa.public_key))
        self.assertEqual(self.sb.trusted_signers(), {})

    def test_trusted_signer_cannot_vouch_for_a_service_it_is_not_allowed(self):
        producer(self.oa, "admin_svc", types=("keys",))                              # same signer, different service
        self.sb.add_trusted_signer(self.sa.public_key, "node-a", ["producer"])
        res = self.ob.import_bundle(export_bundle(self.sa), require_trusted=True)
        self.assertEqual(res["imported"], 1)
        self.assertEqual(len(res["rejected"]), 1)
        self.assertEqual(self.sb.query_claims()[0].source_service, "producer")

    def test_bad_pubkey_rejected_and_foreign_origin_cannot_be_signed(self):
        with self.assertRaises(ClaimError):
            self.sb.add_trusted_signer("nothex", "x", ["*"])
        foreign = self.sa.query_claims(["paper"])[0]
        with self.assertRaises(ClaimError):
            self.sb.sign_claim(foreign)

    def test_key_persists_across_reopen_and_is_not_exported(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "k.db")
            s1 = Store(path)
            pub = s1.public_key
            o1 = Orchestrator(s1)
            producer(o1)
            s1.close()
            s2 = Store(path)
            self.addCleanup(s2.close)
            self.assertEqual(s2.public_key, pub)
            self.assertEqual(s2.verify_claim(s2.query_claims()[0].id), "valid")
            blob = json.dumps(export_bundle(s2))
            self.assertNotIn(s2._seed.hex(), blob)
            self.assertIn(pub, blob)

    def test_database_from_before_signatures_is_upgraded_and_old_claims_report_unsigned(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            s = Store(path)
            producer(Orchestrator(s))
            cid = s.query_claims()[0].id
            s.close()
            conn = sqlite3.connect(path)                         # simulate a pre-signature claim row
            conn.execute("UPDATE claims SET signer=NULL, signature=NULL")
            conn.commit()
            conn.close()
            s2 = Store(path)
            self.addCleanup(s2.close)
            self.assertEqual(s2.verify_claim(cid), "unsigned")


class SignedCliTests(unittest.TestCase):
    def test_two_nodes_trust_flow_via_cli(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.db"), os.path.join(d, "b.db")
            run_cli(a, "discover", EXAMPLES)
            run_cli(a, "tick")
            pub = json.loads(run_cli(a, "whoami").stdout)["public_key"]
            bundle = os.path.join(d, "bundle.json")
            run_cli(a, "export", bundle)
            run_cli(b, "init")
            p = run_cli(b, "import", bundle, "--require-trusted")
            self.assertEqual(json.loads(p.stdout)["imported"], 0)                           # nobody trusted yet
            run_cli(b, "trust", "add", pub, "--label", "node-a", "--service", "literature_monitor",
                    "--service", "evidence_evaluator", "--service", "reporting_service")
            self.assertEqual(json.loads(run_cli(b, "trust", "list").stdout)[pub]["label"], "node-a")
            res = json.loads(run_cli(b, "import", bundle, "--require-trusted").stdout)
            self.assertEqual((res["imported"], res["rejected"]), (5, []))
            report = json.loads(run_cli(b, "claims", "--type", "report").stdout)[0]
            lin = json.loads(run_cli(b, "lineage", report["id"]).stdout)
            self.assertEqual({x["signature"] for x in lin["ancestors"]}, {"valid"})
            self.assertEqual(json.loads(run_cli(b, "trust", "remove", pub).stdout), {"removed": True})


if __name__ == "__main__":
    unittest.main()


class TrustFlowScriptTests(unittest.TestCase):
    """The README's headline demo (examples/trust_flow.sh) must keep producing the documented outcomes."""

    def test_script_outputs_match_the_readme(self):
        script = os.path.join(ROOT, "examples", "trust_flow.sh")
        p = subprocess.run(["bash", script], capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, PYTHONPATH=ROOT))
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        out = p.stdout
        self.assertIn("imported 0 | rejected 5 | signer", out)
        self.assertIn("imported 4 | rejected 1 | signer", out)
        self.assertIn("not trusted for service 'reporting_service'", out)
        self.assertIn("imported 3 | rejected 2 | signature does not verify", out)
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        for line in ("imported 0 | rejected 5 |", "imported 4 | rejected 1 |", "imported 3 | rejected 2 |"):
            self.assertIn(line, readme)                       # the README transcript has not drifted from the script
