import unittest

from helpers import FakeClock, make_orch, manifest

from oas import ManifestError, Orchestrator, Store, export_bundle
from oas import schema as S

PAPER = {"type": "object", "required": ["id", "n"], "additionalProperties": False,
         "properties": {"id": {"type": "string", "minLength": 1}, "n": {"type": "integer", "minimum": 0},
                        "design": {"enum": ["rct", "cohort"]}}}


class ValidatorTests(unittest.TestCase):
    def test_valid_and_each_violation_kind(self):
        ok = {"id": "p1", "n": 5, "design": "rct"}
        self.assertEqual(S.validate(ok, PAPER), [])
        bad = [
            ("not an object", "expected object"),
            ({"n": 1}, "missing required property 'id'"),
            ({"id": "", "n": 1}, "shorter than"),
            ({"id": "a", "n": -1}, "below minimum"),
            ({"id": "a", "n": 1.5}, "expected integer"),
            ({"id": "a", "n": True}, "expected integer"),
            ({"id": "a", "n": 1, "design": "x"}, "must be one of"),
            ({"id": "a", "n": 1, "extra": 1}, "unexpected property"),
        ]
        for value, fragment in bad:
            errs = S.validate(value, PAPER)
            self.assertTrue(errs and fragment in errs[0], msg=f"{value!r}: {errs}")

    def test_arrays_nested_paths_type_lists_const_bounds(self):
        sch = {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": ["string", "null"], "maxLength": 3}}
        self.assertEqual(S.validate(["ab", None], sch), [])
        self.assertIn("fewer than", S.validate([], sch)[0])
        self.assertIn("more than", S.validate(["a", "b", "c"], sch)[0])
        self.assertIn("$[0]", S.validate([5], sch)[0])
        self.assertIn("longer than", S.validate(["abcd"], sch)[0])
        self.assertIn("must equal", S.validate(2, {"const": 1})[0])
        self.assertIn("above maximum", S.validate(11, {"maximum": 10})[0])
        self.assertEqual(S.validate(1.5, {"type": "number"}), [])

    def test_check_schema_rejects_malformed(self):
        for bad in ("x", {"typo": 1}, {"type": "wat"}, {"enum": []}, {"required": "a"}, {"minItems": -1},
                    {"additionalProperties": "no"}, {"properties": {"a": {"type": "wat"}}}, {"minimum": "1"}):
            self.assertTrue(S.check_schema(bad), msg=str(bad))
        self.assertEqual(S.check_schema(PAPER), [])


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.orch, self.store, self.clock, _ = make_orch()
        self.addCleanup(self.store.close)

    def test_manifest_rejects_bad_schema_and_unrelated_type(self):
        with self.assertRaises(ManifestError):
            self.orch.register(manifest("a", writes=["paper"], claim_schemas={"paper": {"type": "wat"}}), lambda c: None)
        with self.assertRaises(ManifestError):
            self.orch.register(manifest("b", writes=["paper"], claim_schemas={"other": {"type": "object"}}), lambda c: None)

    def test_write_validated_and_violation_audited_nothing_committed(self):
        def fn(ctx):
            ctx.write("paper", ctx.config["content"])
        self.orch.register(manifest("w", writes=["paper"], claim_schemas={"paper": PAPER},
                                    config={"content": {"id": "p1", "n": 3}}), fn)
        self.assertEqual(self.orch.run_service("w").status, "success")
        self.orch.register(manifest("w", writes=["paper"], claim_schemas={"paper": PAPER},
                                    config={"content": {"id": "p2", "n": "many"}}), fn)
        res = self.orch.run_service("w")
        self.assertEqual(res.status, "failed")
        self.assertIn("violates its schema", res.error)
        self.assertEqual(len(self.store.query_claims(["paper"])), 1)
        self.assertIn("schema_violation", [a["action"] for a in self.store.list_audit("w")])

    def test_wildcard_schema_key_applies(self):
        self.orch.register(manifest("w", writes=["m.*"], claim_schemas={"m.*": {"type": "object"}}),
                           lambda ctx: ctx.write("m.x", [1]))
        self.assertIn("violates its schema", self.orch.run_service("w").error)

    def test_import_rejects_content_violating_local_schema(self):
        other = Orchestrator(Store(":memory:", clock=FakeClock()), sleep=lambda s: None)
        self.addCleanup(other.store.close)
        other.register(manifest("remote", writes=["paper"]), lambda ctx: (ctx.write("paper", {"id": "p1", "n": 1}),
                                                                         ctx.write("paper", {"id": "p2", "n": "bad"})))
        other.run_service("remote")
        bundle = export_bundle(other.store)
        self.orch.register(manifest("local", reads=["paper"], claim_schemas={"paper": PAPER}), lambda ctx: None)
        res = self.orch.import_bundle(bundle)
        self.assertEqual(res["imported"], 1)
        self.assertEqual(len(res["rejected"]), 1)
        self.assertIn("violates local schema", res["rejected"][0]["reason"])

    def test_validate_warns_when_services_disagree_on_a_schema(self):
        self.orch.register(manifest("w", writes=["paper"], claim_schemas={"paper": PAPER}), lambda c: None)
        self.orch.register(manifest("r", reads=["paper"], claim_schemas={"paper": {"type": "object"}}), lambda c: None)
        msgs = [i["message"] for i in self.orch.validate()]
        self.assertTrue(any("different schemas for claim type 'paper'" in m for m in msgs))
        self.orch.register(manifest("r", reads=["paper"], claim_schemas={"paper": PAPER}), lambda c: None)
        msgs = [i["message"] for i in self.orch.validate()]
        self.assertFalse(any("different schemas" in m for m in msgs))


if __name__ == "__main__":
    unittest.main()
