"""Command line interface: `python -m oas --db PATH <command>`."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from typing import Any

from .federation import export_bundle, read_bundle, write_bundle
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
    im.add_argument("--require-signatures", action="store_true", help="reject unsigned claims")
    im.add_argument("--require-trusted", action="store_true",
                    help="only accept claims signed by trusted signers for their allowed services")
    pr = sub.add_parser("prune", help="delete old runs/audit/messages (and optionally superseded claims)")
    pr.add_argument("--keep-runs", type=int, help="keep the newest N runs per service (plus latest success)")
    pr.add_argument("--audit-older-than", type=float, metavar="SECONDS")
    pr.add_argument("--messages-older-than", type=float, metavar="SECONDS", help="consumed messages only")
    pr.add_argument("--superseded-older-than", type=float, metavar="SECONDS",
                    help="delete superseded claims; lineage will then report them missing")
    sy = sub.add_parser("sync", help="exchange claims through a shared directory (git/Syncthing/share/USB)")
    sy.add_argument("directory")
    sy.add_argument("--require-signatures", action="store_true")
    sy.add_argument("--require-trusted", action="store_true")
    sv = sub.add_parser("serve", help="serve this node's claims read-only over HTTP (trusted networks only)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    pl = sub.add_parser("pull", help="import claims from another node's `oas serve`")
    pl.add_argument("url")
    pl.add_argument("--since", type=int, default=0)
    pl.add_argument("--type", action="append")
    pl.add_argument("--require-signatures", action="store_true")
    pl.add_argument("--require-trusted", action="store_true")
    sub.add_parser("whoami", help="print this node's id and public signing key")
    tr = sub.add_parser("trust", help="manage trusted signers")
    tsub = tr.add_subparsers(dest="trust_cmd", required=True)
    ta = tsub.add_parser("add")
    ta.add_argument("pubkey")
    ta.add_argument("--label", required=True)
    ta.add_argument("--service", action="append", required=True, help="allowed source_service pattern (repeatable)")
    tsub.add_parser("list")
    trm = tsub.add_parser("remove")
    trm.add_argument("pubkey")
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
            _print(orch.import_bundle(read_bundle(args.bundle), require_trusted=args.require_trusted,
                                      require_signatures=args.require_signatures))
        elif args.cmd == "prune":
            out = {}
            if args.keep_runs is not None:
                out["runs_deleted"] = store.prune_runs(args.keep_runs)
            if args.audit_older_than is not None:
                out["audit_deleted"] = store.prune_audit(args.audit_older_than)
            if args.messages_older_than is not None:
                out["messages_deleted"] = store.prune_messages(args.messages_older_than)
            if args.superseded_older_than is not None:
                out["superseded_claims_deleted"] = store.prune_superseded_claims(args.superseded_older_than)
            _print(out)
        elif args.cmd == "sync":
            from .transport import sync_dir
            _print(sync_dir(orch, args.directory, require_trusted=args.require_trusted,
                            require_signatures=args.require_signatures))
        elif args.cmd == "serve":
            from .transport import make_server
            server = make_server(store, args.host, args.port)
            print(json.dumps({"serving": f"http://{args.host}:{server.server_address[1]}", "node_id": store.node_id}),
                  flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        elif args.cmd == "pull":
            from .transport import pull
            _print(pull(orch, args.url, since=args.since, types=args.type, require_trusted=args.require_trusted,
                        require_signatures=args.require_signatures))
        elif args.cmd == "whoami":
            _print({"node_id": store.node_id, "public_key": store.public_key})
        elif args.cmd == "trust":
            if args.trust_cmd == "add":
                store.add_trusted_signer(args.pubkey, args.label, args.service)
                _print({"trusted": args.pubkey, "label": args.label, "services": args.service})
            elif args.trust_cmd == "list":
                _print(store.trusted_signers())
            else:
                _print({"removed": store.remove_trusted_signer(args.pubkey)})
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
