"""Decentralised transports for exchanging claim bundles. Neither requires a central server.

* Directory sync: each node writes `<node_id>.bundle.json` into a shared directory and imports everyone else's.
  The directory can be anything that moves files: a git repository, Syncthing, a network share, a USB stick.
* HTTP: a node can serve a read-only bundle; another node pulls it. Plain HTTP, no authentication or encryption:
  bind to localhost or a trusted network only (claim contents are not confidential in transit). Integrity and
  authenticity still come from the claim ids and signatures checked on import, not from the transport.

Imports always go through Orchestrator.import_bundle, so schemas, signatures and the trusted-signer policy apply.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .errors import OASError
from .federation import BUNDLE_FORMAT, export_bundle
from .store import Store

BUNDLE_SUFFIX = ".bundle.json"
MAX_PULL_BYTES = 256 * 1024 * 1024


def sync_dir(orch, directory: str | Path, *, require_trusted: bool = False,
             require_signatures: bool = False) -> dict[str, Any]:
    """Publish this node's claims to `directory`, then import every other node's bundle found there."""
    store: Store = orch.store
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    bundle = export_bundle(store)
    target = d / f"{store.node_id}{BUNDLE_SUFFIX}"
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")      # atomic publish: readers never see a partial file
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(bundle, f, sort_keys=True)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    imported: dict[str, Any] = {}
    for p in sorted(d.glob(f"*{BUNDLE_SUFFIX}")):
        if p.name == target.name:
            continue
        try:
            other = json.loads(p.read_text(encoding="utf-8"))
            imported[p.name] = orch.import_bundle(other, require_trusted=require_trusted,
                                                  require_signatures=require_signatures)
        except (OSError, ValueError, OASError, RecursionError) as exc:   # one bad file must not stop the others
            imported[p.name] = {"error": f"{type(exc).__name__}: {exc}"}
    return {"published": str(target), "claims_published": len(bundle["claims"]), "imported": imported}


def _handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:    # keep tests/CLI quiet
            pass

        def _send(self, code: int, payload: Any) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:   # noqa: N802 - http.server API
            url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(url.query)
            if url.path == "/whoami":
                self._send(200, {"node_id": store.node_id, "public_key": store.public_key,
                                 "format": BUNDLE_FORMAT})
            elif url.path == "/bundle":
                try:
                    since = int(qs.get("since", ["0"])[0])
                except ValueError:
                    return self._send(400, {"error": "since must be an integer"})
                types = qs.get("type") or None
                self._send(200, export_bundle(store, types, since))
            else:
                self._send(404, {"error": "not found"})

        def _method_not_allowed(self) -> None:
            self._send(405, {"error": "read-only"})
        do_POST = do_PUT = do_DELETE = do_PATCH = _method_not_allowed   # noqa: N815

    return Handler


def make_server(store: Store, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Create (but do not start) a read-only bundle server. `server.server_address[1]` is the bound port."""
    return ThreadingHTTPServer((host, port), _handler(store))


def serve_in_thread(store: Store, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = make_server(store, host, port)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="oas-serve")
    t.start()
    return server, t


def pull(orch, url: str, *, since: int = 0, types: list[str] | None = None, timeout: float = 30.0,
         require_trusted: bool = False, require_signatures: bool = False) -> dict[str, Any]:
    """Fetch /bundle from another node and import it."""
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise OASError("pull URL must start with http:// or https://")
    q = urllib.parse.urlencode([("since", since), *[("type", t) for t in (types or [])]])
    with urllib.request.urlopen(url.rstrip("/") + "/bundle?" + q, timeout=timeout) as resp:   # noqa: S310 - scheme checked
        data = resp.read(MAX_PULL_BYTES + 1)
    if len(data) > MAX_PULL_BYTES:
        raise OASError("bundle exceeds the maximum accepted size")
    try:
        bundle = json.loads(data)
    except RecursionError:
        raise OASError("bundle is nested too deeply to parse") from None
    return orch.import_bundle(bundle, require_trusted=require_trusted, require_signatures=require_signatures)
