import json
import os
import subprocess
import sys
import tempfile
import unittest

from helpers import EXAMPLES, ROOT


def oas(db, *args, check=True):
    env = dict(os.environ, PYTHONPATH=ROOT)
    p = subprocess.run([sys.executable, "-m", "oas", "--db", db, *args], capture_output=True, text=True,
                       env=env, cwd=ROOT, timeout=60)
    if check and p.returncode != 0:
        raise AssertionError(f"oas {args} failed: {p.stderr}")
    return p


class CliTests(unittest.TestCase):
    def test_end_to_end_via_cli_and_two_node_exchange(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.db"), os.path.join(d, "b.db")
            self.assertTrue(json.loads(oas(a, "init").stdout)["node_id"])
            found = json.loads(oas(a, "discover", EXAMPLES).stdout)
            self.assertEqual(len(found["registered"]), 3)
            self.assertEqual(found["errors"], {})
            self.assertEqual(len(json.loads(oas(a, "list").stdout)), 3)
            tick = json.loads(oas(a, "tick").stdout)
            self.assertEqual([r["status"] for r in tick["runs"]], ["success"] * 3)
            self.assertFalse(tick["truncated"])
            claims = json.loads(oas(a, "claims", "--type", "report").stdout)
            self.assertEqual(len(claims), 1)
            lin = json.loads(oas(a, "lineage", claims[0]["id"]).stdout)
            self.assertEqual(len(lin["ancestors"]), 5)  # report + 2 assessments + 2 papers
            graph = json.loads(oas(a, "graph").stdout)
            self.assertEqual(graph["cycles"], [])
            self.assertEqual(len(json.loads(oas(a, "runs", "--service", "literature_monitor").stdout)), 1)
            bundle = os.path.join(d, "bundle.json")
            self.assertEqual(json.loads(oas(a, "export", bundle).stdout)["exported"], 5)
            oas(b, "init")
            imp = json.loads(oas(b, "import", bundle).stdout)
            self.assertEqual((imp["imported"], imp["rejected"]), (5, []))
            self.assertEqual(len(json.loads(oas(b, "claims", "--type", "report").stdout)), 1)

    def test_errors_exit_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            p = oas(os.path.join(d, "x.db"), "run", "ghost", check=False)
            self.assertEqual(p.returncode, 1)
            self.assertIn("ServiceNotFound", p.stderr)


if __name__ == "__main__":
    unittest.main()
