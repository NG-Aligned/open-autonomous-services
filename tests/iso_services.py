"""Service functions run inside isolated child processes (imported by the child via the manifest entrypoint)."""
import os
import time

from oas import PermissionDenied


def writer(ctx):
    ctx.write("out", {"n": ctx.state.get("n", 0) + 1, "cfg": ctx.config.get("tag")}, confidence=0.7)
    ctx.state["n"] = ctx.state.get("n", 0) + 1
    ctx.log("wrote from child")


def reader(ctx):
    claims = ctx.read("data")
    first = ctx.get_claim(claims[0].id) if claims else None
    ctx.write("derived", {"count": len(claims), "same": first is not None and first.id == claims[0].id},
              inputs=[c.id for c in claims])


def forbidden(ctx):
    ctx.write("secret", 1)


def catches(ctx):
    try:
        ctx.write("secret", 1)
    except PermissionDenied as exc:
        ctx.write("out", {"caught": type(exc).__name__})


def hang(ctx):
    with open(ctx.config["pid_file"], "w") as f:
        f.write(str(os.getpid()))
    time.sleep(60)
    ctx.write("out", "should never be written")


def crash(ctx):
    ctx.write("out", "buffered then lost")
    os._exit(7)


def hog(ctx):
    blob = bytearray(600 * 1024 * 1024)
    ctx.write("out", len(blob))


def noisy(ctx):
    print("this must not corrupt the protocol")
    ctx.write("out", {"ok": True})


def messenger(ctx):
    got = [m["payload"] for m in ctx.inbox()]
    ctx.send("peer", "hello", {"got": got})
    ctx.write("out", {"received": got})


def raises(ctx):
    raise ValueError("child exploded")
