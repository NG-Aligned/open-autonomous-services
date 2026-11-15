"""Gap-closing tests: context lookups, malformed input, entrypoint failures, remaining CLI commands, crash safety."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from helpers import EXAMPLES, ROOT, FakeClock, make_orch, manifest

from oas import Orchestrator, Store, import_bundle
from test_cli import oas


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.orch, self.store, self.clock, _ = make_orch()
        self.addCleanup(self.store.close)

    def test_get_claim_respects_read_permission_and_sees_own_pending(self):
        self.orch.register(manifest("prod", writes=["data"]),
                           lambda ctx: ctx.state.update(id=ctx.write("data", {"v": 1})))
        self.orch.run_service("prod")
        cid = self.store.get_state("prod")["id"]
        out = {}

        def reader(ctx):
            out["claim"] = ctx.get_claim(cid)
        self.orch.register(manifest("ok", reads=["data"]), reader)
        self.assertEqual(self.orch.run_service("ok").status, "success")
        self.assertEqual(out["claim"].content, {"v": 1})
        self.orch.register(manifest("nope", reads=["other"]), lambda ctx: ctx.get_claim(cid))
        self.assertIn("PermissionDenied", self.orch.run_service("nope").error)

        def own(ctx):
            new = ctx.write("data", {"v": 2})
            out["own"] = ctx.get_claim(new)
        self.orch.register(manifest("selfwriter", writes=["data"]), own)
        self.orch.run_service("selfwriter")
        self.assertEqual(out["own"].content, {"v": 2})

    def test_reads_do_not_see_own_uncommitted_writes(self):
        seen = {}

        def fn(ctx):
            ctx.write("data", 1)
            seen["during"] = len(ctx.read("data"))
        self.orch.register(manifest("s", reads=["data"], writes=["data"]), fn)
        self.orch.run_service("s")
        self.assertEqual(seen["during"], 0)
        self.assertEqual(len(self.store.query_claims(["data"])), 1)

    def test_entrypoint_missing_function_fails_cleanly(self):
        self.orch.register(manifest("e", entrypoint="research_services:does_not_exist"), base_dir=EXAMPLES)
        res = self.orch.run_service("e")
        self.assertEqual(res.status, "failed")
        self.assertIn("ServiceNotBound", res.error)

    def test_entrypoint_missing_module_fails_cleanly(self):
        self.orch.register(manifest("e2", entrypoint="no_such_module_xyz:fn"))
        res = self.orch.run_service("e2")
        self.assertEqual(res.status, "failed")
        self.assertIn("ModuleNotFoundError", res.error)


class FederationShapeTests(unittest.TestCase):
    def test_malformed_claim_shapes_are_each_rejected(self):
        store = Store(":memory:", clock=FakeClock())
        self.addCleanup(store.close)
        good = {"id": "x", "type": "t", "content": 1, "source_service": "s", "inputs": [],
                "confidence": 1.0, "created_at": 1.0, "supersedes": None, "origin": "n"}
        variants = [
            {**good, "inputs": "notalist"},
            {**good, "inputs": [1]},
            {**good, "confidence": 2},
            {**good, "confidence": True},
            {**good, "created_at": "now"},
            {**good, "type": ""},
            {**good, "supersedes": 5},
            {k: v for k, v in good.items() if k != "origin"},
        ]
        res = import_bundle(store, {"format": "oas-bundle/1", "node_id": "n", "claims": variants})
        self.assertEqual(res["imported"], 0)
        self.assertEqual(len(res["rejected"]), len(variants))


class MoreCliTests(unittest.TestCase):
    def test_validate_audit_enable_disable_run(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "x.db")
            oas(db, "discover", EXAMPLES)
            self.assertEqual([i for i in json.loads(oas(db, "validate").stdout) if i["level"] == "error"], [])
            run = json.loads(oas(db, "run", "literature_monitor").stdout)
            self.assertEqual((run["status"], run["claims_written"]), ("success", 2))
            self.assertEqual(json.loads(oas(db, "disable", "evidence_evaluator").stdout),
                             {"disabled": "evidence_evaluator"})
            listing = {s["id"]: s for s in json.loads(oas(db, "list").stdout)}
            self.assertFalse(listing["evidence_evaluator"]["enabled"])
            tick = json.loads(oas(db, "tick").stdout)
            self.assertNotIn("evidence_evaluator", [r["service_id"] for r in tick["runs"]])
            oas(db, "enable", "evidence_evaluator")
            tick = json.loads(oas(db, "tick").stdout)
            self.assertIn("evidence_evaluator", [r["service_id"] for r in tick["runs"]])
            actions = [a["action"] for a in json.loads(oas(db, "audit").stdout)]
            self.assertIn("disabled", actions)
            self.assertIn("enabled", actions)
            self.assertIn("registered", actions)
            p = oas(db, "register", os.path.join(d, "missing.json"), check=False)
            self.assertEqual(p.returncode, 1)


class CrashSafetyTests(unittest.TestCase):
    def test_hard_process_kill_mid_run_leaves_no_partial_commit(self):
        script = textwrap.dedent("""
            import os, sys
            sys.path.insert(0, sys.argv[1])
            from oas import Orchestrator, Store
            store = Store(sys.argv[2])
            orch = Orchestrator(store)
            def fn(ctx):
                ctx.write("t", {"half": "done"})
                ctx.state["n"] = 42
                os._exit(9)          # simulate power loss / SIGKILL before commit
            orch.register({"id": "svc", "version": "1", "purpose": "crash", "writes": ["t"]}, fn)
            orch.run_service("svc")
        """)
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "crash.db")
            p = subprocess.run([sys.executable, "-c", script, ROOT, db], capture_output=True, timeout=60)
            self.assertEqual(p.returncode, 9)
            store = Store(db)                       # database must still open cleanly
            self.addCleanup(store.close)
            self.assertEqual(store.query_claims(), [])
            self.assertEqual(store.get_state("svc"), {})
            self.assertEqual(store.list_runs("svc"), [])
            self.assertEqual(store.get_service("svc")["consecutive_failures"], 0)
            orch = Orchestrator(store, sleep=lambda s: None)
            orch.register({"id": "svc", "version": "1", "purpose": "recovered", "writes": ["t"]},
                          lambda ctx: ctx.write("t", {"ok": True}))
            # the dead process still holds the lease, so a prompt retry is refused...
            self.assertEqual(orch.run_service("svc").status, "skipped")
            self.assertEqual(store.query_claims(), [])
            # ...and once the lease expires the service recovers and runs normally
            import time
            store.clock = lambda: time.time() + 400
            self.assertEqual(orch.run_service("svc").status, "success")
            self.assertEqual(len(store.query_claims(["t"])), 1)


if __name__ == "__main__":
    unittest.main()
