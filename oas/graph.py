"""Dependency graph derived purely from manifests (no central registry needed)."""
from __future__ import annotations

from fnmatch import fnmatchcase

from .manifest import Manifest


def types_overlap(a: str, b: str) -> bool:
    return a == b or fnmatchcase(a, b) or fnmatchcase(b, a)


def build_edges(manifests: dict[str, Manifest]) -> dict[tuple[str, str], set[str]]:
    """Edges point from producer to consumer. Value = reasons for the edge."""
    edges: dict[tuple[str, str], set[str]] = {}

    def add(a: str, b: str, why: str) -> None:
        if a != b:
            edges.setdefault((a, b), set()).add(why)

    for w in manifests.values():
        for r in manifests.values():
            if w.id == r.id:
                continue
            for wt in w.writes:
                for rt in r.reads:
                    if types_overlap(wt, rt):
                        add(w.id, r.id, f"claim:{wt}")
    for m in manifests.values():
        for t in m.triggers:
            if t["kind"] == "dependency" and t["service"] in manifests:
                add(t["service"], m.id, "dependency")
        for target in m.can_message:
            for other in manifests:
                if fnmatchcase(other, target):
                    add(m.id, other, "message")
    return edges


def topo_order(nodes: list[str], edges: dict[tuple[str, str], set[str]]) -> tuple[list[str], list[str]]:
    """Kahn's algorithm with deterministic tie-breaking. Returns (order, nodes_left_in_cycles)."""
    indeg = {n: 0 for n in nodes}
    out: dict[str, list[str]] = {n: [] for n in nodes}
    for (a, b) in edges:
        if a in indeg and b in indeg:
            out[a].append(b)
            indeg[b] += 1
    ready = sorted(n for n, d in indeg.items() if d == 0)
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in sorted(out[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
        ready.sort()
    leftover = sorted(n for n in nodes if n not in set(order))
    return order + leftover, leftover


def find_cycles(nodes: list[str], edges: dict[tuple[str, str], set[str]]) -> list[list[str]]:
    """Strongly connected components of size > 1 (Tarjan)."""
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for (a, b) in edges:
        if a in adj and b in adj:
            adj[a].append(b)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    result: list[list[str]] = []
    counter = [0]

    def visit(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in adj[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                result.append(sorted(comp))

    for n in sorted(nodes):
        if n not in index:
            visit(n)
    return sorted(result)
