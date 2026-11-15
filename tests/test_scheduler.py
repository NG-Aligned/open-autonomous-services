import unittest

from helpers import make_orch, manifest


def writer(claim_type, content=None):
    def fn(ctx):
        ctx.write(claim_type, content if content is not None else {"t": ctx.now()})
    return fn


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.orch, self.store, self.clock, self.sleep = make_orch()
        self.addCleanup(self.store.close)

    def ids(self, result):
        return [r.service_id for r in result.runs]

    def test_interval_due_logic(self):
        self.orch.register(manifest("ticker", triggers=[{"kind": "interval", "seconds": 60}]), lambda ctx: None)
        self.assertEqual(self.ids(self.orch.tick()), ["ticker"])      # first tick runs immediately
        self.assertEqual(self.ids(self.orch.tick()), [])              # same instant: not due
        self.clock.advance(59)
        self.assertEqual(self.ids(self.orch.tick()), [])
        self.clock.advance(1)
        self.assertEqual(self.ids(self.orch.tick()), ["ticker"])

    def test_event_trigger_fires_once_per_new_claim_and_not_on_own_writes(self):
        self.orch.register(manifest("prod", writes=["x"]), writer("x"))
        runs = []

        def consumer(ctx):
            runs.append(ctx.trigger[0]["count"])
            ctx.write("y", 1)
        self.orch.register(manifest("cons", reads=["x"], writes=["y"],
                                    triggers=[{"kind": "event", "claim_type": "x"}]), consumer)
        self.assertEqual(self.ids(self.orch.tick()), [])              # nothing yet
        self.orch.run_service("prod")
        self.assertEqual(self.ids(self.orch.tick()), ["cons"])
        self.assertEqual(self.ids(self.orch.tick()), [])              # cursor advanced
        self.clock.advance(1)
        self.orch.run_service("prod")
        self.assertEqual(self.ids(self.orch.tick()), ["cons"])
        self.assertEqual(runs, [1, 1])

    def test_self_trigger_loop_avoided(self):
        count = {"n": 0}

        def selfish(ctx):
            count["n"] += 1
            ctx.write("x", count["n"])
        self.orch.register(manifest("selfish", reads=["x"], writes=["x"],
                                    triggers=[{"kind": "event", "claim_type": "x"}]), selfish)
        self.orch.run_service("selfish")
        self.assertEqual(self.ids(self.orch.tick()), [])
        self.assertEqual(count["n"], 1)

    def test_dependency_trigger(self):
        self.orch.register(manifest("up"), lambda ctx: None)
        self.orch.register(manifest("down", triggers=[{"kind": "dependency", "service": "up"}]), lambda ctx: None)
        self.assertEqual(self.ids(self.orch.tick()), [])
        self.orch.run_service("up")
        self.assertEqual(self.ids(self.orch.tick()), ["down"])
        self.assertEqual(self.ids(self.orch.tick()), [])

    def test_message_trigger_and_consumption(self):
        got = []

        def receiver(ctx):
            got.extend(m["payload"] for m in ctx.inbox())
        self.orch.register(manifest("rx", triggers=[{"kind": "message"}]), receiver)
        self.orch.register(manifest("tx", can_message=["rx"]), lambda ctx: ctx.send("rx", "hello", {"n": 1}))
        self.orch.run_service("tx")
        self.assertEqual(self.ids(self.orch.tick()), ["rx"])
        self.assertEqual(got, [{"n": 1}])
        self.assertEqual(self.ids(self.orch.tick()), [])              # consumed

    def test_message_not_consumed_when_receiver_fails(self):
        def bad(ctx):
            ctx.inbox()
            raise RuntimeError("nope")
        self.orch.register(manifest("rx", triggers=[{"kind": "message"}], circuit_breaker=5), bad)
        self.orch.register(manifest("tx", can_message=["rx"]), lambda ctx: ctx.send("rx", "t", 1))
        self.orch.run_service("tx")
        self.orch.tick()
        self.assertEqual(len(self.store.pending_messages("rx")), 1)

    def test_manual_trigger_never_auto_runs(self):
        self.orch.register(manifest("m", triggers=[{"kind": "manual"}]), lambda ctx: None)
        self.assertEqual(self.ids(self.orch.tick()), [])

    def test_cascade_in_one_tick_and_topological_order_independent_of_registration(self):
        order = []
        # registered in reverse order on purpose
        self.orch.register(manifest("c", reads=["b"], triggers=[{"kind": "dependency", "service": "b"}]),
                           lambda ctx: order.append("c"))

        def b(ctx):
            order.append("b")
            ctx.write("b", 1)
        self.orch.register(manifest("b", reads=["a"], writes=["b"], triggers=[{"kind": "event", "claim_type": "a"}]), b)

        def a(ctx):
            order.append("a")
            ctx.write("a", 1)
        self.orch.register(manifest("a", writes=["a"], triggers=[{"kind": "interval", "seconds": 10}]), a)
        result = self.orch.tick()
        self.assertEqual(order, ["a", "b", "c"])
        self.assertFalse(result.truncated)
        self.assertEqual(self.orch.graph()["order"], ["a", "b", "c"])

    def test_cycle_detected_and_tick_is_bounded(self):
        # a reacts to y and writes x; b reacts to x and writes y => endless ping-pong.
        # Content varies per write: identical claims would deduplicate (content-addressed) and end the loop.
        counter = iter(range(1000))

        def varying(claim_type):
            return lambda ctx: ctx.write(claim_type, {"n": next(counter)})
        self.orch.register(manifest("a", reads=["y"], writes=["x"], triggers=[{"kind": "event", "claim_type": "y"}]),
                           varying("x"))
        self.orch.register(manifest("b", reads=["x"], writes=["y"], triggers=[{"kind": "event", "claim_type": "x"}]),
                           varying("y"))
        self.orch.register(manifest("seed", writes=["x"]), varying("x"))
        self.orch.run_service("seed")
        self.assertEqual(self.orch.graph()["cycles"], [["a", "b"]])
        self.assertTrue(any("cycle" in i["message"] for i in self.orch.validate()))
        result = self.orch.tick(max_rounds=4)
        self.assertTrue(result.truncated)
        self.assertEqual(result.rounds, 4)

    def test_failed_service_not_retried_within_same_tick(self):
        calls = {"n": 0}

        def bad(ctx):
            calls["n"] += 1
            raise RuntimeError("x")
        self.orch.register(manifest("prod", writes=["x"]), writer("x"))
        self.orch.register(manifest("bad", reads=["x"], triggers=[{"kind": "event", "claim_type": "x"}],
                                    circuit_breaker=10), bad)
        self.orch.run_service("prod")
        result = self.orch.tick(max_rounds=5)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(result.runs[0].status, "failed")

    def test_event_cursor_only_advances_on_success_so_failed_work_is_redelivered(self):
        mode = {"fail": True}
        processed = []

        def consumer(ctx):
            if mode["fail"]:
                raise RuntimeError("transient")
            processed.extend(c.id for c in ctx.read("x"))
        self.orch.register(manifest("prod", writes=["x"]), writer("x"))
        self.orch.register(manifest("cons", reads=["x"], triggers=[{"kind": "event", "claim_type": "x"}],
                                    circuit_breaker=10), consumer)
        self.orch.run_service("prod")
        self.assertEqual(self.orch.tick().runs[0].status, "failed")
        self.assertEqual(processed, [])
        mode["fail"] = False
        self.assertEqual(self.orch.tick().runs[0].status, "success")     # same claim delivered again
        self.assertEqual(len(processed), 1)
        self.assertEqual(self.orch.tick().runs, [])                      # and now it is done

    def test_disabled_services_are_skipped(self):
        self.orch.register(manifest("t", triggers=[{"kind": "interval", "seconds": 1}]), lambda ctx: None)
        self.orch.disable("t", "maintenance")
        self.assertEqual(self.ids(self.orch.tick()), [])
        self.orch.enable("t")
        self.assertEqual(self.ids(self.orch.tick()), ["t"])

    def test_failed_interval_service_does_not_hot_loop(self):
        calls = {"n": 0}

        def bad(ctx):
            calls["n"] += 1
            raise RuntimeError("x")
        self.orch.register(manifest("t", triggers=[{"kind": "interval", "seconds": 30}], circuit_breaker=10), bad)
        self.orch.tick()
        self.orch.tick()
        self.assertEqual(calls["n"], 1)
        self.clock.advance(30)
        self.orch.tick()
        self.assertEqual(calls["n"], 2)

    def test_discovery_of_providers_and_consumers(self):
        self.orch.register(manifest("p1", writes=["paper"]), lambda ctx: None)
        self.orch.register(manifest("p2", writes=["paper.preprint"]), lambda ctx: None)
        self.orch.register(manifest("c1", reads=["paper.*"]), lambda ctx: None)
        self.assertEqual(self.orch.find_providers("paper"), ["p1"])
        self.assertEqual(self.orch.find_providers("paper.preprint"), ["p2"])
        self.assertEqual(self.orch.find_consumers("paper.preprint"), ["c1"])

    def test_validate_reports_unknown_dependency_and_unserved_reads(self):
        self.orch.register(manifest("x", reads=["nothing"], triggers=[{"kind": "dependency", "service": "ghost"}]),
                           lambda ctx: None)
        issues = self.orch.validate()
        levels = {(i["level"], i["message"].split("'")[1]) for i in issues if "'" in i["message"]}
        self.assertIn(("error", "ghost"), levels)
        self.assertIn(("warning", "nothing"), levels)

    def test_discover_directory_reports_bad_manifests(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "ok.json").write_text(json.dumps(manifest("fine", entrypoint="nomod:fn")))
            (Path(d) / "bad.json").write_text(json.dumps({"id": "BAD"}))
            ok, errors = self.orch.discover(d)
        self.assertEqual(ok, ["fine"])
        self.assertIn("bad.json", errors)


if __name__ == "__main__":
    unittest.main()
