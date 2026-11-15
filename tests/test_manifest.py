import json
import tempfile
import unittest
from pathlib import Path

from helpers import manifest

from oas import Manifest, ManifestError, load_manifest
from oas.manifest import matches


class ManifestTests(unittest.TestCase):
    def test_minimal_valid(self):
        m = Manifest.from_dict(manifest())
        self.assertEqual(m.id, "svc")
        self.assertEqual(m.retry, {"max_attempts": 1, "backoff_seconds": 0.0})
        self.assertEqual(m.continuity, "persist")

    def test_missing_required_fields(self):
        for key in ("id", "version", "purpose"):
            d = manifest()
            del d[key]
            with self.assertRaises(ManifestError):
                Manifest.from_dict(d)

    def test_bad_ids(self):
        for bad in ("Bad", "1abc", "has space", "", "a" * 65):
            with self.assertRaises(ManifestError, msg=bad):
                Manifest.from_dict(manifest(id=bad))

    def test_unknown_key_rejected(self):
        with self.assertRaises(ManifestError):
            Manifest.from_dict(manifest(colour="red"))

    def test_trigger_validation(self):
        bad = [
            [{"kind": "nope"}],
            [{"kind": "interval"}],
            [{"kind": "interval", "seconds": 0}],
            [{"kind": "interval", "seconds": True}],
            [{"kind": "event"}],
            [{"kind": "dependency"}],
            ["interval"],
        ]
        for triggers in bad:
            with self.assertRaises(ManifestError, msg=str(triggers)):
                Manifest.from_dict(manifest(triggers=triggers))
        good = [{"kind": "interval", "seconds": 5}, {"kind": "event", "claim_type": "x"},
                {"kind": "dependency", "service": "other"}, {"kind": "message"}, {"kind": "manual"}]
        self.assertEqual(len(Manifest.from_dict(manifest(triggers=good)).triggers), 5)

    def test_config_schema_defaults_required_types(self):
        schema = {"n": {"type": "int", "default": 3}, "name": {"type": "str", "required": True}}
        with self.assertRaises(ManifestError):  # required missing
            Manifest.from_dict(manifest(config_schema=schema))
        m = Manifest.from_dict(manifest(config_schema=schema, config={"name": "x"}))
        self.assertEqual(m.resolve_config(), {"name": "x", "n": 3})
        with self.assertRaises(ManifestError):  # wrong type
            Manifest.from_dict(manifest(config_schema=schema, config={"name": 5}))
        with self.assertRaises(ManifestError):  # bool is not int
            Manifest.from_dict(manifest(config_schema=schema, config={"name": "x", "n": True}))
        with self.assertRaises(ManifestError):  # unknown key when schema present
            Manifest.from_dict(manifest(config_schema=schema, config={"name": "x", "extra": 1}))

    def test_retry_continuity_timeout_breaker_validation(self):
        for kw in ({"retry": {"max_attempts": 0}}, {"retry": {"backoff_seconds": -1}},
                    {"retry": {"bogus": 1}}, {"continuity": "forever"}, {"timeout_seconds": 0},
                    {"circuit_breaker": 0}, {"entrypoint": "nocolon"}):
            with self.assertRaises(ManifestError, msg=str(kw)):
                Manifest.from_dict(manifest(**kw))

    def test_load_json_and_toml(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.json").write_text(json.dumps(manifest("a")))
            (Path(d) / "b.toml").write_text('id = "b"\nversion = "2"\npurpose = "toml one"\nreads = ["x"]\n')
            (Path(d) / "c.yaml").write_text("id: c")
            self.assertEqual(load_manifest(Path(d) / "a.json").id, "a")
            self.assertEqual(load_manifest(Path(d) / "b.toml").reads, ["x"])
            with self.assertRaises(ManifestError):
                load_manifest(Path(d) / "c.yaml")

    def test_wildcard_matching(self):
        self.assertTrue(matches(["paper.*"], "paper.new"))
        self.assertFalse(matches(["paper.*"], "report"))
        self.assertTrue(matches(["*"], "anything"))


if __name__ == "__main__":
    unittest.main()
