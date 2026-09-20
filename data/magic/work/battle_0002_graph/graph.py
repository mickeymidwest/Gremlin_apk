"""A tiny directed-graph toolkit. Each function has a bug that one of the
tests in test_graph.py pins down. Fix the bodies; keep the signatures;
don't touch the tests.
"""
from collections import deque


class Graph():
    def __init__(self):
        self.nodes = set()
        self.edges = {}

