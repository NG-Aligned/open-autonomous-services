"""Command line interface: `python -m oas --db PATH <command>`."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from typing import Any

from .federation import export_bundle, import_bundle, read_bundle, write_bundle
from .manifest import load_manifest
from .orchestrator import Orchestrator
from .store import Store


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oas", description="Open Autonomous Services")
    p.add_argument("--db", default=os.environ.get("OAS_DB", "oas.db"), help="SQLite file (default: oas.db)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the database and print the node id")
    r = sub.add_parser("register", help="register manifest files")
    r.add_argument("manifests", nargs="+")
    d = sub.add_parser("discover", help="register every manifest in a directory")
    d.add_argument("directory")
    sub.add_parser("list", help="list services")
    t = sub.add_parser("tick", help="run everything that is due")
    t.add_argument("--max-rounds", type=int, default=10)
    rn = sub.add_parser("run", help="run one service now")
    rn.add_argument("service")
    c = sub.add_parser("claims", help="list claims")
    c.add_argument("--type", action="append")
    c.add_argument("--all", action="store_true", help="include superseded claims")
    ln = sub.add_parser("lineage", help="show the provenance ancestry of a claim")
    ln.add_argument("claim_id")
    sub.add_parser("graph", help="show dependency graph, order and cycles")
    sub.add_parser("validate", help="report manifest/graph issues")
    rs = sub.add_parser("runs", help="show run history")
    rs.add_argument("--service")
    au = sub.add_parser("audit", help="show audit log")
    au.add_argument("--service")
    for name in ("enable", "disable"):
        e = sub.add_parser(name)
        e.add_argument("service")
    ex = sub.add_parser("export", help="export claims to a bundle file")
    ex.add_argument("out")
    ex.add_argument("--type", action="append")
    im = sub.add_parser("import", help="import a claim bundle")
    im.add_argument("bundle")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.db)
    orch = Orchestrator(store)
    try:
        if args.cmd == "init":
            _print({"db": args.db, "node_id": store.node_id})
        elif args.cmd == "register":
            out = []
            for path in args.manifests:
                m = load_manifest(path)
                orch.register(m, base_dir=os.path.dirname(os.path.abspath(path)))
                out.append(m.id)
            _print({"registered": out})
        elif args.cmd == "discover":
            ok, errors = orch.discover(args.directory)
            _print({"registered": ok, "errors": errors})
        elif args.cmd == "list":
            _print([{"id": s["id"], "version": s["manifest"]["version"], "enabled": s["enabled"],
                     "consecutive_failures": s["consecutive_failures"], "disabled_reason": s["disabled_reason"]}
                    for s in store.list_services()])
        elif args.cmd == "tick":
            res = orch.tick(max_rounds=args.max_rounds)
            _print({"rounds": res.rounds, "truncated": res.truncated,
                    "runs": [{k: v for k, v in asdict(r).items() if k != "logs"} for r in res.runs]})
        elif args.cmd == "run":
            _print(asdict(orch.run_service(args.service)))
        elif args.cmd == "claims":
            _print([c.to_wire() | {"seq": c.seq} for c in
                    store.query_claims(args.type, current_only=not args.all)])
        elif args.cmd == "lineage":
            _print(store.lineage(args.claim_id))
        elif args.cmd == "graph":
            _print(orch.graph())
        elif args.cmd == "validate":
            _print(orch.validate())
        elif args.cmd == "runs":
            _print(store.list_runs(args.service))
        elif args.cmd == "audit":
            _print(store.list_audit(args.service))
        elif args.cmd == "enable":
            orch.enable(args.service)
            _print({"enabled": args.service})
        elif args.cmd == "disable":
            orch.disable(args.service)
            _print({"disabled": args.service})
        elif args.cmd == "export":
            bundle = export_bundle(store, args.type)
            write_bundle(bundle, args.out)
            _print({"exported": len(bundle["claims"]), "node_id": store.node_id})
        elif args.cmd == "import":
            _print(import_bundle(store, read_bundle(args.bundle)))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
