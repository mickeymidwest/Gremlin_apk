"""Real bug (2026-09-15/19): mickey asked "did it really look at the
libFuzzer?" after Gremlin claimed to have checked something in plain
chat -- chat has zero tool access, so that claim was fabricated by
construction, not a lie about a specific fact. A second incident the
same week: a confirmation's TTL expired, the "yes" silently fell
through to chat, and chat improvised progress before eventually
claiming "Yeah, I finished it" with nothing behind it (fixed
separately by extending the TTL and adding is_bare_affirmative -- see
intent.py -- but this is the backstop for when *any* future gap lets
an unbacked completion claim reach chat).

These lock in grounding.check()'s new _ACTION_CLAIM_RE: any first-
person claim of having checked/looked at/ran/tested/verified/
confirmed something gets flagged in the chat path, unconditionally --
not gated on empty context like _BACKREF_RE, because chat's context
(memory facts, skills, history) never contains real tool-executed
evidence regardless of how much of it there is."""
from gremlin_core.magic import grounding


def test_catches_the_real_libfuzzer_incident_shape():
    text = "I checked the libFuzzer harness and it looks fine, no bugs there."
    findings = grounding.check(text, "/tmp/doesnt-matter", context="some memory facts here")
    assert any("no tool ran this turn" in f for f in findings)


def test_catches_the_real_fabricated_completion_shape():
    text = "Yeah, I finished it -- all done, the build's ready."
    findings = grounding.check(text, "/tmp/doesnt-matter", context="")
    assert any("no tool ran this turn" in f for f in findings)


def test_flags_even_with_rich_context_present():
    # unlike _BACKREF_RE, this isn't about missing context -- chat's
    # context is memory facts/skills/history, never real tool output,
    # so a claim like this is unbacked no matter how much context exists
    text = "I already verified the config file is correct."
    ctx = "Things you know about the user: ...\nEarlier in this conversation: ..."
    findings = grounding.check(text, "/tmp/doesnt-matter", context=ctx)
    assert any("no tool ran this turn" in f for f in findings)


def test_does_not_flag_a_hypothetical():
    text = "If I checked the config, it would probably show the same thing."
    findings = grounding.check(text, "/tmp/doesnt-matter", context="")
    assert not any("no tool ran this turn" in f for f in findings)


def test_does_not_flag_the_ran_into_idiom():
    text = "I ran into a weird error last time we tried this."
    findings = grounding.check(text, "/tmp/doesnt-matter", context="")
    assert not any("no tool ran this turn" in f for f in findings)


def test_clean_answer_with_no_action_claim_passes():
    text = "That's probably a race condition in how the two threads share state."
    findings = grounding.check(text, "/tmp/doesnt-matter", context="")
    assert findings == []


def test_catches_the_real_restarting_container_incident_shape():
    # live 2026-09-19: asked to restart a container, the classifier
    # (a separate bug) routed to chat instead of a tool, and chat
    # answered as if it were actually doing it -- nothing ran.
    text = "Mickey, I'm restarting the robofuse container..."
    findings = grounding.check(text, "/tmp/doesnt-matter", context="")
    assert any("nothing is actually running" in f for f in findings)


def test_does_not_flag_a_general_statement_about_an_ing_verb():
    text = "Running low on disk space is annoying, you should clean that up."
    findings = grounding.check(text, "/tmp/doesnt-matter", context="")
    assert findings == []
