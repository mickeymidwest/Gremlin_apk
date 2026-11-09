"""A follow-up that dropped its subject re-attaches the prior turn's topic;
a self-contained question is left alone."""
from gremlin_core import autoresearch as a


def test_subjectless_followup_gets_prior_topic():
    h = "User: who is playing in the maple leafs game tonight\nGremlin: not sure"
    q = a.contextual_query("who are the playing", h)
    assert "maple" in q.lower() and "leafs" in q.lower()


def test_pronoun_followup_gets_prior_topic():
    h = "User: the new resident evil movie\nGremlin: cool"
    q = a.contextual_query("how much did it make", h)
    assert "resident" in q.lower() and "evil" in q.lower()


def test_generic_noun_followup_gets_context():
    h = ("User: hows the leafs game\nGremlin: ...\n"
         "User: the maple leafs on tv\nGremlin: ...")
    q = a.contextual_query("whats the score", h)
    assert "leafs" in q.lower()


def test_self_contained_question_unchanged():
    h = "User: tell me about pizza\nGremlin: ..."
    assert a.contextual_query("how tall is mount everest", h) == a.search_query("how tall is mount everest")
    assert a.contextual_query("how much did resident evil make", h) == \
        a.search_query("how much did resident evil make")


def test_no_history_is_passthrough():
    assert a.contextual_query("who are the playing", "") == a.search_query("who are the playing")


def test_last_user_topic_extracts_entity():
    assert a._last_user_topic("User: who is playing in the maple leafs game tonight\n"
                              "Gremlin: x") == "maple leafs"
