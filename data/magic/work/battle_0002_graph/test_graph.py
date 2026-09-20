from graph import Graph


def _diamond():
    g = Graph()
    # short route A->E->G ; long route A->B->C->D->G
    g.add_edge("A", "E")
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    g.add_edge("C", "D")
    g.add_edge("D", "G")
    g.add_edge("E", "G")
    return g


def test_nodes_includes_sink_nodes():
    g = Graph()
    g.add_edge("a", "b")
    assert g.nodes() == {"a", "b"}


def test_bfs_returns_the_shortest_path():
    g = _diamond()
    assert g.bfs_path("A", "G") == ["A", "E", "G"]


def test_bfs_same_node():
    g = _diamond()
    assert g.bfs_path("G", "G") == ["G"]


def test_bfs_unreachable_is_none():
    g = Graph()
    g.add_edge("a", "b")
    g.add_edge("c", "d")
    assert g.bfs_path("a", "d") is None


def test_reachable():
    g = _diamond()
    assert g.is_reachable("A", "G") is True
    assert g.is_reachable("G", "A") is False


def test_has_cycle_true():
    g = Graph()
    g.add_edge("x", "y")
    g.add_edge("y", "z")
    g.add_edge("z", "x")
    assert g.has_cycle() is True


def test_has_cycle_false_on_a_dag():
    g = _diamond()
    assert g.has_cycle() is False


def test_topo_order_is_a_valid_ordering():
    g = _diamond()
    order = g.topo_order()
    assert order is not None
    pos = {n: i for i, n in enumerate(order)}
    assert set(order) == g.nodes()
    for u in g.nodes():
        for v in g.neighbors(u):
            assert pos[u] < pos[v]


def test_topo_order_none_on_cycle():
    g = Graph()
    g.add_edge("x", "y")
    g.add_edge("y", "x")
    assert g.topo_order() is None
