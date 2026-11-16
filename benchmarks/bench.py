"""Micro-benchmarks. Usage: python benchmarks/bench.py [claims] ; prints a markdown table of measured timings."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from oas import Claim, Orchestrator, Store, compute_claim_id, export_bundle  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
rows = []


def timed(label, fn, n=1):
    t = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t
    rows.append((label, dt, n))
    return out


def populate(store, n, types=("paper", "assessment", "report", "misc.a", "misc.b")):
    with store.transaction():
        for i in range(n):
            t = types[i % len(types)]
            content = {"i": i}
            cid = compute_claim_id(t, content, "bulk", [], 1.0, float(i), None)
            store.insert_claim(Claim(cid, t, content, "bulk", (), 1.0, float(i), None, store.node_id))


def main():
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "b.db"))
        orch = Orchestrator(store)
        # 1. write throughput through the real service path (includes signing + atomic commit)
        W = 1000
        orch.register({"id": "w", "version": "1", "purpose": "p", "writes": ["bulk"]},
                      lambda ctx: [ctx.write("bulk", {"i": i}) for i in range(W)])
        timed(f"service run writing {W} claims (signed, one transaction)", lambda: orch.run_service("w"), W)
        # 2. populate a large store with unsigned bulk claims (direct insert)
        timed(f"direct insert of {N} unsigned claims (one transaction)", lambda: populate(store, N), N)
        total = N + W
        timed(f"query literal type ('paper') over {total} claims", lambda: store.query_claims(["paper"]))
        timed(f"query wildcard type ('misc.*') over {total} claims", lambda: store.query_claims(["misc.*"]))
        timed("query literal type with limit=10", lambda: store.query_claims(["paper"], limit=10))
        timed("max_seq()", store.max_seq)
        # 3. event trigger evaluation cost with a big claim table
        orch.register({"id": "c", "version": "1", "purpose": "p", "reads": ["paper"],
                       "triggers": [{"kind": "event", "claim_type": "paper"}]}, lambda ctx: None)
        timed("due_reasons() for an event-triggered service", lambda: orch.due_reasons("c"))
        # 4. lineage over a supersession chain
        chain = 300
        prev = None
        with store.transaction():
            for i in range(chain):
                cid = compute_claim_id("chain", {"i": i}, "bulk", [], 1.0, 1e6 + i, prev)
                store.insert_claim(Claim(cid, "chain", {"i": i}, "bulk", (), 1.0, 1e6 + i, prev, store.node_id))
                prev = cid
        timed(f"lineage of a {chain}-deep supersession chain", lambda: store.lineage(prev), chain)
        # 5. tick with many services
        S = 100
        for i in range(S):
            orch.register({"id": f"svc{i:03d}", "version": "1", "purpose": "p",
                           "triggers": [{"kind": "interval", "seconds": 3600}]}, lambda ctx: None)
        res = timed(f"tick() running {S} due interval services", orch.tick, S)
        timed(f"tick() with nothing due ({S + 2} services registered)", orch.tick, S + 2)
        # 6. federation
        bundle = timed(f"export_bundle of {total + chain} claims", lambda: export_bundle(store), total + chain)
        node2 = Store(os.path.join(d, "b2.db"))
        small = {**bundle, "claims": [c for c in bundle["claims"] if c["signature"]][:300]}
        timed("import 300 signed claims (verifies each signature)", lambda: Orchestrator(node2).import_bundle(small), 300)
        unsigned = {**bundle, "claims": [c for c in bundle["claims"] if not c["signature"]][:5000]}
        timed("import 5000 unsigned claims", lambda: Orchestrator(node2).import_bundle(unsigned), 5000)
        store.close()
        node2.close()
    print("| Operation | Time | Per item |\n|---|---|---|")
    for label, dt, n in rows:
        per = f"{dt / n * 1000:.2f} ms" if n > 1 else "-"
        print(f"| {label} | {dt * 1000:.0f} ms | {per} |")


if __name__ == "__main__":
    main()
