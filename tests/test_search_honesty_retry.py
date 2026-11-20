"""Regression tests for factual `who <verb>` search, fake search claims, and uncertainty fallback."""
import asyncio

from magic_core import action_honesty as ah
from magic_core import autoresearch as ar
from magic_core.magic import reply


def test_who_action_verbs_and_explicit_lookup_trigger_search():
    assert ar.should_search("who put thr hit out on avons nephew on the wire") is True
    assert ar.should_search("who ordered the hit on D'Angelo Barksdale") is True
    assert ar.should_search("he didnt try to look it up") is True
    assert ar.should_search("can you look up who won") is True
    # General factual yes/no & causal external questions must also search
    assert ar.should_search("in ghost whisperer does she see ghosts or just hear them") is True
    assert ar.should_search("does melinda see ghosts in ghost whisperer") is True
    assert ar.should_search("why did stringer kill d'angelo") is True
    assert ar.should_search("did avon know stringer ordered the hit") is True
    # Meta words from follow-up must drop out so prior subject leads the condensed query
    q = ar.contextual_query(
        "he didnt try to look it up",
        history="User: who put thr hit out on avons nephew on the wire\nMagic: I don't know.\n",
    )
    condensed = ar._condense_keywords(q)
    assert "avons" in condensed and "wire" in condensed and "didnt" not in condensed


def test_event_lookup_fetches_pages_and_correction_followup_researches():
    snips = [{"snippet": "Mount Everest is 8,848.86 m high. " + "x" * 120}]  # > 120 chars with full terms
    assert ar._should_fetch("who put thr hit out on avons nephew on the wire", [{"snippet": "x" * 150}]) is True
    assert ar._should_fetch("how tall is mount everest", snips) is False
    assert "strangle" in ar._page_retrieve_query("who put the hit out on avons nephew")
    hist = (
        "User: who put thr hit out on avons nephew on the wire\n"
        "Magic: Wait...\n(Looked it up just now: en.wikipedia.org)\n"
    )
    assert ar._correction_followup(
        "Bruh was choked to death in season 2 I just watched it happen and Avon thinks d'angelo killed himself",
        hist,
    ) is True
    assert ar._correction_followup("but i asked about the ghost tv on permount plus", hist) is True
    assert ar._correction_followup("But I also asked about dragonballz", hist) is True
    assert ar._correction_followup("ok cool thanks", hist) is False


def test_source_tier_and_wiki_backup_beats_clickbait(monkeypatch):
    def _fake_search(q, limit=5, timeout=12):
        if "site:wikipedia.org" in q:
            return [{
                "title": "Goku - Dragon Ball Wiki",
                "url": "https://dragonball.fandom.com/wiki/Goku",
                "snippet": "Goku died twice in Dragon Ball Z during battles with Raditz and Cell, and was also killed briefly by Hit in Dragon Ball Super.",
            }]
        return [
            {"title": "Blog", "url": "https://fictionhorizon.com/goku/", "snippet": "Goku clickbait listicle about every single anime battle transformation in Dragon Ball."},
            {"title": "FB", "url": "https://www.facebook.com/x", "snippet": "Random social media post noise mentioning Goku from Dragon Ball fans online."},
            {"title": "CBR", "url": "https://www.cbr.com/goku/", "snippet": "CBR ranked listicle article covering Goku moments across Dragon Ball Z."},
        ]
    monkeypatch.setattr(ar.web_search, "search", _fake_search)
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda *a, **k: (
            "In Dragon Ball Z, Goku died twice: first sacrificing himself against Raditz alongside Piccolo's Special Beam Cannon, "
            "and second teleporting away with Semi-Perfect Cell before self-destruction. Later in Dragon Ball Super, Hit killed Goku briefly."
        ),
    )
    srcs, ev = asyncio.run(ar.gather("How many times in Dragonballz gt and super did Goku die"))
    assert srcs and srcs[0]["url"] == "https://dragonball.fandom.com/wiki/Goku"
    assert "facebook.com" not in str([s["url"] for s in srcs])


def test_edu_space_and_tech_fact_sites_ranked_and_targeted():
    assert ar.should_search("research papers on kv cache quantization") is True
    assert ar._source_tier("https://science.nasa.gov/universe/black-holes/") == 0
    assert ar._source_tier("https://arxiv.org/abs/2401.00001") == 0
    assert ar._source_tier("https://cs.stanford.edu/people/paper.pdf") == 0
    assert ar._source_tier("https://arstechnica.com/gadgets/2026/11/chip/") == 0
    assert "site:.edu" in ar._backup_site_filter("research papers on kv cache quantization")
    assert "site:nasa.gov" in ar._backup_site_filter("how big is a black hole in space")
    assert "site:arstechnica.com" in ar._backup_site_filter("how does cuda tensor core hardware work")
    assert ar._should_fetch("when did rainbow brite start and end", [{"snippet": "x" * 200}]) is True
    from magic_core import web_search as ws
    assert "wt" not in ws._clean('<span data-mw=\'{"wt":"1>2"}\'>1984 to 1986</span>')


def test_missing_subject_terms_triggers_keyword_retry_and_ranks_exact_snippet(monkeypatch):
    calls = []

    def _fake_search(q, limit=5, timeout=12):
        calls.append(q)
        if q == "gundam burning finger":
            return [{
                "title": "Burning Gundam | GUNDAM Official Website",
                "url": "https://en.gundam-official.com/mecha/1",
                "snippet": "Its ultimate signature move is Erupting Burning Finger in Mobile Fighter G Gundam.",
            }]
        return [{
            "title": "God Gundam | Fandom",
            "url": "https://gundam.fandom.com/wiki/God_Gundam",
            "snippet": "Burning Gundam in the English dub of Mobile Fighter G Gundam.",
        }]

    monkeypatch.setattr(ar.web_search, "search", _fake_search)
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda *a, **k: (
            "Mobile Fighter G Gundam is the 1994 anime television series directed by Yasuhiro Imagawa. "
            "Pilot Domon Kasshu uses the Shining Finger and later the Erupting Burning Finger signature finishing attack inside Burning Gundam."
        ),
    )
    srcs, ev = asyncio.run(ar.gather("What gundam show had the burning finger"))
    assert "gundam burning finger" in calls
    assert srcs and srcs[0]["url"] == "https://en.gundam-official.com/mecha/1"
    assert "Erupting Burning Finger" in ev


def test_truncated_snippets_fetch_and_reddit_does_not_beat_wikipedia():
    ranked = ar._rank_results(
        [
            {"title": "Reddit", "url": "https://www.reddit.com/r/x/1", "snippet": "real kids next door"},
            {"title": "Wiki", "url": "https://en.wikipedia.org/wiki/Codename:_Kids_Next_Door", "snippet": "Codename: Kids Next Door ..."},
        ],
        keep=2,
        query="did the kids in kids next door have real names",
    )
    assert ranked[0]["url"].startswith("https://en.wikipedia.org/")
    assert ar._should_fetch("did the kids in kids next door have real names", ranked) is True
    assert "named" in ar._page_retrieve_query("did the kids in kids next door have real names")


def test_short_names_and_shop_dedup_in_rank_results():
    assert "ed" in ar._subject_terms("which ed wears what by hair and clothes")
    assert ar._source_tier("https://www.etsy.com/listing/123/ed-hat") == 3
    ranked = ar._rank_results(
        [
            {"title": "Ed Hat", "url": "https://www.etsy.com/listing/123/ed-hat", "snippet": "ed hair hat"},
            {"title": "Ed p1", "url": "https://ed.fandom.com/wiki/Ed?page=1", "snippet": "Ed wears jacket"},
            {"title": "Ed p11", "url": "https://ed.fandom.com/wiki/Ed?page=11", "snippet": "Ed wears jacket"},
            {"title": "Edd", "url": "https://ed.fandom.com/wiki/Edd", "snippet": "Double D wears beanie"},
            {"title": "Show", "url": "https://en.wikipedia.org/wiki/Ed,_Edd_n_Eddy", "snippet": "Ed Edd Eddy clothes"},
        ],
        keep=3,
        query="which ed wears what",
    )
    urls = [r["url"] for r in ranked]
    assert len([u for u in urls if "wiki/Ed?" in u]) == 1
    assert "etsy.com" not in str(urls)


def test_short_tell_me_and_what_is_searches_and_bypasses_casual_fastpath():
    for msg in (
        "tell me about big o",
        "what is big o",
        "plot of big o",
        "describe the opening scene in ghost in the shell 1995",
        "explain ghost in the shell 1995 body creation",
        "ghost in the shell 1995",
    ):
        assert ar.should_search(msg) is True, msg
        assert reply._is_casual_chat(msg) is False, msg
    assert ar.should_search("tell me about yourself") is False
    assert ar.should_search("explain how a python function works") is False
    assert ar.should_search("what is a python function") is False
    assert ar.should_search("i was born in 1995") is False
    assert reply._is_casual_chat("hey") is True
    assert reply._is_casual_chat("ok cool") is True


def test_revise_verdict_grammar_compiles_and_lookup_helps_reviews(monkeypatch):
    from magic_core.backends import grammars as G
    from magic_core.magic import revise

    if G.LlamaGrammar is not None:
        assert G.compile(revise._VERDICT_GRAMMAR) is not None

    monkeypatch.setattr(
        ar.web_search,
        "search",
        lambda q, limit=5, timeout=12: [
            {
                "title": "The Big O - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/The_Big_O",
                "snippet": (
                    "The Big O (1999) is a neo-noir mecha anime television series produced by Sunrise and set in the dome metropolis of Paradigm City, "
                    "starring smooth negotiator Roger Smith and android assistant R. Dorothy Wayneright alongside Big O."
                ),
            }
        ],
    )
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda *a, **k: (
            "The Big O (1999) is a neo-noir mecha anime series animated by Sunrise and set in Paradigm City where residents lost their memories forty years prior, "
            "following negotiator Roger Smith as he pilots the giant megadeus Big O."
        ),
    )

    class _ReviseWithLookupBackend:
        def __init__(self):
            self.seen_prompts = []

        async def generate(self, prompt, *, system="", **k):
            self.seen_prompts.append(prompt)
            if "strict reviewer" in system:
                assert "Paradigm City" in prompt
                return type("R", (), {
                    "ok": True,
                    "text": '{"pass": false, "flaw": "Draft claims New Babylon instead of Paradigm City", "fix": "Use Paradigm City and Roger Smith from evidence"}',
                })()
            assert "Paradigm City" in prompt
            return type("R", (), {
                "ok": True,
                "text": "The Big O (1999) is a Sunrise mecha anime set in Paradigm City following negotiator Roger Smith.",
            })()

    sink = []
    bad_draft = "Big O is a mecha anime from 1999.\n1. **Setting**: New Babylon dome city with Big OMs."
    assert ar.needs_fact_check("check out big o", bad_draft) is True
    out, changed = asyncio.run(
        revise.review_and_revise(
            _ReviseWithLookupBackend(),
            "check out big o",
            bad_draft,
            lookup_web=True,
            source_sink=sink,
        )
    )
    assert changed is True and "Paradigm City" in out
    assert sink and sink[0]["url"] == "https://en.wikipedia.org/wiki/The_Big_O"


def test_toolhost_web_docs_and_battle_grammar(monkeypatch, tmp_path):
    from magic_core.backends import grammars as G
    from magic_core.magic.toolhost import ShellToolHost, ToolCall

    assert "web_docs" in ShellToolHost.EXPLORE_TOOLS
    if G.LlamaGrammar is not None:
        assert G.compile(G.react_turn_grammar(ShellToolHost.EXPLORE_TOOLS)) is not None

    monkeypatch.setattr(
        ar.web_search,
        "search",
        lambda q, limit=4, timeout=12: [
            {"title": "FastAPI Docs", "url": "https://fastapi.tiangolo.com/", "snippet": "Use @app.get('/')"}
        ],
    )
    monkeypatch.setattr(ar.web_search, "fetch_text", lambda *a, **k: "from fastapi import FastAPI")
    th = ShellToolHost(tmp_path, allowed=ShellToolHost.EXPLORE_TOOLS)
    res = th.run(ToolCall("web_docs", {"query": "fastapi get decorator"}))
    assert res.ok and "FastAPI Docs" in res.output and "FastAPI" in res.output

    th_off = ShellToolHost(tmp_path, allow_web=False)
    assert not th_off.run(ToolCall("web_docs", {"query": "fastapi"})).ok


def test_build_project_and_build_loop_gather_docs_on_api_goal_and_error(monkeypatch, tmp_path):
    from magic_core import build_loop, build_project

    monkeypatch.setattr(
        ar.web_search,
        "search",
        lambda q, limit=3, timeout=12: [
            {
                "title": "Python sqlite3 Documentation",
                "url": "https://docs.python.org/3/library/sqlite3.html",
                "snippet": (
                    "The Python standard library sqlite3 module provides a lightweight DB-API 2.0 interface using sqlite3.connect(db) "
                    "to open database connections, create tables, execute parameterized SQL queries, and fetch structured rows."
                ),
            }
        ],
    )
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda *a, **k: (
            "Python sqlite3 documentation explaining sqlite3.connect(database), cursor execution, transaction commits, "
            "and Row factory access for mapping query columns cleanly in lightweight local Python sqlite applications."
        ),
    )

    seen = []

    class _FakeRouter:
        async def route(self, model, prompt, system="", max_tokens=4096):
            seen.append(prompt)
            return type("R", (), {"ok": True, "text": "=== FILE: app.py ===\nimport sqlite3\n=== END FILE ===", "error": ""})()

    monkeypatch.setattr(build_project.review_mod, "_revise", lambda r, f, p, g, fb, revise_system="": asyncio.sleep(0, result=p))

    patch, used_t = asyncio.run(
        build_project.propose_with_retry_and_teacher(
            _FakeRouter(), ["local"], "build a sqlite app", str(tmp_path), "gemini"
        )
    )
    assert not used_t and seen and "sqlite3.connect" in seen[0]

    # build_loop._revise_step_proposal also enriches API errors with live docs
    captured_fb = []

    async def _cap_revise(r, f, p, g, fb, revise_system=""):
        captured_fb.append(fb)
        return p

    monkeypatch.setattr(build_loop.review_mod, "_revise", _cap_revise)
    asyncio.run(
        build_loop._revise_step_proposal(
            _FakeRouter(), "local", "patch", "step goal", "ModuleNotFoundError: No module named 'fastapi'"
        )
    )
    assert captured_fb and "Live API / docs reference:" in captured_fb[0]


def test_contraction_questions_tag_questions_and_verify_sink(monkeypatch):
    assert ar.should_search("doesn't batman beyond fly") is True
    assert ar.should_search("can't batman beyond fly") is True
    assert ar.should_search("batman beyond can fly right") is True
    assert "thrusters" in ar._page_retrieve_query("can batman beyond fly")
    assert ar.needs_fact_check("batman beyond?", "Canonically, Terry uses jet propulsion boots.") is True
    assert ar.should_search("in the show weeds how did uturn get killed") is True
    assert ar._source_tier("https://getyarn.io/yarn-clip/1") == 3
    assert ar._source_tier("https://www.wordplays.com/crossword-solver/x") == 3
    assert ar._should_fetch("uturn", [{"snippet": "a"}, {"snippet": "b"}, {"snippet": "cut off ..."}]) is True


def test_pushback_condenses_reaction_words_and_revise_drops_poisoned_draft():
    from magic_core.magic import revise

    hist = "User: the shown weeds Uturn got killed\nMagic: Heylia killed U-Turn.\n"
    assert ar._correction_followup("lmao this is not how he got killed", hist) is True
    assert "lmao" not in ar._condense_keywords(ar.contextual_query("lmao this is not how he got killed", hist))

    seen_revise_prompts = []

    class _CleanRewriteBackend:
        async def generate(self, prompt, *, system="", history=None, **k):
            if "strict reviewer" in system:
                return type("R", (), {
                    "ok": True,
                    "text": '{"pass": false, "flaw": "Draft says Heylia killed U-Turn instead of Marvin", "fix": "State that Marvin killed U-Turn"}',
                })()
            seen_revise_prompts.append(prompt)
            # Even a concise true answer must NOT be rejected against long hallucinated draft
            return type("R", (), {"ok": True, "text": "Marvin killed U-Turn as revenge."})()

    long_poison = "Heylia the Armenian mob boss killed U-Turn in Season 3 Episode 6 Crime. " * 6
    out, changed = asyncio.run(
        revise.review_and_revise(
            _CleanRewriteBackend(),
            "weeds uturn killed",
            long_poison,
            evidence="Marvin was U-Turn's right-hand-man. He killed U-Turn as revenge.",
            drop_draft_on_fail=True,
        )
    )
    assert changed is True and "Marvin killed U-Turn" in out
    assert seen_revise_prompts and "Your draft:" not in seen_revise_prompts[0]


def test_claims_web_search_flags_fake_search_and_allows_meta():
    fake = "Here's the actual answer from a quick search:\n- In Season 2..."
    assert ah.claims_web_search(fake)
    assert ah.claims_web_search("I just searched online and found it.")
    # Meta-discussion about hallucinating searches must not flag
    meta = "I sometimes pretend I answered from a quick search when I didn't run one."
    assert ah.claims_web_search(meta) == []


def test_answer_auto_searches_when_draft_admits_ignorance(monkeypatch, tmp_path):
    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply.look, "workspace_probe", lambda *a, **k: ([], ""))
    monkeypatch.setattr(
        ar.web_search,
        "search",
        lambda q, limit=3, timeout=12: [
            {
                "title": "D'Angelo Barksdale - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/D%27Angelo_Barksdale",
                "snippet": (
                    "D'Angelo Barksdale is a fictional character on HBO's The Wire played by Lawrence Gilliard Jr. "
                    "In season two, Stringer Bell secretly ordered D'Angelo Barksdale killed in prison while Avon Barksdale was incarcerated."
                ),
            }
        ],
    )
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda *a, **k: (
            "In season two of The Wire, Stringer Bell arranges for D'Angelo Barksdale to be strangled in the prison library "
            "and staged as a suicide without telling Avon Barksdale who ordered the hit."
        ),
    )

    class _MockBackend:
        def __init__(self):
            self.calls = 0

        async def generate(self, prompt, max_tokens=2048, temperature=0.4, history=None):
            self.calls += 1
            if self.calls == 1:
                txt = "I don't know who ordered that hit."
            else:
                assert "Stringer Bell" in (prompt + str(history))
                txt = "Stringer Bell ordered the hit on D'Angelo Barksdale in prison."
            return type("R", (), {"ok": True, "text": txt, "model": "magic", "meta": {}})()

    be = _MockBackend()
    # Even if upfront should_search() returns False, fallback uncertainty triggers search + retry
    monkeypatch.setattr(ar, "should_search", lambda *a, **k: False)
    res = asyncio.run(reply.answer(be, "Avon's nephew D'Angelo got killed in prison — Stringer or Omar?", str(tmp_path)))
    assert be.calls == 2
    assert "Stringer Bell" in res["answer"]
    assert "en.wikipedia.org" in res["answer"]


def test_answer_flags_or_rewrites_fake_quick_search_when_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply.look, "workspace_probe", lambda *a, **k: ([], ""))
    monkeypatch.setattr(ar, "should_search", lambda *a, **k: False)
    monkeypatch.setattr(ar.web_search, "search", lambda *a, **k: [])

    class _HonestRetryBackend:
        def __init__(self):
            self.calls = 0

        async def generate(self, prompt, max_tokens=2048, temperature=0.4, history=None):
            self.calls += 1
            if self.calls == 1:
                return type("R", (), {
                    "ok": True,
                    "text": "Here's the actual answer from a quick search: it was Season 2.",
                    "model": "magic",
                    "meta": {},
                })()
            assert "NO_WEB_OR_TOOLS_RAN_THIS_TURN" in prompt
            return type("R", (), {
                "ok": True,
                "text": "I couldn't verify that online right now without search results.",
                "model": "magic",
                "meta": {},
            })()

    be = _HonestRetryBackend()
    res = asyncio.run(reply.answer(be, "Wait — was that Season 2?", str(tmp_path)))
    assert "from a quick search" not in res["answer"]
    assert "couldn't verify" in res["answer"]


def test_forum_and_ai_aggregator_domains_demoted_and_wikipedia_recovers(monkeypatch):
    assert ar._source_tier("https://us.forums.blizzard.com/en/wow/t/togashi/123") == 2
    assert ar._source_tier("https://myanimelist.net/forum/?topicid=1") == 2
    assert ar._source_tier("https://anime.fandom.com/f/p/123") == 2
    assert ar._source_tier("https://hunterxhunter.fandom.com/wiki/Yoshihiro_Togashi") == 0
    assert ar._source_tier("https://www.bestaitools.com/togashi") == 3
    assert ar._source_tier("https://theresanaiforthat.com/ai/foo") == 3
    assert ar._source_tier("https://www.brainyquote.com/quotes/yoshihiro") == 3

    def _fake_search(q, limit=5, timeout=12):
        if "OR site:" in q:
            return []
        if "site:wikipedia.org" in q:
            return [{
                "title": "Yoshihiro Togashi - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Yoshihiro_Togashi",
                "snippet": "Yoshihiro Togashi created Yu Yu Hakusho before Hunter x Hunter.",
            }]
        return [
            {"title": "Forum", "url": "https://us.forums.blizzard.com/en/d4/t/1", "snippet": "Forum discussion mentioning Yoshihiro Togashi and Hunter x Hunter manga hiatus history online."},
            {"title": "AI Tools", "url": "https://www.bestaitools.com/x", "snippet": "AI directory blog listing summaries about Yoshihiro Togashi and Hunter x Hunter anime."},
            {"title": "News", "url": "https://www.slashfilm.com/123/togashi/", "snippet": "Feature article explaining that Yoshihiro Togashi created Yu Yu Hakusho before starting Hunter x Hunter."},
        ]

    monkeypatch.setattr(ar.web_search, "search", _fake_search)
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda *a, **k: (
            "Yoshihiro Togashi is a Japanese manga artist best known for writing Yu Yu Hakusho starting in 1990 "
            "and Hunter x Hunter starting in 1998 in Weekly Shonen Jump."
        ),
    )
    srcs, ev = asyncio.run(ar.gather("Did Yoshihiro Togashi make a show before Hunter x Hunter?"))
    urls = [s["url"] for s in srcs]
    assert urls[0] == "https://en.wikipedia.org/wiki/Yoshihiro_Togashi"
    assert all("bestaitools.com" not in u for u in urls)
    assert urls.index("https://www.slashfilm.com/123/togashi/") < urls.index("https://us.forums.blizzard.com/en/d4/t/1")


def test_self_contained_topic_not_poisoned_by_prior_history_and_missing_entity_retries(monkeypatch):
    hist = "User: Yoshihiro Togashi Hunter x Hunter creator\nMagic: Yu Yu Hakusho.\n"
    for msg in (
        "that's wild, tupac was connected to the black panthers",
        "did tupac have ties to their black panther party",
    ):
        assert ar._needs_context(msg) is False, msg
        q = ar.contextual_query(msg, hist)
        assert "hunter" not in q.lower() and "togashi" not in q.lower(), q

    calls = []

    def _fake_search(q, limit=5, timeout=12):
        calls.append(q)
        if q.startswith("tupac "):
            return [{
                "title": "Tupac Shakur - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Tupac_Shakur",
                "snippet": "Tupac Shakur's mother Afeni Shakur was an active Black Panther Party member.",
            }]
        return [{
            "title": "White Wolf (Hunter) - Marvel",
            "url": "https://marvel.fandom.com/wiki/Hunter_(White_Wolf)",
            "snippet": "Hunter is the adopted brother of Black Panther T'Challa.",
        }]

    monkeypatch.setattr(ar.web_search, "search", _fake_search)
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda *a, **k: (
            "Tupac Shakur grew up surrounded by former Black Panther Party members through his mother Afeni Shakur, "
            "stepfather Mutulu Shakur, and godmother Assata Shakur in New York and Baltimore."
        ),
    )
    srcs, ev = asyncio.run(ar.gather(
        "that's wild, tupac had ties to the black panthers",
        history=hist,
        force=True,
    ))
    assert any(c.startswith("tupac ") for c in calls)
    assert srcs and srcs[0]["url"] == "https://en.wikipedia.org/wiki/Tupac_Shakur"
    assert "Afeni Shakur" in ev


def test_needs_fact_check_catches_collaboration_and_attribution_claims():
    draft = (
        'The way he flows with Eminem in "Rap Devil" hits hard. '
        "That collaboration definitely helped push people's perception of him."
    )
    assert ar.needs_fact_check("mgk snapped on that track", draft) is True
    assert ar.needs_fact_check("tell me a joke", draft) is False


def test_declarative_pop_culture_triggers_and_blocked_pages():
    assert ar.should_search("that weird episode of Seinfeld where George is an architect") is True
    assert ar.should_search("in the 90s movie The Matrix Morpheus talks about red pills") is True
    assert ar.should_search("plot of Blade Runner 2049") is True
    assert ar.should_search("season 2 episode 6 of The Wire") is True
    assert reply._is_casual_chat("who played Morpheus") is False
    assert reply._is_casual_chat("matrix 1999") is False
    assert ar._is_blocked_text("Please enable cookies and JavaScript must be enabled to continue.") is True
    assert ar._is_blocked_text("Pardon our interruption. Ray ID: 8a1b2c3d") is True


def test_fail_closed_no_source_blocks_improvisation_in_answer_and_stream(monkeypatch, tmp_path):
    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply.look, "workspace_probe", lambda *a, **k: ([], ""))
    search_calls = []

    def _empty_search(q, limit=5, timeout=12):
        search_calls.append(q)
        return []

    monkeypatch.setattr(ar.web_search, "search", _empty_search)

    class _ImprovisingBackend:
        def __init__(self):
            self.calls = 0

        async def generate(self, prompt, max_tokens=2048, temperature=0.4, history=None):
            self.calls += 1
            # Ignores NO_USABLE_SOURCE and invents a plot anyway (no search phrasing)
            return type("R", (), {
                "ok": True,
                "text": "In Season 3, Episode 4, the character moved to Chicago and opened a diner.",
                "model": "magic",
                "meta": {},
            })()

        async def generate_stream(self, prompt, max_tokens=2048, temperature=0.4, history=None):
            self.calls += 1
            yield "In Season 3, Episode 4, he moved to Chicago."

    be = _ImprovisingBackend()
    res = asyncio.run(reply.answer(be, "what happened in that weird episode of Seinfeld?", str(tmp_path)))
    assert "didn't get a usable source" in res["answer"]
    assert "opened a diner" not in res["answer"]
    # Must NOT re-run search inside _maybe_search_retry once gather failed closed
    n_calls_after_answer = len(search_calls)
    assert n_calls_after_answer <= 2

    # Streaming path fails closed BEFORE emitting hallucinated tokens
    async def _collect_stream():
        deltas = []
        done_obj = None
        async for kind, payload in reply.answer_stream(
            be, "what happened in that weird episode of Seinfeld?", str(tmp_path)
        ):
            if kind == "delta":
                deltas.append(payload)
            elif kind == "done":
                done_obj = payload
        return "".join(deltas), done_obj

    streamed_text, done_obj = asyncio.run(_collect_stream())
    assert "didn't get a usable source" in streamed_text
    assert "Chicago" not in streamed_text
    assert done_obj and "didn't get a usable source" in done_obj["answer"]


def test_hive_web_fact_check_wired_into_answer_and_stream(monkeypatch, tmp_path):
    from magic_core.magic import hive as hv

    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(reply.look, "wants", lambda *a, **k: [])
    monkeypatch.setattr(reply.look, "workspace_probe", lambda *a, **k: ([], ""))
    monkeypatch.setattr(
        ar.web_search,
        "search",
        lambda q, limit=5, timeout=12: [{
            "title": "The Wire - Wikipedia",
            "url": "https://en.wikipedia.org/wiki/The_Wire",
            "snippet": (
                "D'Angelo Barksdale is killed in prison during season two after Stringer Bell secretly ordered the hit "
                "without Avon Barksdale knowing."
            ),
        }],
    )
    monkeypatch.setattr(
        ar.web_search,
        "fetch_text",
        lambda *a, **k: (
            "In season 2 of HBO's The Wire, Stringer Bell secretly ordered D'Angelo Barksdale killed in prison "
            "while staging his death in the prison library to look like a suicide, keeping Avon Barksdale in the dark."
        ),
    )

    class _FakeReg:
        def get(self, name):
            return object() if name == "qwen3.5-4b-cpu" else None

    class _FakeHive:
        def __init__(self, reg, **kw):
            pass

        def check_web_facts(self, draft, source_text, question=""):
            if "Avon" in draft:
                return ["Avon ordered the hit"]
            return []

    monkeypatch.setattr(hv, "build_hive", lambda reg, **kw: _FakeHive(reg))
    monkeypatch.setattr(reply, "_HIVE_REGISTRY", _FakeReg())

    class _WebReplyBackend:
        def __init__(self):
            self.calls = 0

        async def generate(self, prompt, max_tokens=2048, temperature=0.4, history=None):
            self.calls += 1
            if self.calls == 1:
                return type("R", (), {
                    "ok": True,
                    "text": "Avon ordered the hit on D'Angelo in prison.",
                    "model": "magic",
                    "meta": {},
                })()
            assert "Avon ordered the hit" in prompt
            return type("R", (), {
                "ok": True,
                "text": "Stringer Bell secretly ordered D'Angelo Barksdale killed in prison.",
                "model": "magic",
                "meta": {},
            })()

        async def generate_stream(self, prompt, max_tokens=2048, temperature=0.4, history=None):
            yield "Avon ordered the hit on D'Angelo in prison."

    be = _WebReplyBackend()
    res = asyncio.run(reply.answer(be, "who ordered the hit on D'Angelo Barksdale?", str(tmp_path)))
    assert be.calls == 2
    assert "Stringer Bell" in res["answer"]
    assert "Avon ordered the hit" not in res["answer"]

    async def _collect_stream():
        deltas = []
        done_obj = None
        async for kind, payload in reply.answer_stream(
            be, "who ordered the hit on D'Angelo Barksdale?", str(tmp_path)
        ):
            if kind == "delta":
                deltas.append(payload)
            elif kind == "done":
                done_obj = payload
        return "".join(deltas), done_obj

    st, done_obj = asyncio.run(_collect_stream())
    assert "couldn't confirm these details" in st
    assert "Avon ordered the hit" in st
    assert done_obj and "en.wikipedia.org" in done_obj["answer"]


def test_build_prompt_keeps_web_evidence_out_of_assistant_got_it_turn(tmp_path):
    prompt_text, hist, _ctx = reply._build_prompt(
        "tell me about CatDog", str(tmp_path), "", web_evidence="Live web search results..."
    )
    assert "Live web search results..." in prompt_text
    assert "tell me about CatDog" in prompt_text
    assert not any("I'll use those search results" in m.get("content", "") for m in hist)
