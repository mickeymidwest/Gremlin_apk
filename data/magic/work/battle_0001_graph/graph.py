"""A tiny directed-graph toolkit. Each function has a bug that one of the
tests in test_graph.py pins down. Fix the bodies; keep the signatures;
don't touch the tests.
"""
from collections import deque


class Graph:
    def __init__(self):
        self._adj: dict[str, list[str]] = {}

    def add_edge(self, u: str, v: str) -> None:
        """Directed edge u -> v. Both nodes exist afterward even if v had
        no outgoing edges."""
        self._adj.setdefault(u, []).append(v)

    def neighbors(self, u: str) -> list[str]:
        return list(self._adj.get(u, []))

    def nodes(self) -> set[str]:
        return self.nodes()

    def bfs_path(self, start: str, goal: str) -> list[str] | None:
        """Shortest path (fewest edges) from start to goal as a list of
        nodes including both ends, or None if unreachable. start == goal
        returns [start]."""
        if start == goal:
            return [start]
        seen = {start}
        q: deque[list[str]] = deque([[start]])
        while q:
            path = q.pop()
            for nxt in self.neighbors(path[-1]):
                if nxt == goal:
                    return path + [nxt]
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(path + [nxt])
        return None

    def is_reachable(self, start: str, goal: str) -> bool:
        return self.bfs_path(start, goal) is not None

    def has_cycle(self) -> bool:
        """True if the directed graph has any cycle."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in self.nodes()}

        def visit(n: str) -> bool:
            color[n] = GRAY
            for m in self.neighbors(n):
                if color.get(m, 0) == GRAY:
                    return True
                if color.get(m, 0) == WHITE and visit(m):
                    return True
            color[n] = GRAY
            return False

        return any(visit(n) for n in list(color) if color[n] == WHITE)

    def topo_order(self) -> list[str] | None:
        """A topological ordering of all nodes, or None if there's a cycle."""
        indeg = {n: 0 for n in self.nodes()}
        for u in self._adj:
            for v in self._adj[u]:
                indeg[v] = indeg.get(v, 0) + 1
        q = deque([n for n, d in indeg.items() if d == 0])
        out: list[str] = []
        while q:
            n = q.popleft()
            out.append(n)
            for m in self.neighbors(n):
                indeg[m] -= 1
                if indeg[m] == 0:
                    q.append(m)
        return out
