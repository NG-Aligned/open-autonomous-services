import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from helpers import EXAMPLES, ROOT, FakeClock, manifest

from oas import Orchestrator, Store
from oas.transport import pull, serve_in_thread, sync_dir
from test_cli import oas as run_cli


def node(path=":memory:"):
    store = Store(path, clock=FakeClock())
    return Orchestrator(store, sleep=lambda s: None), store


def produce(orch, service, n=1, type="paper"):
    orch.register(manifest(service, writes=[type]),
                  lambda ctx: [ctx.write(type, {"by": service, "i": i}) for i in range(n)])
    orch.run_service(service)


class DirSyncTests(unittest.TestCase):
    def test_three_nodes_converge_through_a_shared_directory(self):
        nodes = [node() for _ in range(3)]
        for _, s in nodes:
            self.addCleanup(s.close)
        for i, (o, _) in enumerate(nodes):
            produce(o, f"svc{i}", n=i + 1)
        with tempfile.TemporaryDirectory() as d:
            for _ in range(2):                                      # two rounds: publish then pick up
                for o, _ in nodes:
                    sync_dir(o, d)
            ids = [{c.id for c in s.query_claims(current_only=False)} for _, s in nodes]
            self.assertEqual(ids[0], ids[1])
            self.assertEqual(ids[1], ids[2])
            self.assertEqual(len(ids[0]), 1 + 2 + 3)
            files = sorted(f for f in os.listdir(d))
            self.assertEqual(len(files), 3)
            self.assertFalse([f for f in files if f.startswith(".tmp")])     # atomic publish leaves no temp files

    def test_resync_is_idempotent_and_skips_reverification_of_known_claims(self):
        (oa, sa), (ob, sb) = node(), node()
        self.addCleanup(sa.close)
        self.addCleanup(sb.close)
        produce(oa, "p", n=5)
        with tempfile.TemporaryDirectory() as d:
            sync_dir(oa, d)
            first = sync_dir(ob, d)["imported"]
            second = sync_dir(ob, d)["imported"]
            name = next(iter(first))
            self.assertEqual((first[name]["imported"], first[name]["duplicates"]), (5, 0))
            self.assertEqual((second[name]["imported"], second[name]["duplicates"]), (0, 5))

    def test_bad_files_are_reported_and_do_not_block_good_ones_and_policy_applies(self):
        (oa, sa), (ob, sb) = node(), node()
        self.addCleanup(sa.close)
        self.addCleanup(sb.close)
        produce(oa, "p")
        with tempfile.TemporaryDirectory() as d:
            sync_dir(oa, d)
            with open(os.path.join(d, "garbage.bundle.json"), "w") as f:
                f.write("{not json")
            with open(os.path.join(d, "wrong.bundle.json"), "w") as f:
                json.dump({"format": "nope"}, f)
            res = sync_dir(ob, d, require_trusted=True)["imported"]
            self.assertIn("error", res["garbage.bundle.json"])
            self.assertIn("error", res["wrong.bundle.json"])
            good = res[f"{sa.node_id}.bundle.json"]
            self.assertEqual(good["imported"], 0)                   # untrusted signer
            self.assertIn("not trusted", good["rejected"][0]["reason"])
            sb.add_trusted_signer(sa.public_key, "a", ["p"])
            res = sync_dir(ob, d, require_trusted=True)["imported"]
            self.assertEqual(res[f"{sa.node_id}.bundle.json"]["imported"], 1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.oa, self.sa = node()
        self.ob, self.sb = node()
        self.addCleanup(self.sa.close)
        self.addCleanup(self.sb.close)
        produce(self.oa, "p", n=3)
        self.server, self.thread = serve_in_thread(self.sa)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_pull_imports_verified_claims_and_supports_since_and_types(self):
        res = pull(self.ob, self.url)
        self.assertEqual((res["imported"], res["rejected"]), (3, []))
        self.assertEqual(self.sb.verify_claim(self.sb.query_claims()[0].id), "valid")
        again = pull(self.ob, self.url)
        self.assertEqual((again["imported"], again["duplicates"]), (0, 3))
        self.assertEqual(pull(self.ob, self.url, since=self.sa.max_seq())["imported"], 0)
        oc, sc = node()
        self.addCleanup(sc.close)
        self.assertEqual(pull(oc, self.url, types=["nothing"])["imported"], 0)
        self.assertEqual(pull(oc, self.url, types=["paper"])["imported"], 3)

    def test_whoami_and_read_only_and_errors(self):
        who = json.loads(urllib.request.urlopen(self.url + "/whoami", timeout=5).read())
        self.assertEqual(who["public_key"], self.sa.public_key)
        for method in ("POST", "PUT", "DELETE"):
            req = urllib.request.Request(self.url + "/bundle", data=b"{}", method=method)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 405)
        for path, code in (("/nope", 404), ("/bundle?since=abc", 400)):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(self.url + path, timeout=5)
            self.assertEqual(cm.exception.code, code)
        self.assertEqual(len(self.sb.query_claims()), 0)

    def test_pull_rejects_non_http_urls_and_applies_trust_policy(self):
        from oas import OASError
        for bad in ("file:///etc/passwd", "ftp://x", "/tmp/x"):
            with self.assertRaises(OASError):
                pull(self.ob, bad)
        res = pull(self.ob, self.url, require_trusted=True)
        self.assertEqual(res["imported"], 0)
        self.sb.add_trusted_signer(self.sa.public_key, "a", ["p"])
        self.assertEqual(pull(self.ob, self.url, require_trusted=True)["imported"], 3)


class TransportCliTests(unittest.TestCase):
    def test_sync_and_serve_pull_via_cli(self):
        with tempfile.TemporaryDirectory() as d:
            a, b, c = (os.path.join(d, n) for n in ("a.db", "b.db", "c.db"))
            shared = os.path.join(d, "shared")
            run_cli(a, "discover", EXAMPLES)
            run_cli(a, "tick")
            run_cli(a, "sync", shared)
            res = json.loads(run_cli(b, "sync", shared).stdout)
            self.assertEqual(sum(r["imported"] for r in res["imported"].values()), 5)
            self.assertEqual(len(json.loads(run_cli(b, "claims", "--type", "report").stdout)), 1)
            env = dict(os.environ, PYTHONPATH=ROOT)
            proc = subprocess.Popen([sys.executable, "-m", "oas", "--db", a, "serve", "--port", "0"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=ROOT, text=True)
            try:
                info = json.loads(proc.stdout.readline())
                self.assertTrue(info["serving"].startswith("http://127.0.0.1:"))
                # port 0 reports the real bound port in the URL
                self.assertNotEqual(info["serving"].rsplit(":", 1)[1], "0")
                res = json.loads(run_cli(c, "pull", info["serving"]).stdout)
                self.assertEqual((res["imported"], res["rejected"]), (5, []))
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()


class DaemonTests(unittest.TestCase):
    def start(self, db, *extra):
        env = dict(os.environ, PYTHONPATH=ROOT)
        return subprocess.Popen([sys.executable, "-m", "oas", "--db", db, "daemon", *extra], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, cwd=ROOT, text=True)

    def test_daemon_runs_due_services_then_stops_after_max_ticks(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "x.db")
            run_cli(db, "discover", EXAMPLES)
            p = self.start(db, "--interval", "0.1", "--max-ticks", "3")
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, msg=err)
            lines = [json.loads(line) for line in out.strip().splitlines()]
            self.assertEqual(lines[0]["daemon"], "started")
            self.assertEqual(lines[-1], {"daemon": "stopped", "ticks": 3})
            ran = [r["service"] for line in lines if "runs" in line for r in line["runs"]]
            self.assertEqual(ran, ["literature_monitor", "evidence_evaluator", "reporting_service"])   # first tick only
            self.assertEqual(len(json.loads(run_cli(db, "claims", "--type", "report").stdout)), 1)

    def test_daemon_stops_gracefully_on_sigterm(self):
        import signal
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "x.db")
            run_cli(db, "discover", EXAMPLES)
            p = self.start(db, "--interval", "0.2")
            first = json.loads(p.stdout.readline())
            self.assertEqual(first["daemon"], "started")
            time.sleep(0.6)
            p.send_signal(signal.SIGTERM)
            out, err = p.communicate(timeout=15)
            self.assertEqual(p.returncode, 0, msg=err)
            self.assertEqual(json.loads(out.strip().splitlines()[-1])["daemon"], "stopped")

    def test_daemon_survives_a_failing_service_and_keeps_ticking(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "x.db")
            with open(os.path.join(d, "boom.json"), "w") as f:
                json.dump({"id": "boom", "version": "1", "purpose": "always fails", "entrypoint": "nomod_xyz:fn",
                           "triggers": [{"kind": "interval", "seconds": 0.1}], "circuit_breaker": 100}, f)
            run_cli(db, "discover", d)
            p = self.start(db, "--interval", "0.15", "--max-ticks", "4")
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, msg=err)
            statuses = [r["status"] for line in map(json.loads, out.strip().splitlines()) if "runs" in line
                        for r in line["runs"]]
            self.assertGreaterEqual(len(statuses), 2)
            self.assertEqual(set(statuses), {"failed"})
