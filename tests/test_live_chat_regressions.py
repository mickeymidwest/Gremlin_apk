"""Regression tests for live chat/search/clear/revise failures captured 2026-11-20."""
import asyncio

from magic_core import action_honesty as ah
from magic_core import autoresearch as ar
from magic_core import history as history_mod
from magic_core.magic import planner, reply, revise


def test_is_clear_command_catches_bare_clear_and_conversational_leadin():
    for msg in (
        "Clear",
        "clear!",
        "reset",
        "wipe",
        "No clear the conversation let's start fresh",
        "hey, clear the chat please",
        "nah start over",
        "let's start fresh",
        "fresh start",
        "clear all",
    ):
        assert history_mod.is_clear_command(msg) is True, msg

    for non_clear in (
        "clear the cache in chrome",
        "is the sky clear today?",
        "reset my router settings",
        "wipe the USB stick",
        "give me a clear explanation",
    ):
        assert history_mod.is_clear_command(non_clear) is False, non_clear


def test_creative_veto_does_not_block_rap_devil_or_pulp_fiction_queries():
    # Regression: bare `\brap\b` in creative veto blocked "Rap Devil" from searching & fact-checking.
    assert ar.should_search("Man MGK really killed it with Eminem on Rap Devil.") is True
    assert ar.should_search("who directed Pulp Fiction?") is True
    assert ar.should_search("who wrote the song Rap Devil?") is True
    assert ar.should_search("who wrote the poem The Raven?") is True
    assert ar.should_search("Wild that Tupac had ties to the Black Panther Party.") is True
    # Casual banter with no factual claim stays offline
    assert ar.should_search("Man CatDog was such a crazy cartoon.") is False
    # Genuine creative generation asks are still vetoed
    assert ar.should_search("write a rap about linux") is False
    assert ar.should_search("drop a rap about python") is False
    assert ar.should_search("tell me a joke") is False
    assert ar.should_search("write me a poem about rain") is False

    bad_draft = (
        'Mac Miller collaborated with Eminem on the song "Rap Devil." '
        "It showcased a blend of MGK's melodic flow and Eminem's aggressive rap style."
    )
    assert ar.needs_fact_check("Mgk was awesome on that rap devil.", bad_draft) is True
    assert ar.needs_fact_check("Man MGK really killed it with Eminem on Rap Devil.", bad_draft) is True


def test_multi_item_paste_searches_each_factual_item_independently(monkeypatch):
    paste = (
        'What major shonen did the creator of Hunter x Hunter make before it?"\n'
        '"Wild that Tupac had ties to the Black Panther Party."\n'
        '"Man CatDog was such a crazy cartoon."\n'
        '"Man MGK really killed it with Eminem on Rap Devil."'
    )
    assert ar.should_search(paste) is True

    seen_queries = []

    def _fake_search(q, limit=5, timeout=12):
        seen_queries.append(q)
        ql = q.lower()
        if "hunter" in ql:
            return [{
                "title": "Yoshihiro Togashi - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Yoshihiro_Togashi",
                "snippet": (
                    "Yoshihiro Togashi created the major shonen manga series Yu Yu Hakusho (1990-1994) "
                    "in Weekly Shonen Jump before creating Hunter x Hunter in 1998."
                ),
            }]
        if "tupac" in ql:
            return [
                {
                    "title": "White Wolf (Hunter) - Marvel Database",
                    "url": "https://marvel.fandom.com/wiki/Hunter_(White_Wolf)",
                    "snippet": "Hunter is the adopted brother of Black Panther T'Challa in Wakanda in Marvel Comics.",
                },
                {
                    "title": "Tupac Shakur - Wikipedia",
                    "url": "https://en.wikipedia.org/wiki/Tupac_Shakur",
                    "snippet": (
                        "Tupac Shakur's mother Afeni Shakur, stepfather Mutulu Shakur, and godmother Assata Shakur "
                        "were prominent members of the Black Panther Party."
                    ),
                },
            ]
        if "rap devil" in ql or "mgk" in ql or "eminem" in ql:
            return [{
                "title": "Rap Devil - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Rap_Devil",
                "snippet": (
                    "Rap Devil is a 2018 diss track by American rapper Machine Gun Kelly (MGK) aimed at Eminem, "
                    "released in response to Eminem's song Not Alike; it is not a collaboration."
                ),
            }]
        return []

    monkeypatch.setattr(ar.web_search, "search", _fake_search)
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda url, *a, **k: (
            "Yoshihiro Togashi wrote Yu Yu Hakusho from 1990 to 1994 before Hunter x Hunter. "
            "Tupac Shakur had deep family ties to the Black Panther Party through Afeni Shakur and Mutulu Shakur. "
            "Rap Devil is a 2018 diss track recorded by Machine Gun Kelly targeting Eminem."
        ),
    )

    srcs, ev = asyncio.run(ar.gather(paste))
    assert srcs and len(srcs) == 3
    urls = [s["url"] for s in srcs]
    assert "https://en.wikipedia.org/wiki/Yoshihiro_Togashi" in urls
    assert "https://en.wikipedia.org/wiki/Tupac_Shakur" in urls
    assert "https://en.wikipedia.org/wiki/Rap_Devil" in urls
    # Marvel Black Panther fandom wiki must be evicted because it misses proper entity "Tupac"
    assert all("marvel.fandom.com" not in u for u in urls)
    # CatDog casual line was NOT sent as a web search
    assert not any("catdog" in q.lower() for q in seen_queries)
    assert "Yu Yu Hakusho" in ev and "Afeni Shakur" in ev and "diss track" in ev

    # Unquoted multi-topic paste splits cleanly too
    unquoted = (
        "What major shonen did the creator of Hunter x Hunter make before it?\n"
        "Wild that Tupac had ties to the Black Panther Party.\n"
        "Man CatDog was such a crazy cartoon.\n"
        "Man MGK really killed it with Eminem on Rap Devil."
    )
    assert len(ar._split_multi_items(unquoted)) == 4
    assert ar.should_search(unquoted) is True


def test_fandom_only_hijack_missing_lead_proper_entity_fails_closed(monkeypatch):
    monkeypatch.setattr(
        ar.web_search,
        "search",
        lambda q, limit=5, timeout=12: [{
            "title": "Black Panther - Marvel Database",
            "url": "https://marvel.fandom.com/wiki/Black_Panther",
            "snippet": (
                "Black Panther (T'Challa) is the king of Wakanda and superhero in Marvel Comics, "
                "adopted brother of Hunter (White Wolf), leader of the Wakandan royal guard."
            ),
        }],
    )
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda *a, **k: (
            "T'Challa is the Black Panther of Wakanda in Marvel Comics alongside Shuri and Hunter (White Wolf). "
            "The royal family defends Wakanda and protects vibranium reserves."
        ),
    )
    srcs, ev = asyncio.run(ar.gather("Wild that Tupac had ties to the Black Panther Party.", force=True))
    assert srcs is None and ar.is_no_source(ev)


def test_revise_strips_meta_preamble_and_think_tags():
    class _PreambleBackend:
        async def generate(self, prompt, *, system="", **k):
            if "strict reviewer" in system:
                return type("R", (), {
                    "ok": True,
                    "text": '{"pass": false, "flaw": "wrong artist", "fix": "MGK is Machine Gun Kelly and Rap Devil is a diss track"}',
                })()
            return type("R", (), {
                "ok": True,
                "text": (
                    "<think>Let me fix this carefully.</think>\n"
                    "Sure, here's the corrected answer:\n\n"
                    "MGK (Machine Gun Kelly) didn't collaborate with Eminem on Rap Devil — it was a 2018 diss track aimed at Eminem."
                ),
            })()

    out, changed = asyncio.run(
        revise.review_and_revise(
            _PreambleBackend(),
            "Man MGK really killed it with Eminem on Rap Devil.",
            "Mac Miller collaborated with Eminem on Rap Devil.",
        )
    )
    assert changed is True
    assert not out.lower().startswith("sure, here")
    assert "<think>" not in out and "</think>" not in out
    assert "Machine Gun Kelly" in out


def test_action_honesty_catches_based_on_my_search_and_runs_after_revise(monkeypatch, tmp_path):
    assert ah.claims_web_search(
        "Specific documented records are not available from reliable sources based on my search."
    )
    assert ah.claims_web_search("My search didn't find anything about that.")

    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", True)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply.look, "workspace_probe", lambda *a, **k: ([], ""))
    monkeypatch.setattr(ar, "should_search", lambda *a, **k: False)
    monkeypatch.setattr(ar, "needs_fact_check", lambda *a, **k: False)

    class _ReviseAddsFakeSearchBackend:
        def __init__(self):
            self.calls = 0

        async def generate(self, prompt, *, system="", **k):
            self.calls += 1
            if "strict reviewer" in system:
                return type("R", (), {
                    "ok": True,
                    "text": '{"pass": false, "flaw": "be clearer", "fix": "clarify"}',
                })()
            if "improving your OWN draft" in system:
                return type("R", (), {
                    "ok": True,
                    "text": (
                        "Sure, here's the corrected answer:\n\n"
                        "Specific records confirming that are not available from reliable sources based on my search. " * 3
                    ),
                })()
            if "NO_WEB_OR_TOOLS_RAN_THIS_TURN" in prompt:
                return type("R", (), {
                    "ok": True,
                    "text": "I haven't searched online for that, so I can't verify specific records right now.",
                })()
            return type("R", (), {
                "ok": True,
                "text": "Here is a general thought about the topic with plenty of words so the reviewer definitely runs on it. " * 4,
            })()

    be = _ReviseAddsFakeSearchBackend()
    res = asyncio.run(reply.answer(be, "Tell me your thoughts on that topic please", str(tmp_path)))
    assert "based on my search" not in res["answer"].lower()
    assert "sure, here's the corrected answer" not in res["answer"].lower()


def test_answer_stream_strips_think_and_replaces_done_answer_on_search_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", True)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply.look, "workspace_probe", lambda *a, **k: ([], ""))
    monkeypatch.setattr(ar, "should_search", lambda *a, **k: False)
    monkeypatch.setattr(
        ar.web_search,
        "search",
        lambda q, limit=5, timeout=12: [{
            "title": "Rap Devil - Wikipedia",
            "url": "https://en.wikipedia.org/wiki/Rap_Devil",
            "snippet": (
                "Rap Devil is a 2018 diss track by Machine Gun Kelly aimed at Eminem, "
                "released in September 2018 in response to Eminem's Not Alike from Kamikaze."
            ),
        }],
    )
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda *a, **k: (
            "Rap Devil is a diss track recorded by American rapper Machine Gun Kelly directed at Eminem "
            "and released in September 2018. Eminem responded with Killshot."
        ),
    )

    class _StreamFixBackend:
        async def generate_stream(self, prompt, **k):
            yield "internal reasoning</think>\n"
            yield "Yeah, MGK and Eminem collaborated on Rap Devil in 2018."

        async def generate(self, prompt, *, system="", **k):
            if "strict reviewer" in system:
                return type("R", (), {
                    "ok": True,
                    "text": '{"pass": false, "flaw": "Rap Devil is a diss track, not a collaboration", "fix": "Say it is a diss track"}',
                })()
            return type("R", (), {
                "ok": True,
                "text": "Actually, Rap Devil wasn't a collab — it's Machine Gun Kelly's 2018 diss track aimed at Eminem.",
            })()

    async def _run_stream():
        done = None
        async for kind, payload in reply.answer_stream(
            _StreamFixBackend(), "Mgk was awesome on that rap devil.", str(tmp_path)
        ):
            if kind == "done":
                done = payload
        return done

    done = asyncio.run(_run_stream())
    assert done is not None
    assert "</think>" not in done["answer"]
    assert "collaborated" not in done["answer"]
    assert "diss track" in done["answer"]
    assert "en.wikipedia.org" in done["answer"]


def test_planner_note_forbids_echoing_step_headings():
    class _FakePlan:
        async def generate(self, prompt, **k):
            return type("R", (), {
                "ok": True,
                "text": '{"steps": ["Identify creator", "Name series"], "missing": []}',
            })()

    note = asyncio.run(planner.plan_note(_FakePlan(), "q", grounded=True))
    assert "Plan your answer" in note
    assert "NEVER echo step labels as headings" in note
