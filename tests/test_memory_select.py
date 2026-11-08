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


def test_huge_fact_does_not_evict_normal_facts():
    facts = [
        "Mickey's desktop is a Dell G5",                       # 0 old, relevant below
        "A giant pasted blob: " + "y" * 3000,                 # 1 huge, in the older pool
        "Mickey likes dark humor",                            # 2
        "Mickey prefers short answers",                       # 3 newest
    ]
    out = ms.select(facts, "what desktop do I have", recent=2, matched=3, max_chars=800)
    joined = " ".join(out)
    assert "Dell G5" in joined                                # the relevant normal fact survives
    assert "yyyy" not in joined                               # the huge blob didn't crowd it out
    assert sum(len(o) + 3 for o in out) <= 800


def test_lone_oversized_fact_is_truncated_not_dropped():
    out = ms.select(["z" * 5000], "anything", recent=1, max_chars=400)
    assert len(out) == 1 and len(out[0]) <= 400 and out[0].endswith("…")


def test_common_words_do_not_false_match():
    facts = ["Mickey's desktop is a Dell G5", "Mickey runs Jellyfin in Docker", "Mickey likes dark humor"]
    # query shares only stopwords (from/their/them/mickey/just) with the facts
    out = ms.select(facts, "just tell me about them from their side", recent=1, matched=3)
    assert out == facts[-1:]                                  # nothing matched -> only the newest
