"""R4 memory retrieval: relevant facts surface, newest always kept, budget held."""
from gremlin_core import memory_select as ms

FACTS = [
    "Mickey's desktop is a Dell G5 with an RTX 2070 Super",          # 0 oldest
    "Mickey runs a Jellyfin media server in Docker",                 # 1
    "Mickey likes dark humor and swearing",                          # 2
    "Mickey is learning Kotlin for Android apps",                    # 3
    "Mickey prefers short, direct answers",                          # 4 newest
]


def test_relevant_old_fact_surfaces():
    out = ms.select(FACTS, "how much vram does my desktop gpu have?", recent=2, matched=3)
    assert "Dell G5 with an RTX 2070 Super" in " ".join(out)       # old fact 0 retrieved
    assert "prefers short, direct answers" in " ".join(out)        # newest still kept


def test_newest_kept_even_when_irrelevant():
    out = ms.select(FACTS, "what's the weather like", recent=2, matched=3)
    assert FACTS[3] in out and FACTS[4] in out                     # 2 newest always present


def test_jellyfin_query_pulls_the_docker_fact():
    out = ms.select(FACTS, "is my jellyfin container running?", recent=1, matched=2)
    assert any("Jellyfin" in o for o in out)


def test_empty_and_greeting():
    assert ms.select([], "hi") == []
    out = ms.select(FACTS, "hey", recent=2, matched=3)             # no informative tokens
    assert out == FACTS[-2:]                                       # just the newest two


def test_budget_drops_oldest_first():
    big = [f"fact number {i} " + "x" * 200 for i in range(20)]
    out = ms.select(big, "fact number 0", recent=3, matched=5, max_chars=600)
    assert sum(len(o) + 3 for o in out) <= 600
    assert big[-1] in out                                          # newest survives the trim


def test_block_formats():
    b = ms.block(FACTS, "jellyfin docker", recent=1, matched=2)
    assert b.startswith("Things you (Gremlin) know") and "- " in b
    assert ms.block([], "x") == ""
