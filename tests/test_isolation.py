import os
import time
import unittest

from helpers import ROOT, make_orch, manifest

from oas import ManifestError

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.orch, self.store, self.clock, self.sleep = make_orch()
        self.addCleanup(self.store.close)

    def reg(self, fn, id="iso", **kw):
        kw.setdefault("writes", ["out"])
        self.orch.register(manifest(id, entrypoint=f"iso_services:{fn}", isolation="process", **kw),
                           base_dir=TESTS_DIR)
        return id

    def test_runs_in_another_process_and_commits_claims_state_logs(self):
        self.reg("writer", config={"tag": "x"})
        res = self.orch.run_service("iso")
        self.assertEqual((res.status, res.claims_written, res.logs), ("success", 1, ["wrote from child"]))
        c = self.store.query_claims(["out"])[0]
        self.assertEqual((c.content, c.confidence, self.store.verify_claim(c.id)),
                         ({"n": 1, "cfg": "x"}, 0.7, "valid"))
        self.orch.run_service("iso")
        self.assertEqual(self.store.get_state("iso"), {"n": 2})              # state round-trips through the child

    def test_reads_and_provenance_through_rpc(self):
        self.orch.register(manifest("prod", writes=["data"]), lambda ctx: (ctx.write("data", 1), ctx.write("data", 2)))
        self.orch.run_service("prod")
        self.reg("reader", id="rd", reads=["data"], writes=["derived"])
        self.assertEqual(self.orch.run_service("rd").status, "success")
        d = self.store.query_claims(["derived"])[0]
        self.assertEqual(d.content, {"count": 2, "same": True})
        self.assertEqual(len(d.inputs), 2)
        self.assertEqual({a["type"] for a in self.store.lineage(d.id)["ancestors"]}, {"derived", "data"})

    def test_permissions_enforced_in_parent_and_audited(self):
        self.reg("forbidden", writes=["public"])
        res = self.orch.run_service("iso")
        self.assertEqual(res.status, "failed")
        self.assertIn("PermissionDenied", res.error)
        self.assertEqual(self.store.query_claims(), [])
        self.assertIn(False, [bool(a["allowed"]) for a in self.store.list_audit("iso")])

    def test_child_can_catch_a_permission_error_as_the_real_exception_type(self):
        self.reg("catches", writes=["out"])
        self.assertEqual(self.orch.run_service("iso").status, "success")
        self.assertEqual(self.store.query_claims(["out"])[0].content, {"caught": "PermissionDenied"})

    def test_timeout_really_kills_the_process(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            pid_file = os.path.join(d, "pid")
            self.reg("hang", timeout_seconds=1.0, config={"pid_file": pid_file})
            t0 = time.monotonic()
            res = self.orch.run_service("iso")
            elapsed = time.monotonic() - t0
            self.assertEqual(res.status, "failed")
            self.assertIn("ServiceTimeout", res.error)
            self.assertEqual(self.store.list_runs("iso")[0]["status"], "timeout")
            self.assertLess(elapsed, 5)
            with open(pid_file) as f:
                pid = int(f.read())
            self.assertNotEqual(pid, os.getpid())
            self.assertFalse(alive(pid), "child must be dead after timeout")
            self.assertEqual(self.store.query_claims(), [])

    def test_child_crash_only_fails_that_run(self):
        self.reg("crash")
        res = self.orch.run_service("iso")
        self.assertEqual(res.status, "failed")
        self.assertIn("exited with code 7", res.error)
        self.assertEqual(self.store.query_claims(), [])
        self.orch.register(manifest("ok", writes=["out"]), lambda ctx: ctx.write("out", 1))
        self.assertEqual(self.orch.run_service("ok").status, "success")     # parent unaffected

    def test_child_exception_is_reported(self):
        self.reg("raises")
        res = self.orch.run_service("iso")
        self.assertIn("ValueError: child exploded", res.error)

    def test_memory_limit_contains_a_hog(self):
        self.reg("hog", max_memory_mb=200)
        res = self.orch.run_service("iso")
        self.assertEqual(res.status, "failed")
        self.assertIn("MemoryError", res.error)
        self.assertEqual(self.store.query_claims(), [])

    def test_prints_in_the_child_do_not_break_the_protocol(self):
        self.reg("noisy")
        self.assertEqual(self.orch.run_service("iso").status, "success")
        self.assertEqual(self.store.query_claims(["out"])[0].content, {"ok": True})

    def test_messages_through_rpc_with_consumption_on_success(self):
        self.orch.register(manifest("peer"), lambda ctx: None)
        self.reg("messenger", can_message=["peer"])                   # target must exist before anyone messages it
        self.orch.register(manifest("src", can_message=["iso"]), lambda ctx: ctx.send("iso", "t", {"n": 1}))
        self.assertEqual(self.orch.run_service("src").status, "success")
        self.assertEqual(self.orch.run_service("iso").status, "success")
        self.assertEqual(self.store.query_claims(["out"])[0].content, {"received": [{"n": 1}]})
        self.assertEqual(self.store.pending_messages("iso"), [])
        self.assertEqual(self.store.pending_messages("peer")[0]["payload"], {"got": [{"n": 1}]})

    def test_retries_apply_to_isolated_services(self):
        self.reg("crash", retry={"max_attempts": 2, "backoff_seconds": 0.5})
        res = self.orch.run_service("iso")
        self.assertEqual((res.status, res.attempts), ("failed", 2))
        self.assertEqual(self.sleep.calls, [0.5])

    def test_manifest_validation(self):
        with self.assertRaises(ManifestError):
            self.orch.register(manifest("x", isolation="process"), lambda ctx: None)         # needs entrypoint
        with self.assertRaises(ManifestError):
            self.orch.register(manifest("y", entrypoint="a:b", isolation="vm"), lambda ctx: None)
        with self.assertRaises(ManifestError):
            self.orch.register(manifest("z", entrypoint="a:b", max_memory_mb=64), lambda ctx: None)  # needs process
        with self.assertRaises(ManifestError):
            self.orch.register(manifest("w", entrypoint="a:b", isolation="process", max_memory_mb=4), lambda ctx: None)


if __name__ == "__main__":
    unittest.main()
