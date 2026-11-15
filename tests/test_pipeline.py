"""End-to-end: the three-service research pipeline from examples/research_pipeline."""
import os
import tempfile
import unittest

from helpers import EXAMPLES, FakeClock

from oas import Orchestrator, Store


def build(path=":memory:", clock=None):
    clock = clock or FakeClock()
    store = Store(path, clock=clock)
    orch = Orchestrator(store, sleep=lambda s: None)
    _OPEN.append(store)
    return orch, store, clock


_OPEN = []


class PipelineTests(unittest.TestCase):
    def tearDown(self):
        for s in _OPEN:
            s.close()
        _OPEN.clear()

    def test_discover_graph_and_validate(self):
        orch, store, _ = build()
        ok, errors = orch.discover(EXAMPLES)
        self.assertEqual(sorted(ok), ["evidence_evaluator", "literature_monitor", "reporting_service"])
        self.assertEqual(errors, {})
        g = orch.graph()
        self.assertEqual(g["order"], ["literature_monitor", "evidence_evaluator", "reporting_service"])
        self.assertEqual(g["cycles"], [])
        self.assertEqual([i for i in orch.validate() if i["level"] == "error"], [])
        self.assertEqual(orch.find_providers("evidence.assessment"), ["evidence_evaluator"])

    def test_full_run_with_provenance_and_supersession(self):
        orch, store, clock = build()
        orch.discover(EXAMPLES)
        r1 = orch.tick()
        self.assertEqual([r.service_id for r in r1.runs],
                         ["literature_monitor", "evidence_evaluator", "reporting_service"])
        self.assertTrue(all(r.status == "success" for r in r1.runs))
        self.assertEqual(len(store.query_claims(["paper"])), 2)
        self.assertEqual(len(store.query_claims(["evidence.assessment"])), 2)
        report1 = store.query_claims(["report"])[0]
        self.assertEqual(report1.content["n_assessed"], 2)
        self.assertEqual(report1.content["ranked"][0]["paper_id"], "p1")  # rct n=1200 ranks first

        self.assertEqual(orch.tick().runs, [])          # nothing due yet
        clock.advance(60)
        orch.tick()
        clock.advance(60)
        orch.tick()                                     # monitor exhausts its 5 papers
        clock.advance(60)
        self.assertEqual([r.service_id for r in orch.tick().runs], ["literature_monitor"])  # no new papers -> no cascade

        self.assertEqual(len(store.query_claims(["paper"])), 5)
        reports = store.query_claims(["report"], current_only=False)
        self.assertEqual(len(reports), 3)
        current = store.query_claims(["report"])
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0].content["n_assessed"], 5)
        # weakest link is p4 (case-report, n=1): 0.2 * (0.5 + 0.5 * log10(1)/3) = 0.1
        self.assertEqual(current[0].confidence, 0.1)

        lin = store.lineage(current[0].id)
        types = [a["type"] for a in lin["ancestors"]]
        self.assertEqual(types.count("paper"), 5)
        self.assertEqual(types.count("evidence.assessment"), 5)
        self.assertEqual(types.count("report"), 3)       # whole supersession chain
        self.assertEqual(lin["missing"], [])

    def test_state_survives_restart_and_entrypoints_reload(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.db")
            clock = FakeClock()
            orch, store, _ = build(path, clock)
            orch.discover(EXAMPLES)
            orch.tick()
            store.close()

            orch2, store2, _ = build(path, clock)        # fresh process equivalent: no functions bound
            self.assertEqual(store2.get_state("literature_monitor"), {"cursor": 2})
            clock.advance(60)
            res = orch2.tick()
            self.assertEqual([r.status for r in res.runs], ["success"] * 3)
            self.assertEqual(store2.get_state("literature_monitor"), {"cursor": 4})
            self.assertEqual(len(store2.query_claims(["paper"])), 4)
            store2.close()

    def test_replacing_a_service_implementation_keeps_the_rest_working(self):
        orch, store, clock = build()
        orch.discover(EXAMPLES)
        orch.tick()
        before = {c.id for c in store.query_claims(["evidence.assessment"])}

        def strict_evaluator(ctx):
            assessed = set(ctx.state.get("assessed", []))
            for claim in ctx.read("paper"):
                if claim.id in assessed:
                    continue
                ctx.write("evidence.assessment", {"paper_id": claim.content["id"], "design": "strict",
                                                  "n": claim.content["n"], "limitations": ["strict-v2"]},
                          confidence=0.5, inputs=[claim.id])
                assessed.add(claim.id)
            ctx.state["assessed"] = sorted(assessed)

        m = orch._manifests()["evidence_evaluator"]
        m.version = "2.0.0"
        orch.register(m, strict_evaluator)
        clock.advance(60)
        orch.tick()
        assessments = store.query_claims(["evidence.assessment"])
        self.assertEqual(len(assessments), 4)
        self.assertTrue(before <= {c.id for c in assessments})                 # old claims untouched
        self.assertEqual(sum(1 for c in assessments if c.content["limitations"] == ["strict-v2"]), 2)
        self.assertEqual(store.query_claims(["report"])[0].content["n_assessed"], 4)


if __name__ == "__main__":
    unittest.main()
