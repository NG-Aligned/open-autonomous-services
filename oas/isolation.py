"""Optional process isolation for services (manifest: "isolation": "process").

The service function runs in a child Python process. The child holds no database handle: every ServiceContext
call (read, write, send, inbox, ...) is an RPC to the parent, which executes it on the real, permission-checked
ServiceContext. A timeout genuinely kills the child, a crash only fails that run, and an optional memory limit
bounds the child.

Honest scope: the child runs as the same OS user, so malicious code could still open the database file or the
network directly. This isolates *failures and runaway execution*; it is not a hardened security sandbox.
Requires `entrypoint` ("module:function") because the child imports the function itself.

Wire protocol: JSON lines over a socketpair (the child's stdout is redirected to stderr so prints cannot corrupt it).
"""
from __future__ import annotations

import dataclasses
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

from . import errors as _errors
from .context import ServiceContext
from .errors import OASError, ServiceTimeout
from .store import Claim

_RPC = {"read", "get_claim", "write", "send", "inbox", "log", "now"}
MAX_LINE = 64 * 1024 * 1024
DEFAULT_TIMEOUT = 300.0   # isolated services always have a bound, so a hostile child cannot hang the parent forever


def _claim_to_json(c: Claim) -> dict[str, Any]:
    d = dataclasses.asdict(c)
    d["inputs"] = list(c.inputs)
    return d


def _claim_from_json(d: dict[str, Any]) -> Claim:
    d = dict(d)
    d["inputs"] = tuple(d["inputs"])
    return Claim(**d)


# ----------------------------------------------------------------------------------------------
# Parent side
# ----------------------------------------------------------------------------------------------
class _Channel:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""
        self.deadline: float | None = None     # set by the parent; sends must not outlive it

    def send(self, obj: Any) -> None:
        data = json.dumps(obj, allow_nan=False).encode("utf-8") + b"\n"
        if self.deadline is None:
            self.sock.sendall(data)
            return
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceTimeout("process exceeded its timeout and was killed")
        self.sock.settimeout(remaining)           # a child that stops reading must not block the parent in sendall
        try:
            self.sock.sendall(data)
        except TimeoutError:
            raise ServiceTimeout("process exceeded its timeout and was killed") from None
        finally:
            self.sock.settimeout(None)

    def recv(self, deadline: float | None, proc: subprocess.Popen) -> Any:
        """Return the next message, or None if the child closed the channel. Raises ServiceTimeout at deadline."""
        while b"\n" not in self.buf:
            wait = 0.5 if deadline is None else max(0.0, min(0.5, deadline - time.monotonic()))
            if deadline is not None and time.monotonic() >= deadline:
                raise ServiceTimeout("process exceeded its timeout and was killed")
            ready, _, _ = select.select([self.sock], [], [], wait)
            if ready:
                chunk = self.sock.recv(1 << 20)
                if not chunk:
                    return None
                self.buf += chunk
                if len(self.buf) > MAX_LINE:
                    raise OASError("child sent an oversized message")
            elif proc.poll() is not None and not select.select([self.sock], [], [], 0)[0]:
                return None
        line, _, self.buf = self.buf.partition(b"\n")
        return json.loads(line)


def run_in_process(ctx: ServiceContext, entrypoint: str, base_dir: str | None, timeout: float | None,
                   max_memory_mb: int | None) -> None:
    """Run the service in a child process, serving its context calls here. Updates ctx.state on success."""
    parent_sock, child_sock = socket.socketpair()
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (pkg_root, base_dir, env.get("PYTHONPATH")) if p)
    # stderr goes to a file, not a pipe: a chatty child (its prints are redirected to stderr) would otherwise
    # fill the pipe buffer and block forever because nothing reads it while the service runs.
    err_file = tempfile.TemporaryFile()
    proc = subprocess.Popen([sys.executable, "-m", "oas.isolation", str(child_sock.fileno())],
                            pass_fds=[child_sock.fileno()], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=err_file)
    child_sock.close()
    chan = _Channel(parent_sock)
    deadline = None if timeout is None else time.monotonic() + timeout
    chan.deadline = deadline
    try:
        chan.send({"init": {"entrypoint": entrypoint, "base_dir": base_dir, "service_id": ctx.service_id,
                            "config": ctx.config, "state": ctx.state, "trigger": ctx.trigger,
                            "max_memory_mb": max_memory_mb}})
        while True:
            msg = chan.recv(deadline, proc)
            if msg is None:
                code = proc.wait(timeout=5)
                err_file.seek(0, os.SEEK_END)
                err_file.seek(max(0, err_file.tell() - 4096))
                err = err_file.read().decode("utf-8", "replace").strip().splitlines()
                raise OASError(f"service process exited with code {code} before finishing"
                               + (f": {err[-1][:300]}" if err else ""))
            if "done" in msg:
                ctx.state.clear()
                ctx.state.update(msg["done"])
                return
            if "error" in msg:
                raise OASError(msg["error"])
            name = msg.get("call")
            if name not in _RPC:
                chan.send({"err": ["OASError", f"unknown call {name!r}"]})
                continue
            try:
                result = getattr(ctx, name)(*msg.get("args", []), **msg.get("kwargs", {}))
                if name == "read":
                    result = [_claim_to_json(c) for c in result]
                elif name == "get_claim":
                    result = _claim_to_json(result) if result is not None else None
                chan.send({"ok": result})
            except Exception as exc:  # noqa: BLE001 - relayed to the child, which may handle or re-raise it
                chan.send({"err": [type(exc).__name__, str(exc)]})
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        finally:
            parent_sock.close()
            err_file.close()


# ----------------------------------------------------------------------------------------------
# Child side
# ----------------------------------------------------------------------------------------------
class _ProxyContext:
    def __init__(self, chan: _Channel, init: dict[str, Any]) -> None:
        self._chan = chan
        self.service_id = init["service_id"]
        self.config = init["config"]
        self.state = init["state"]
        self.trigger = init["trigger"]

    def _rpc(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self._chan.send({"call": name, "args": list(args), "kwargs": kwargs})
        reply = self._chan.recv(None, _NoProc())
        if reply is None:
            raise OASError("parent closed the channel")
        if "err" in reply:
            cls = getattr(_errors, reply["err"][0], None)
            raise (cls if isinstance(cls, type) and issubclass(cls, Exception) else OASError)(reply["err"][1])
        return reply["ok"]

    def read(self, claim_type: str, **kw: Any) -> list[Claim]:
        return [_claim_from_json(d) for d in self._rpc("read", claim_type, **kw)]

    def get_claim(self, claim_id: str) -> Claim | None:
        d = self._rpc("get_claim", claim_id)
        return _claim_from_json(d) if d else None

    def write(self, claim_type: str, content: Any, **kw: Any) -> str:
        if "inputs" in kw:
            kw["inputs"] = list(kw["inputs"])
        return self._rpc("write", claim_type, content, **kw)

    def send(self, to_service: str, topic: str, payload: Any = None) -> None:
        self._rpc("send", to_service, topic, payload)

    def inbox(self) -> list[dict[str, Any]]:
        return self._rpc("inbox")

    def log(self, message: str) -> None:
        self._rpc("log", str(message))

    def now(self) -> float:
        return self._rpc("now")


class _NoProc:
    def poll(self) -> None:
        return None


def _child_main(fd: int) -> int:
    import importlib

    sys.stdout = sys.stderr   # user prints must not corrupt the protocol channel
    sock = socket.socket(fileno=fd)
    chan = _Channel(sock)
    init = chan.recv(None, _NoProc())["init"]
    if init.get("max_memory_mb"):
        try:
            import resource
            limit = int(init["max_memory_mb"]) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ImportError, ValueError, OSError):
            pass
    try:
        if init.get("base_dir") and init["base_dir"] not in sys.path:
            sys.path.insert(0, init["base_dir"])
        mod_name, _, attr = init["entrypoint"].partition(":")
        fn = getattr(importlib.import_module(mod_name), attr)
        ctx = _ProxyContext(chan, init)
        fn(ctx)
        chan.send({"done": ctx.state})
        return 0
    except BaseException as exc:  # noqa: BLE001 - everything is reported to the parent
        try:
            chan.send({"error": f"{type(exc).__name__}: {exc}"})
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(_child_main(int(sys.argv[1])))
