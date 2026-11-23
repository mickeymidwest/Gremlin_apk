"""Tests for private SSE stream error reporting, VRAM mid-load serialization,
and avoiding 7.5GB swap thrash during post-turn auxiliary calls."""
import asyncio
import json
import sys

sys.path.insert(0, "local_plugins")
import private_chat as pc  # noqa: E402
from magic_core.backends.base import ModelBackend, ModelInfo  # noqa: E402
from magic_core.magic import reply as reply_mod  # noqa: E402
from magic_core.magic import vram  # noqa: E402


def _collect(agen):
    async def _run():
        return [ev async for ev in agen]
    return asyncio.run(_run())


def test_answer_private_stream_preserves_error_message_and_model_name():
    class _BoomStream(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="mn-violet-lotus-12b", kind="local_gguf"))

        async def generate(self, *args, **kwargs):
            raise RuntimeError("unused")

        async def generate_stream(self, *args, **kwargs):
            if False:
                yield ""
            raise RuntimeError("mn-violet-lotus-12b is still busy loading")

    events = _collect(
        reply_mod.answer_private_stream(
            _BoomStream(), "hi", system="sys", history_msgs=[], unfiltered=True
        )
    )
    assert len(events) == 1
    kind, payload = events[0]
    assert kind == "done"
    assert payload["ok"] is False
    assert payload["model"] == "mn-violet-lotus-12b"
    assert payload["error"] and "RuntimeError" in payload["error"]


def test_answer_private_stream_empty_output_has_non_null_error():
    class _EmptyStream(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="l3-8b-stheno-v3.2", kind="local_gguf"))

        async def generate(self, *args, **kwargs):
            return None

        async def generate_stream(self, *args, **kwargs):
            if False:
                yield ""

    events = _collect(
        reply_mod.answer_private_stream(
            _EmptyStream(), "hi", system="sys", history_msgs=[], unfiltered=True
        )
    )
    kind, payload = events[0]
    assert kind == "done"
    assert payload["ok"] is False
    assert payload["model"] == "l3-8b-stheno-v3.2"
    assert isinstance(payload["error"], str) and payload["error"]


def test_ensure_only_unloads_in_progress_loading_backend(monkeypatch):
    class _LoadingBackend:
        def __init__(self):
            self._llm = None
            self._loading = True
            self._lock = asyncio.Lock()
            self.unloaded = False

        async def unload(self):
            self.unloaded = True
            self._loading = False
            self._llm = None

    loading_other = _LoadingBackend()
    target = _LoadingBackend()
    target._loading = False

    monkeypatch.setattr(
        vram,
        "_local_gguf_backends",
        lambda reg: {"llama-3.1-8b-abliterated": loading_other, "mn-violet-lotus-12b": target},
    )
    monkeypatch.setattr(vram, "footprint_mb", lambda path: 5400)
    monkeypatch.setattr(vram, "_model_path", lambda reg, name: "/x.gguf")
    monkeypatch.setattr(vram, "set_active", lambda name: None)
    monkeypatch.setattr(vram, "free_mb", lambda: 7600)

    async def _fast_sleep(_):
        return None

    monkeypatch.setattr(vram.asyncio, "sleep", _fast_sleep)

    asyncio.run(vram.ensure_only(object(), keep="mn-violet-lotus-12b"))
    assert loading_other.unloaded is True
    assert target.unloaded is False


def test_pick_aux_backend_skips_cold_cpu_worker_when_ram_tight(monkeypatch):
    class CpuChatBackend:
        _proc = None

    class _FakeReg:
        def __init__(self):
            self._m = {
                "mn-violet-lotus-12b": type("GPU", (), {"_llm": object()})(),
                "qwen3.5-4b-cpu": CpuChatBackend(),
            }

        def get(self, name):
            return self._m.get(name)

        def primary_model_name(self):
            return "qwen3.5-9b"

    monkeypatch.setattr(pc, "_avail_ram_mb", lambda: 1800)
    reg = _FakeReg()
    picked = pc._pick_aux_backend(reg, model_name="mn-violet-lotus-12b")
    assert picked is reg.get("mn-violet-lotus-12b")


def test_sse_stream_fail_open_empty_sets_helpful_error(monkeypatch):
    from flask import Flask
    import asyncio as _aio
    from types import SimpleNamespace

    app = Flask(__name__)

    class _EmptyBe:
        info = SimpleNamespace(name="l3-8b-stheno-v3.2")
        _llm = object()

        async def generate_stream(self, *args, **kwargs):
            if False:
                yield ""

    class _Reg:
        def get(self, name):
            return _EmptyBe()

        def names(self):
            return ["l3-8b-stheno-v3.2"]

        def primary_model_name(self):
            return "qwen3.5-9b"

    loop = _aio.new_event_loop()
    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setattr(vram, "ensure_only", lambda reg, keep=None: _aio.sleep(0))
        pc.register(app, {"registry": _Reg(), "loop": loop, "check_auth": lambda: None})
        client = app.test_client()
        resp = client.post(
            "/private/open/stream",
            json={"persona": {"name": "Mia", "unfiltered": True, "unfiltered_model": "l3-8b-stheno-v3.2"}, "unfiltered": True},
        )
        body = resp.get_data(as_text=True)
        done_lines = [ln for ln in body.splitlines() if ln.startswith("data:") and '"done"' in ln]
        assert done_lines
        payload = json.loads(done_lines[-1][len("data:"):].strip())
        assert payload["ok"] is False
        assert payload["skipped"] is True
        assert isinstance(payload["error"], str) and payload["error"]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


def test_two_girls_prompt_encourages_shared_context_crosstalk_and_line_tags():
    persona = {
        "name": "Mia",
        "second": {"name": "Zoe", "character": {"hair": "blonde"}},
    }
    sp = pc.system_prompt(persona)
    assert "TWO DISTINCT women" in sp
    assert "shared context with each other" in sp
    assert "Mia: ..." in sp and "Zoe: ..." in sp

    op = pc._open_prompt(persona)
    assert "Mia: ..." in op and "Zoe: ..." in op
    assert "hanging out together" in op

    # When only Mia spoke last turn, _cast_silence_note nudges group crosstalk
    nudge = pc._cast_silence_note(persona, [("How are yall?", "Mia: Just hanging out.")])
    assert "GROUP CROSSTALK" in nudge
    assert "Zoe: ..." in nudge

    # When both Mia and Zoe spoke, no redundant crosstalk nudge
    clean = pc._cast_silence_note(
        persona,
        [("How are yall?", "Mia: Nothing much, me and Zoe were just hanging out.\nZoe: Yeah, we were talking about plans together.")],
    )
    assert clean == ""


def test_memories_block_includes_second_girl_memories(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "_MEM_DIR", tmp_path)
    pc._save_memories({"name": "Mia"}, ["Mia loves rooftop drinks"])
    pc._save_memories({"name": "Zoe"}, ["Zoe always steals Mickey's hoodie"])
    block = pc._memories_block({"name": "Mia", "second": {"name": "Zoe"}})
    assert "Mia loves rooftop drinks" in block
    assert "Zoe remembers" in block and "Zoe always steals Mickey's hoodie" in block


def test_zero_delay_puts_both_girls_in_opening_scene():
    persona = {
        "name": "Mia",
        "second": {"name": "Zoe", "character": {"clothes": "black dress"}},
        "second_waits": True,
        "wait_turns": 0,
    }
    assert pc._wants_waiting_second(persona) is False
    eff, note = pc._resolve_cast(persona, "k", "", turn_count=0)
    assert "second" in eff and note == ""
    assert "Zoe also present" in pc._initial_scene_seed(persona)
    op = pc._open_prompt(persona)
    assert "Mia: ..." in op and "Zoe: ..." in op
    assert "hanging out together" in op

    # Non-zero delay with second_waits=True still keeps her off-screen initially
    delayed = dict(persona, wait_turns=2)
    assert pc._wants_waiting_second(delayed) is True
    eff2, note2 = pc._resolve_cast(delayed, "k2", "", turn_count=0)
    assert "second" not in eff2 and "NOT in this scene" in note2


def test_sanitize_reply_removes_user_mickey_and_spoken_for_mickey_lines():
    raw = (
        "Cory Chase: Pour yourself a glass, user Mickey, we were waiting for you.\n"
        "Mickey: Sounds great, what music is playing?\n"
        "Taking a slow sip while eyeing them both.\n"
        "Maya Farrell: Oh, wait until you hear this track, the user Mickey is gonna love it."
    )
    cleaned = pc._sanitize_reply(raw, active_names=["Cory Chase", "Maya Farrell"])
    assert "user mickey" not in cleaned.lower()
    assert "Mickey:" not in cleaned
    assert "Taking a slow sip" not in cleaned
    assert "Cory Chase: Pour yourself a glass, Mickey" in cleaned
    assert "Maya Farrell: Oh, wait until you hear this track, Mickey is gonna love it." in cleaned


def test_missing_cast_speakers_and_autonomous_nudge_prompt():
    sample = "(Cory Chase) pouring red wine\n\n### Maya Farrell: grabbing the corkscrew"
    assert pc._speaker_present_in_text(sample, "Cory Chase") is True
    assert pc._speaker_present_in_text(sample, "Maya Farrell") is True
    assert pc._missing_cast_speakers("Cory Chase: Hey!", ["Cory Chase", "Maya Farrell"]) == ["Maya Farrell"]

    persona = {"name": "Cory Chase", "second": {"name": "Maya Farrell"}}
    order_note = pc._multi_girl_order_note(persona)
    assert "Cory Chase: ..." in order_note and "Maya Farrell: ..." in order_note
    assert "NEVER write 'Mickey:'" in order_note

    nudge = pc._nudge_prompt(persona, scene_state="wine bar", turn_count=2)
    assert "GROUP AUTONOMY" in nudge
    assert "Cory Chase" in nudge and "Maya Farrell" in nudge


def test_private_chat_completes_missing_second_girl_bubble():
    import threading
    from flask import Flask
    from magic_core.backends.base import ModelBackend, ModelInfo, GenerationResult

    app = Flask(__name__)
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    calls = []

    class _TwoTurnFakeBackend(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="fake", kind="local"))

        async def generate(self, prompt, system=None, max_tokens=900, temperature=0.9, history=None):
            calls.append(prompt)
            if "SANDBOXED TURN — Cory Chase ALONE" in prompt or (system and "YOU ARE STRICTLY Cory Chase ALONE" in system):
                return GenerationResult(model="fake", text="Cory Chase: Hey Mickey, we missed you — come sit with us.")
            if "SANDBOXED TURN — Maya Farrell ALONE" in prompt or (system and "YOU ARE STRICTLY Maya Farrell ALONE" in system):
                return GenerationResult(
                    model="fake",
                    text="Maya Farrell: Seriously, Cory wouldn't stop talking about your playlist until you walked in.",
                )
            return GenerationResult(model="fake", text="ok")

    be = _TwoTurnFakeBackend()

    class _Reg:
        def primary_model_name(self):
            return "fake"

        def get(self, name):
            return be if name == "fake" else None

        def all(self):
            return {"fake": be}

    try:
        pc.register(app, {"registry": _Reg(), "loop": loop, "check_auth": lambda: None})
        client = app.test_client()
        resp = client.post(
            "/private/chat",
            json={
                "message": "How are y'all doing tonight?",
                "persona": {"name": "Cory Chase", "second": {"name": "Maya Farrell"}},
            },
        )
        assert resp.status_code == 200
        ans = resp.get_json()["answer"]
        assert "Cory Chase:" in ans
        assert "Maya Farrell:" in ans
        assert any("Cory Chase ALONE" in p for p in calls)
        assert any("Maya Farrell ALONE" in p for p in calls)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


def test_per_girl_sandbox_isolates_looks_and_strips_combined_speaker_tags():
    persona = {
        "name": "Cory Chase",
        "character": {"hair": "brunette", "eyes": "green", "build": "athletic"},
        "second": {
            "name": "Maya Farrell",
            "character": {"hair": "platinum blonde", "eyes": "icy blue", "build": "petite"},
        },
        "third": {
            "name": "Elena Vance",
            "character": {"hair": "raven black", "eyes": "amber", "build": "curvy"},
        },
        "scenario": "Late-night rooftop lounge",
    }
    names = ["Cory Chase", "Maya Farrell", "Elena Vance"]
    cory_card, cory_note = pc._sandbox_persona_for_girl(persona, 0, names)
    maya_card, maya_note = pc._sandbox_persona_for_girl(persona, 1, names)
    elena_card, elena_note = pc._sandbox_persona_for_girl(persona, 2, names)

    cory_sp = pc.system_prompt(cory_card) + "\n\n" + cory_note
    maya_sp = pc.system_prompt(maya_card) + "\n\n" + maya_note
    elena_sp = pc.system_prompt(elena_card) + "\n\n" + elena_note

    assert "brunette" in cory_sp and "platinum blonde" not in cory_sp and "raven black" not in cory_sp
    assert "platinum blonde" in maya_sp and "brunette" not in maya_sp and "raven black" not in maya_sp
    assert "raven black" in elena_sp and "brunette" not in elena_sp and "platinum blonde" not in elena_sp
    assert "Late-night rooftop lounge" in cory_sp and "Late-night rooftop lounge" in maya_sp

    # Combined speaker headers and other-girl leaks inside one sandboxed turn get cleaned cleanly
    messy_cory = (
        "Cory Chase & Maya Farrell: We grabbed the wine for you!\n"
        "Maya Farrell: Wait until you taste this pinot.\n"
        "Elena Vance: Hurry up and pour!"
    )
    cleaned_cory = pc._strip_combined_or_other_speakers(messy_cory, "Cory Chase", names)
    assert cleaned_cory.startswith("Cory Chase: We grabbed the wine for you!")
    assert "Cory Chase & Maya Farrell" not in cleaned_cory
    assert "Maya Farrell:" not in cleaned_cory
    assert "Elena Vance:" not in cleaned_cory


def test_normalize_speakers_and_cap_paragraphs_for_open_and_chat():
    # Inline switch + header-only markdown + multi-paragraph wall per speaker
    sprawling = (
        "**Cory Chase**\n"
        "She pours a deep red wine and thinks about how tense tonight started. "
        "Her fingers tap the crystal stem while she waits for Mickey.\n\n"
        "She sets the second glass down by the vinyl player.\n\n"
        "Then she starts wandering down the hall talking about last summer.\n\n"
        "*Maya Farrell:* leans against the sofa watching Cory. "
        "(Cory Chase) Oh don't pretend you weren't nervous too.\n\n"
        "Maya Farrell: I never get nervous when the playlist hits right. "
        "Wait till you hear track three, Mickey.\n\n"
        "Let's grab the snacks before the rain picks up.\n\n"
        "And here is a third sprawling paragraph that should be trimmed."
    )
    chat_capped = pc._sanitize_reply(
        sprawling,
        active_names=["Cory Chase", "Maya Farrell"],
        is_open=False,
    )
    # Every inline/header speaker becomes canonical Name: and stays capped at <= 2 paragraphs per turn block
    assert "Cory Chase: She pours a deep red wine" in chat_capped
    assert "Maya Farrell: leans against the sofa" in chat_capped
    assert "talking about last summer" not in chat_capped
    assert "third sprawling paragraph" not in chat_capped

    # Startup open caps each girl strictly to 1 short paragraph (not half the story!)
    open_wall = (
        "Cory Chase: First opener paragraph setting the scene right now.\n\n"
        "Second paragraph rambling on about half the story.\n\n"
        "Maya Farrell: Maya's opening reaction right here.\n\n"
        "Second paragraph rambling from Maya too."
    )
    open_capped = pc._sanitize_reply(
        open_wall,
        active_names=["Cory Chase", "Maya Farrell"],
        is_open=True,
    )
    assert "First opener paragraph" in open_capped
    assert "Maya's opening reaction" in open_capped
    assert "rambling on about half the story" not in open_capped
    assert "rambling from Maya" not in open_capped


def test_open_prompt_short_and_story_mode_keeps_multi_girl_bubbles():
    persona = {
        "name": "Cory Chase",
        "narration": 2,  # Story mode must still enforce per-girl bubbles when 2+ girls are active
        "second": {"name": "Maya Farrell"},
    }
    sp = pc.system_prompt(persona)
    assert "Cory Chase: ..." in sp and "Maya Farrell: ..." in sp
    assert "own chat bubble" in sp
    assert "1 short paragraph" in sp

    op = pc._open_prompt(persona)
    assert "DO NOT WRITE HALF THE STORY" in op
    assert "1 brief paragraph" in op


def test_script_start_seeds_opener_base_preserving_head_and_tail():
    import threading
    from flask import Flask
    from magic_core.backends.base import ModelBackend, ModelInfo, GenerationResult

    pc._sessions.clear()
    app = Flask(__name__)
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    seen_systems = []

    class _FakeScriptBackend(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="fake", kind="local"))

        async def generate(self, prompt, system=None, max_tokens=900, temperature=0.9, history=None):
            seen_systems.append(system or "")
            return GenerationResult(model="fake", text="Cory Chase: *opens the cabin door* Welcome out of the rain.")

    be = _FakeScriptBackend()

    class _Reg:
        def primary_model_name(self):
            return "fake"

        def get(self, name):
            return be if name == "fake" else None

        def all(self):
            return {"fake": be}

    try:
        pc.register(app, {"registry": _Reg(), "loop": loop, "check_auth": lambda: None})
        client = app.test_client()
        story_text = (
            "Premise head: Cory and Maya rented the lakeside cabin for a quiet weekend.\n\n"
            + ("Middle filler scene description. " * 120)
            + "\n\nPremise tail: Storm shutters slam as Mickey walks up the porch steps."
        )
        r_start = client.post(
            "/private/script/start",
            json={
                "title": "Lake Cabin",
                "text": story_text,
                "mode": "continue",
                "persona": {"name": "Cory Chase"},
            },
        )
        assert r_start.status_code == 200 and r_start.get_json()["ok"] is True

        r_open = client.post(
            "/private/open",
            json={"persona": {"name": "Cory Chase"}, "force": True},
        )
        assert r_open.status_code == 200
        data = r_open.get_json()
        assert data["ok"] is True
        assert "opens the cabin door" in data["answer"]
        assert any("Premise head:" in s and "Premise tail:" in s for s in seen_systems)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


def test_per_speaker_reroll_and_nudge_keep_other_girls_intact():
    import threading
    from flask import Flask
    from magic_core.backends.base import ModelBackend, ModelInfo, GenerationResult

    pc._sessions.clear()
    app = Flask(__name__)
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    step = {"i": 0}

    class _FakeMultiSpeakerBackend(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="fake", kind="local"))

        async def generate(self, prompt, system=None, max_tokens=900, temperature=0.9, history=None):
            step["i"] += 1
            if "SINGLE-CHARACTER RE-ROLL" in prompt:
                return GenerationResult(model="fake", text="Maya Farrell: Fresh replacement Maya bubble!")
            if "SPOTLIGHT NUDGE" in prompt:
                return GenerationResult(model="fake", text="Cory Chase: Quick solo follow-up from Cory!")
            if "SANDBOXED TURN — Cory Chase ALONE" in prompt:
                return GenerationResult(model="fake", text="Cory Chase: Original Cory bubble.")
            if "SANDBOXED TURN — Maya Farrell ALONE" in prompt:
                return GenerationResult(model="fake", text="Maya Farrell: Original Maya bubble.")
            return GenerationResult(model="fake", text="ok")

    be = _FakeMultiSpeakerBackend()

    class _Reg:
        def primary_model_name(self):
            return "fake"

        def get(self, name):
            return be if name == "fake" else None

        def all(self):
            return {"fake": be}

    persona = {"name": "Cory Chase", "second": {"name": "Maya Farrell"}}
    try:
        pc.register(app, {"registry": _Reg(), "loop": loop, "check_auth": lambda: None})
        client = app.test_client()
        r1 = client.post("/private/chat", json={"message": "Hey girls", "persona": persona})
        assert r1.status_code == 200
        assert "Original Cory bubble." in r1.get_json()["answer"]
        assert "Original Maya bubble." in r1.get_json()["answer"]

        r_reroll = client.post("/private/reroll_speaker", json={"speaker_name": "Maya Farrell", "persona": persona})
        assert r_reroll.status_code == 200
        ans_r = r_reroll.get_json()["answer"]
        assert "Cory Chase: Original Cory bubble." in ans_r
        assert "Maya Farrell: Fresh replacement Maya bubble!" in ans_r
        assert "Original Maya bubble." not in ans_r

        r_nudge = client.post("/private/nudge_speaker", json={"speaker_name": "Cory Chase", "persona": persona})
        assert r_nudge.status_code == 200
        ans_n = r_nudge.get_json()["answer"]
        assert "Cory Chase: Quick solo follow-up from Cory!" in ans_n
        assert "Maya Farrell:" not in ans_n
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


def test_multi_girl_stays_on_topic_and_girls_interact_with_each_other_and_mickey():
    import threading
    from flask import Flask
    from magic_core.backends.base import ModelBackend, ModelInfo, GenerationResult

    pc._sessions.clear()
    app = Flask(__name__)
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    recorded_calls = []

    class _OnTopicTwoGirlBackend(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="fake", kind="local"))

        async def generate(self, prompt, system=None, max_tokens=900, temperature=0.9, history=None):
            recorded_calls.append({"prompt": prompt, "system": system or "", "history": list(history or [])})
            if "SANDBOXED TURN — Maya Farrell ALONE" in prompt:
                return GenerationResult(
                    model="fake",
                    text="Maya: *I nudge Cory's shoulder and hold up the sci-fi thriller case for Mickey.* \"Cory and I picked Blade Runner for tonight — tell him why you insisted, Cory.\"",
                )
            if "SANDBOXED TURN — Cory Chase ALONE" in prompt:
                return GenerationResult(
                    model="fake",
                    text="Cory: *I laugh at Maya and pat the cushion between us for you.* \"Because Maya always steals the blanket during the neon rain scene, Mickey.\"",
                )
            return GenerationResult(model="fake", text="SCENE: Couch movie night")

    be = _OnTopicTwoGirlBackend()

    class _Reg:
        def primary_model_name(self):
            return "fake"

        def get(self, name):
            return be if name == "fake" else None

        def all(self):
            return {"fake": be}

    persona = {
        "name": "Cory Chase",
        "two_dynamic": "best friends who tease each other constantly",
        "second": {"name": "Maya Farrell"},
    }
    try:
        pc.register(app, {"registry": _Reg(), "loop": loop, "check_auth": lambda: None})
        client = app.test_client()
        resp = client.post(
            "/private/chat",
            json={
                "message": "Maya, what movie did you and Cory pick for tonight?",
                "persona": persona,
            },
        )
        assert resp.status_code == 200
        ans = resp.get_json()["answer"]
        # Addressed girl (Maya) speaks first, followed by Cory reacting to Maya + Mickey
        assert ans.index("Maya Farrell:") < ans.index("Cory Chase:")
        assert "Cory Chase: Cory:" not in ans
        assert "Maya Farrell: Maya:" not in ans

        # Filter to the 2 sandboxed character calls (excluding post-turn scene_state extraction)
        sandbox_calls = [c for c in recorded_calls if "SANDBOXED TURN" in c["prompt"]]
        assert len(sandbox_calls) == 2
        first_call, second_call = sandbox_calls[0], sandbox_calls[1]
        assert "SANDBOXED TURN — Maya Farrell ALONE" in first_call["prompt"]
        assert "THREE-WAY INTERACTION & ON-TOPIC LOCK" in first_call["system"]
        assert "best friends who tease each other constantly" in first_call["system"]

        # Second speaker sees what the first speaker JUST said inside the turn prompt (not out-of-order in history)
        assert "SANDBOXED TURN — Cory Chase ALONE" in second_call["prompt"]
        assert "[JUST SPOKEN IN THIS TURN RIGHT BEFORE YOU:" in second_call["prompt"]
        assert "Blade Runner" in second_call["prompt"]
        assert second_call["history"] == []
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


def test_strip_combined_or_other_speakers_splits_inline_switches_and_first_name_aliases():
    names = ["Cory Chase", "Maya Farrell"]
    raw = (
        "Cory: *I clink my glass against Maya's and look at you.* \"We were just talking about that concert.\" "
        "Maya: \"Yeah, I told Cory you knew the opening band!\"\n\n"
        "Cory Chase: \"So tell us which song was your favorite.\""
    )
    cleaned = pc._strip_combined_or_other_speakers(raw, "Cory Chase", names)
    assert cleaned.startswith("Cory Chase: *I clink my glass")
    assert "Cory Chase: Cory:" not in cleaned
    assert "Maya" not in cleaned or "Maya Farrell:" not in cleaned
    assert "opening band" not in cleaned
    # Second paragraph keeps its content without repeating a duplicate 'Cory Chase:' header
    assert "So tell us which song was your favorite." in cleaned
    assert cleaned.count("Cory Chase:") == 1


def test_active_whispers_and_multi_girl_remember_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "_MEM_DIR", tmp_path)
    persona = {"name": "Cory Chase", "second": {"name": "Maya Farrell"}}
    pc._capture_memory(persona, "Please remember that I love smoky mezcal cocktails.")
    assert "I love smoky mezcal cocktails" in pc._load_memories({"name": "Cory Chase"})
    assert "I love smoky mezcal cocktails" in pc._load_memories({"name": "Maya Farrell"})

    pc._sessions["w_key"] = {
        "turns": [("@Maya (quietly) meet me on the balcony in five", "Maya Farrell: *catches your eye and nods subtly*")],
        "active_whispers": {"Maya": "meet me on the balcony in five"},
    }
    block = pc._scene_state_block("w_key")
    assert "to Maya: meet me on the balcony in five" in block


def test_multi_girl_sse_stream_prefixes_live_deltas_and_chains_girl_turns(monkeypatch):
    import threading
    from flask import Flask
    from magic_core.backends.base import ModelBackend, ModelInfo, GenerationResult

    pc._sessions.clear()
    app = Flask(__name__)
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    stream_prompts = []

    class _StreamingTwoGirlBackend(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="fake", kind="local"))

        async def generate(self, prompt, system=None, max_tokens=900, temperature=0.9, history=None):
            return GenerationResult(model="fake", text="SCENE: Living room couch")

        async def generate_stream(self, prompt, system=None, max_tokens=900, temperature=0.9, history=None):
            stream_prompts.append(prompt)
            if "SANDBOXED TURN — Maya Farrell ALONE" in prompt:
                # Deliberately omit 'Maya Farrell:' prefix to verify live stream delta auto-prefixing
                yield "*I turn to Cory and grin before answering you.* "
                yield "\"We were just debating which vinyl record to put on next, right Cory?\""
            elif "SANDBOXED TURN — Cory Chase ALONE" in prompt:
                yield "Cory Chase: *I bump Maya's hip playfully and look at Mickey.* "
                yield "\"Maya wanted synthwave, I voted for slow jazz — break the tie for us.\""

    be = _StreamingTwoGirlBackend()

    class _Reg:
        def primary_model_name(self):
            return "fake"

        def get(self, name):
            return be if name == "fake" else None

        def all(self):
            return {"fake": be}

    persona = {"name": "Cory Chase", "second": {"name": "Maya Farrell"}}
    try:
        monkeypatch.setattr(vram, "ensure_only", lambda reg, keep=None: asyncio.sleep(0))
        pc.register(app, {"registry": _Reg(), "loop": loop, "check_auth": lambda: None})
        client = app.test_client()
        resp = client.post(
            "/private/chat/stream",
            json={
                "message": "Maya, what music were you and Cory picking out?",
                "persona": persona,
            },
        )
        body = resp.get_data(as_text=True)
        events = [
            json.loads(ln[len("data:"):].strip())
            for ln in body.splitlines()
            if ln.startswith("data:")
        ]
        deltas = "".join(ev.get("text", "") for ev in events if ev.get("type") == "delta")
        done_ev = next(ev for ev in events if ev.get("type") == "done")
        assert deltas.startswith("Maya Farrell: *I turn to Cory")
        assert "\n\nCory Chase: *I bump Maya's hip" in deltas
        assert done_ev["ok"] is True
        assert "Maya Farrell: *I turn to Cory" in done_ev["answer"]
        assert "Cory Chase: *I bump Maya's hip" in done_ev["answer"]
        assert len(stream_prompts) == 2
        assert "[JUST SPOKEN IN THIS TURN RIGHT BEFORE YOU:" in stream_prompts[1]
        assert "which vinyl record to put on next" in stream_prompts[1]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
