import os
import tempfile
import threading
import time
import unittest

from helpers import make_orch, manifest

from oas import ManifestError, Orchestrator, ServiceDisabled, ServiceNotFound, Store
from helpers import FakeClock


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.orch, self.store, self.clock, self.sleep = make_orch()
        self.addCleanup(self.store.close)

    def test_success_commits_claims_state_and_run_record(self):
        def fn(ctx):
            ctx.write("t", {"a": 1})
            ctx.state["n"] = 5
            ctx.log("hello")
        self.orch.register(manifest(writes=["t"]), fn)
        res = self.orch.run_service("svc")
        self.assertEqual((res.status, res.claims_written, res.attempts), ("success", 1, 1))
        self.assertEqual(self.store.get_state("svc"), {"n": 5})
        run = self.store.list_runs("svc")[0]
        self.assertEqual((run["status"], run["claims_written"], run["log"]), ("success", 1, ["hello"]))

    def test_failure_rolls_back_claims_state_messages(self):
        self.orch.register(manifest("peer"), lambda ctx: None)

        def fn(ctx):
            ctx.write("t", 1)
            ctx.state["n"] = 99
            ctx.send("peer", "x", {})
            raise RuntimeError("boom")
        self.orch.register(manifest(writes=["t"], can_message=["peer"]), fn)
        res = self.orch.run_service("svc")
        self.assertEqual(res.status, "failed")
        self.assertIn("RuntimeError: boom", res.error)
        self.assertEqual(self.store.query_claims(), [])
        self.assertEqual(self.store.get_state("svc"), {})
        self.assertEqual(self.store.pending_messages("peer"), [])
        self.assertEqual(self.store.list_runs("svc")[0]["status"], "failed")

    def test_retry_with_exponential_backoff_and_state_from_success_only(self):
        calls = {"n": 0}

        def flaky(ctx):
            calls["n"] += 1
            ctx.state["attempt_marker"] = calls["n"]
            if calls["n"] < 3:
                raise ValueError("transient")
            ctx.write("t", "ok")
        self.orch.register(manifest(writes=["t"], retry={"max_attempts": 3, "backoff_seconds": 0.5}), flaky)
        res = self.orch.run_service("svc")
        self.assertEqual((res.status, res.attempts), ("success", 3))
        self.assertEqual(self.sleep.calls, [0.5, 1.0])
        self.assertEqual([r["status"] for r in self.store.list_runs("svc")], ["failed", "failed", "success"])
        self.assertEqual(self.store.get_state("svc"), {"attempt_marker": 3})
        self.assertEqual(len(self.store.query_claims()), 1)

    def test_retries_exhausted(self):
        def bad(ctx):
            raise ValueError("always")
        self.orch.register(manifest(retry={"max_attempts": 2, "backoff_seconds": 1}), bad)
        res = self.orch.run_service("svc")
        self.assertEqual((res.status, res.attempts), ("failed", 2))
        self.assertEqual(self.sleep.calls, [1])
        self.assertEqual(self.store.get_service("svc")["consecutive_failures"], 1)

    def test_circuit_breaker_opens_blocks_and_enable_resets(self):
        state = {"fail": True}

        def fn(ctx):
            if state["fail"]:
                raise ValueError("down")
        self.orch.register(manifest(circuit_breaker=2), fn)
        self.orch.run_service("svc")
        self.assertTrue(self.store.get_service("svc")["enabled"])
        self.orch.run_service("svc")
        svc = self.store.get_service("svc")
        self.assertFalse(svc["enabled"])
        self.assertIn("circuit breaker", svc["disabled_reason"])
        with self.assertRaises(ServiceDisabled):
            self.orch.run_service("svc")
        state["fail"] = False
        self.orch.enable("svc")
        self.assertEqual(self.orch.run_service("svc").status, "success")
        self.assertEqual(self.store.get_service("svc")["consecutive_failures"], 0)
        actions = [a["action"] for a in self.store.list_audit("svc")]
        self.assertIn("circuit_breaker_open", actions)

    def test_success_resets_failure_counter(self):
        state = {"fail": True}

        def fn(ctx):
            if state["fail"]:
                raise ValueError("x")
        self.orch.register(manifest(circuit_breaker=3), fn)
        self.orch.run_service("svc")
        self.assertEqual(self.store.get_service("svc")["consecutive_failures"], 1)
        state["fail"] = False
        self.orch.run_service("svc")
        self.assertEqual(self.store.get_service("svc")["consecutive_failures"], 0)

    def test_timeout_fails_run_and_late_writes_never_commit(self):
        release = threading.Event()
        outcome = {}

        def slow(ctx):
            release.wait(5)
            try:
                ctx.write("t", "late")
                outcome["wrote"] = True
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = type(exc).__name__
        self.orch.register(manifest(writes=["t"], timeout_seconds=0.1), slow)
        res = self.orch.run_service("svc")
        self.assertEqual(res.status, "failed")
        self.assertIn("ServiceTimeout", res.error)
        self.assertEqual(self.store.list_runs("svc")[0]["status"], "timeout")
        release.set()
        time.sleep(0.2)
        self.assertEqual(outcome.get("error"), "ServiceCancelled")
        self.assertEqual(self.store.query_claims(), [])

    def test_state_persists_between_runs(self):
        def counter(ctx):
            ctx.state["n"] = ctx.state.get("n", 0) + 1
        self.orch.register(manifest(), counter)
        for _ in range(3):
            self.orch.run_service("svc")
        self.assertEqual(self.store.get_state("svc"), {"n": 3})

    def test_ephemeral_continuity_resets_state(self):
        seen = []

        def fn(ctx):
            seen.append(dict(ctx.state))
            ctx.state["n"] = 1
        self.orch.register(manifest(continuity="ephemeral"), fn)
        self.orch.run_service("svc")
        self.orch.run_service("svc")
        self.assertEqual(seen, [{}, {}])
        self.assertEqual(self.store.get_state("svc"), {})

    def test_unserialisable_state_fails_without_partial_commit(self):
        def fn(ctx):
            ctx.write("t", 1)
            ctx.state["bad"] = {1, 2}
        self.orch.register(manifest(writes=["t"]), fn)
        res = self.orch.run_service("svc")
        self.assertEqual(res.status, "failed")
        self.assertEqual(self.store.query_claims(), [])

    def test_config_defaults_applied_and_validated_on_register(self):
        seen = {}
        self.orch.register(manifest(config_schema={"n": {"type": "int", "default": 4}}),
                           lambda ctx: seen.update(ctx.config))
        self.orch.run_service("svc")
        self.assertEqual(seen, {"n": 4})
        with self.assertRaises(ManifestError):
            self.orch.register(manifest("other", config_schema={"n": {"type": "int", "required": True}}),
                               lambda ctx: None)

    def test_register_requires_function_or_entrypoint(self):
        with self.assertRaises(ManifestError):
            self.orch.register(manifest())

    def test_unbound_service_after_reopen_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.db")
            clock = FakeClock()
            s1 = Store(path, clock=clock)
            o1 = Orchestrator(s1, sleep=lambda s: None)
            o1.register(manifest(), lambda ctx: None)
            s1.close()
            s2 = Store(path, clock=clock)
            o2 = Orchestrator(s2, sleep=lambda s: None)
            res = o2.run_service("svc")
            self.assertEqual(res.status, "failed")
            self.assertIn("ServiceNotBound", res.error)
            o2.bind("svc", lambda ctx: ctx.state.update(ok=True))
            self.assertEqual(o2.run_service("svc").status, "success")
            s2.close()

    def test_reregister_new_version_keeps_state_and_claims_and_reenables(self):
        def v1(ctx):
            ctx.write("t", "from-v1")
            ctx.state["n"] = 1
        self.orch.register(manifest(version="1.0.0", writes=["t"], circuit_breaker=1), v1)
        self.orch.run_service("svc")
        self.orch.register(manifest(version="2.0.0", writes=["t"], circuit_breaker=1),
                           lambda ctx: (_ for _ in ()).throw(ValueError("v2 broken")))
        self.orch.run_service("svc")
        self.assertFalse(self.store.get_service("svc")["enabled"])

        def v3(ctx):
            ctx.state["n"] += 1
        self.orch.register(manifest(version="3.0.0", writes=["t"]), v3)
        self.assertTrue(self.store.get_service("svc")["enabled"])
        self.orch.clock  # noqa: B018
        self.assertEqual(self.orch.run_service("svc").status, "success")
        self.assertEqual(self.store.get_state("svc"), {"n": 2})
        self.assertEqual(len(self.store.query_claims(["t"])), 1)
        self.assertEqual(self.store.get_service("svc")["manifest"]["version"], "3.0.0")

    def test_unknown_service(self):
        with self.assertRaises(ServiceNotFound):
            self.orch.run_service("ghost")


if __name__ == "__main__":
    unittest.main()
