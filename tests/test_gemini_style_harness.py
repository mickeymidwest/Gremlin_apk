"""Tests for Gemini-style research-before-answering, multi-source web gather,
and self-editing harness enhancements (scout, dirty-DONE verify gate, line hints)."""
import asyncio
from pathlib import Path

from magic_core import autoresearch, look
from magic_core.backends.base import GenerationResult, ModelBackend, ModelInfo
from magic_core.magic import reply
from magic_core.magic.battle import run_battle
from magic_core.magic.model import ModelReply
from magic_core.magic.toolhost import ShellToolHost, ToolCall
from magic_core.magic.types import Task


def test_workspace_probe_finds_symbols_and_skips_private_dirs(tmp_path):
    (tmp_path / "app.py").write_text(
        "def compute_invoice_total(items):\n    return sum(i.price for i in items)\n"
    )
    priv = tmp_path / "private_saved"
    priv.mkdir()
    (priv / "secret.md").write_text("def compute_invoice_total():\n    secret\n")

    keys, ev = look.workspace_probe("Where is `compute_invoice_total` implemented?", str(tmp_path))
    assert keys == ["repo"]
    assert "app.py" in ev and "compute_invoice_total" in ev
    assert "private_saved" not in ev


def test_autoresearch_retries_condensed_query_and_reads_multiple_sources(monkeypatch):
    queries = []
    fetched = []

    def fake_search(q, limit=3):
        queries.append(q)
        if len(queries) == 1:
            return []  # verbose query misses
        return [
            {
                "title": "Steam Deck OLED 2026 Price and Review",
                "url": "https://alpha.example.com/a",
                "snippet": "Valve's Steam Deck OLED handheld gaming PC starts at $549 USD for the 512GB model in 2026.",
            },
            {
                "title": "Asus ROG Ally X 2026 Comparison Review",
                "url": "https://beta.example.com/b",
                "snippet": "The Asus ROG Ally X handheld gaming system lists around $799 USD with an upgraded 80Wh battery in 2026.",
            },
        ]

    def fake_fetch(url, max_chars=9000):
        fetched.append(url)
        return (
            f"Detailed Steam Deck vs Asus ROG Ally latest 2026 comparison from {url} "
            "covering pricing tiers, Ryzen Z1 Extreme benchmark speed, battery life, and OLED screen specs."
        )

    monkeypatch.setattr(autoresearch.web_search, "search", fake_search)
    monkeypatch.setattr(autoresearch.web_search, "fetch_text", fake_fetch)

    sources, ev = asyncio.run(
        autoresearch.gather("What is the price of Steam Deck vs Asus ROG Ally latest 2026?")
    )
    assert len(queries) == 2
    assert len(fetched) == 2
    assert len(sources) == 2
    assert "alpha.example.com" in ev and "beta.example.com" in ev


def test_reply_grounds_codebase_question_with_workspace_probe(tmp_path):
    (tmp_path / "calc.py").write_text("def magic_tax_rate():\n    return 0.0825\n")

    class _EchoBackend(ModelBackend):
        def __init__(self):
            super().__init__(ModelInfo(name="fake", kind="local"))
            self.seen_history = []

        async def generate(self, prompt, system=None, max_tokens=512, temperature=0.6, history=None):
            self.seen_history = history or []
            return GenerationResult(model="fake", text="`calc.py` sets `magic_tax_rate()` to `0.0825`.")

    be = _EchoBackend()
    out = asyncio.run(
        reply.answer(be, "Where is `magic_tax_rate` defined in this repo?", str(tmp_path))
    )
    assert "calc.py" in out["answer"]
    assert out["looked"] == ["repo"]
    assert any("magic_tax_rate" in str(m.get("content", "")) for m in be.seen_history)


def test_toolhost_edit_miss_includes_line_numbers_and_view_hint(tmp_path):
    (tmp_path / "m.py").write_text("alpha = 1\nbeta = 2\ngamma = 3\n")
    th = ShellToolHost(str(tmp_path))
    res = th.run(ToolCall(name="edit_file", args={"path": "m.py", "search": "beta = 999", "replace": "beta = 3"}))
    assert not res.ok
    assert "view_file(" in res.output and "2: beta = 2" in res.output


def test_battle_initial_scout_and_done_verify_gate(tmp_path):
    (tmp_path / "m.py").write_text("def compute_add(a, b):\n    return a - b\n")
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "test_m.py").write_text("from m import compute_add\ndef test_add():\n    assert compute_add(2, 3) == 5\n")

    responses = [
        # Step 1: explore (phase gate unlock)
        'ACTION: read_file\n```json\n{"path": "m.py"}\n```',
        # Step 2: still-buggy edit
        'ACTION: edit_file\n```json\n{"path": "m.py", "search": "    return a - b", "replace": "    return a * b"}\n```',
        # Step 3: model tries to say DONE while test fails -> harness must reject
        'DONE\nFixed compute_add.',
        # Step 4: after REJECTED, model fixes properly
        'ACTION: edit_file\n```json\n{"path": "m.py", "search": "    return a * b", "replace": "    return a + b"}\n```',
        # Step 5: DONE now passes verify_cmd!
        'DONE\nReally fixed compute_add.',
    ]

    class _FakeModel:
        def __init__(self):
            self.idx = 0
            self.openings = []

        def complete(self, messages, system="", max_tokens=1024, grammar=None):
            if not self.openings:
                self.openings.append(messages[0]["content"])
            txt = responses[self.idx]
            self.idx += 1
            return ModelReply(text=txt)

    fm = _FakeModel()
    # Non-pytest verify_cmd disables auto-check so Step 3 DONE arrives with dirty unverified edit
    task = Task(
        id="t-scout",
        prompt="Fix `compute_add` in `m.py` so tests pass.",
        verify_cmd="python3 -B -c 'import m; assert m.compute_add(2, 3) == 5'",
    )
    tr = run_battle(task, str(tmp_path), fm, skills=[], facts=[], plan=False, autocommit=False)
    assert "INITIAL LOCATE SCOUT" in fm.openings[0]
    assert "SYMBOL HITS" in fm.openings[0] and "m.py" in fm.openings[0]
    assert "Really fixed compute_add." in tr.final_message
    assert (tmp_path / "m.py").read_text() == "def compute_add(a, b):\n    return a + b\n"


def test_toolhost_read_files_find_callers_and_mcp_read(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def helper_x():\n    return 1\n")
    (tmp_path / "b.py").write_text("from a import helper_x\nval = helper_x()\n")

    host = ShellToolHost(str(tmp_path))

    # read_files: batching + path jail
    batch = host.run(ToolCall(name="read_files", args={
        "paths": [{"path": "a.py", "start": 1, "end": 2}, "b.py", "../outside.py"]
    }))
    assert batch.ok
    assert "a.py [1-2" in batch.output and "def helper_x" in batch.output
    assert "=== b.py ===" in batch.output and "val = helper_x()" in batch.output
    assert "escapes" in batch.output  # escaped path rejected inside batch report

    # find_callers: defs + call sites
    callers = host.run(ToolCall(name="find_callers", args={"symbol": "helper_x"}))
    assert callers.ok
    assert "DEFINITIONS:" in callers.output and "a.py:1:" in callers.output
    assert "REFERENCES:" in callers.output and "b.py:2:" in callers.output


def test_toolhost_mcp_read(tmp_path, monkeypatch):
    from magic_core import tools as mc_tools

    host = ShellToolHost(str(tmp_path))

    class _FakeTool:
        def __init__(self, name, cap, fn):
            self.name = name
            self.capability = cap
            self.handler = fn

    async def async_ping(args, ctx):
        return {"answer": f"pong:{args.get('q', '')}", "ok": True}

    monkeypatch.setitem(mc_tools.REGISTRY._tools, "mcp_ro_ping", _FakeTool("mcp_ro_ping", "read_only", async_ping))
    monkeypatch.setitem(mc_tools.REGISTRY._tools, "mcp_rw_wipe", _FakeTool("mcp_rw_wipe", "write", lambda args, ctx: "wiped"))

    bad_missing = host.run(ToolCall(name="mcp_read", args={"tool": "mcp_nope"}))
    assert not bad_missing.ok

    bad_cap = host.run(ToolCall(name="mcp_read", args={"tool": "mcp_rw_wipe"}))
    assert not bad_cap.ok and "read_only" in bad_cap.output

    good_mcp = host.run(ToolCall(name="mcp_read", args={"tool": "mcp_ro_ping", "args": {"q": "ok"}}))
    assert good_mcp.ok and "pong:ok" in good_mcp.output


def test_battle_on_step_and_revert_to_best(tmp_path):
    import subprocess
    (tmp_path / "m.py").write_text("def add(a, b):\n    return 0\n\ndef mul(a, b):\n    return 0\n")
    (tmp_path / "test_m.py").write_text(
        "import m\ndef test_add():\n    assert m.add(2, 3) == 5\ndef test_mul():\n    assert m.mul(2, 3) == 6\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    responses = [
        'ACTION: read_file\n```json\n{"path": "m.py"}\n```',
        'ACTION: edit_file\n```json\n{"path": "m.py", "old": "def add(a, b):\\n    return 0", "new": "def add(a, b):\\n    return a + b"}\n```',
        'ACTION: edit_file\n```json\n{"path": "m.py", "old": "return a + b", "new": "return 999"}\n```',
        'ACTION: revert_to_best\n```json\n{}\n```',
        'ACTION: edit_file\n```json\n{"path": "m.py", "old": "def mul(a, b):\\n    return 0", "new": "def mul(a, b):\\n    return a * b"}\n```',
        'DONE: Both add and mul fixed cleanly.',
    ]

    class _FakeModel:
        def __init__(self):
            self.idx = 0
            self.seen_regression = False

        def complete(self, messages, system="", max_tokens=1024, grammar=None):
            joined = "\n".join(str(m.get("content", "")) for m in messages)
            if "REGRESSION:" in joined and "revert_to_best" in joined:
                self.seen_regression = True
            txt = responses[self.idx]
            self.idx += 1
            return ModelReply(text=txt)

    fm = _FakeModel()
    events = []
    task = Task(
        id="t-best",
        prompt="Fix `add` and `mul` in `m.py`.",
        verify_cmd="pytest -q test_m.py",
    )
    tr = run_battle(
        task, str(tmp_path), fm, skills=[], facts=[],
        plan=False, autocommit=True, on_step=events.append,
    )
    assert fm.seen_regression
    assert "Both add and mul fixed cleanly." in tr.final_message
    assert (tmp_path / "m.py").read_text() == "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
    kinds = [e["kind"] for e in events]
    assert "scout" in kinds and "tool" in kinds and "auto_check" in kinds and "done" in kinds
    tag_check = subprocess.run(["git", "tag", "-l", "magic-best"], cwd=tmp_path, capture_output=True, text=True)
    assert "magic-best" in tag_check.stdout


def test_qa_verifier_scores_facts_and_abstention():
    from magic_core.magic.verifier import QAVerifier

    qa = QAVerifier()
    good = qa.score(
        "The port is 8080 and `run_battle` executes the loop.",
        expected_terms=["8080", "run_battle"],
        forbidden_terms=["9999"],
    )
    assert good.value == 1.0 and not good.failure_signal

    honest_unknown = qa.score("I cannot find that value in the current repo files.", unanswerable=True)
    assert honest_unknown.value == 1.0

    hallucinated = qa.score("The latency is 123 ms on port 4567.", unanswerable=True)
    assert hallucinated.value == 0.0 and "uncertainty" in hallucinated.failure_signal


def test_service_control_restart_and_verify(monkeypatch):
    from types import SimpleNamespace
    from magic_core import service_control

    async def fake_restart(kind, name):
        return SimpleNamespace(ok=True, stdout=f"restarted {name}", stderr="")

    async def fake_status_ok(kind, name):
        return SimpleNamespace(ok=True, stdout=f"● {name} - Active: active (running)", stderr="")

    async def fake_status_bad(kind, name):
        return SimpleNamespace(ok=True, stdout=f"● {name} - Active: failed (Result: exit-code)", stderr="")

    class _Resp:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(service_control, "restart", fake_restart)
    monkeypatch.setattr(service_control, "get_status", fake_status_ok)
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=4.0: _Resp())

    ok, msg = asyncio.run(service_control.restart_and_verify(
        "/tmp", "systemd", "magic.service", health_url="http://127.0.0.1:9000/status"
    ))
    assert ok and "verified healthy" in msg

    monkeypatch.setattr(service_control, "get_status", fake_status_bad)
    ok2, msg2 = asyncio.run(service_control.restart_and_verify(
        "/tmp", "systemd", "magic.service", health_url=None
    ))
    assert not ok2 and "post-check failed" in msg2


def test_reply_plan_missing_triggers_workspace_probe(monkeypatch, tmp_path):
    from magic_core.magic import planner

    (tmp_path / "magic_core").mkdir()
    (tmp_path / "magic_core" / "sample.py").write_text("def sample_magic_fn():\n    return 42\n")

    class _FakeBackend(ModelBackend):
        def __init__(self):
            self.prompts = []
            self.histories = []
        async def generate(self, prompt, **kw):
            self.prompts.append(prompt)
            self.histories.append(kw.get("history") or [])
            return GenerationResult(text="sample_magic_fn returns 42.", model="fake")
        async def info(self):
            return ModelInfo(name="fake", family="qwen", context_window=4096)

    be = _FakeBackend()
    probe_calls = []

    def fake_probe(q, root="."):
        probe_calls.append(q)
        if len(probe_calls) == 1:
            return [], ""
        return ["repo"], "workspace probe: magic_core/sample.py defines sample_magic_fn -> 42"

    async def fake_gather(msg, history=""):
        return None, ""

    async def fake_triage(primary, question, evidence="", grounded=False):
        return "PLAN: check sample.py. Note: You do NOT actually have this current/live info yet."

    monkeypatch.setattr(reply, "_PLAN", True)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(autoresearch, "gather", fake_gather)
    monkeypatch.setattr(look, "wants", lambda q, r=".": [])
    monkeypatch.setattr(look, "workspace_probe", fake_probe)
    monkeypatch.setattr(planner, "plan_note", fake_triage)

    res = asyncio.run(reply.answer(
        be, "Explain how `sample_magic_fn` works", str(tmp_path)
    ))
    assert len(probe_calls) >= 2 and any("sample_magic_fn" in c for c in probe_calls)
    joined_hist = "\n".join(str(m.get("content", "")) for m in be.histories[-1])
    assert "workspace probe:" in joined_hist
    assert "sample_magic_fn returns 42." in res["answer"]


def test_campaign_ignores_build_dirs():
    from magic_core.magic import campaign

    ignored = campaign._IGNORE(".", ["build", ".gradle", "hf_cache", ".finetune-venv", "node_modules", "magic_core"])
    for d in ("build", ".gradle", "hf_cache", ".finetune-venv", "node_modules"):
        assert d in ignored
    assert "magic_core" not in ignored


def test_build_loop_use_battle(monkeypatch, tmp_path):
    import subprocess
    from magic_core import build_loop

    (tmp_path / "m.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    from types import SimpleNamespace

    class _FakeRouter:
        class registry:
            @staticmethod
            def get(name):
                return SimpleNamespace(info=SimpleNamespace(kind="api"))
        async def generate(self, *a, **kw):
            return GenerationResult(text='{"verdict": "approve", "summary": "ok"}', model="fake")

    async def fake_run_step_with_battle(router, model_names, root, step_goal, *, verify_cmd="", step_budget=8, on_event=None, store_root=None):
        (Path(root) / "m.py").write_text("x = 2\n")
        subprocess.run(["git", "add", "m.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "battle edit"], cwd=root, check=True)
        return True, "updated x = 2"

    monkeypatch.setattr(build_loop, "run_step_with_battle", fake_run_step_with_battle)

    steps = [build_loop.BuildStep(index=1, title="bump x", description="change x to 2", files=["m.py"])]
    res = asyncio.run(build_loop.run_build_loop(
        _FakeRouter(), str(tmp_path), "bump x", ["fake"],
        mode="build", steps=steps, use_battle=True, run_tests=False, skip_review=True,
    ))
    assert res.ok
    assert res.outcomes[0].status == "committed"
    assert "m.py" in res.files_changed


def test_resource_cgroup_throttling(monkeypatch):
    from magic_core import resource

    resource._can_cgroup_scope.cache_clear()
    monkeypatch.setattr(resource, "_can_cgroup_scope", lambda: False)
    assert resource.cgroup_argv(["pytest", "-q"]) == ["pytest", "-q"]
    assert resource.cgroup_shell_cmd("pytest -q") == "pytest -q"

    monkeypatch.setattr(resource, "_can_cgroup_scope", lambda: True)
    wrapped = resource.cgroup_argv(["pytest", "-q"], cpu_quota="60%", memory_high="2000M")
    assert wrapped[:6] == ["systemd-run", "--user", "--scope", "-q", "-pCPUQuota=60%", "-pMemoryHigh=2000M"]
    assert wrapped[-2:] == ["pytest", "-q"]
    sh = resource.cgroup_shell_cmd("pytest -q test_x.py")
    assert "systemd-run --user --scope" in sh and "bash -c" in sh


def test_look_journal_and_zram_probes():
    assert "journal" in look.wants("Check my service logs right now for errors")
    assert "memory" in look.wants("How is my zram swap usage looking right now?")


def test_auto_verifier_dispatches_qa_tasks():
    from magic_core.magic.verifier import AutoVerifier
    from magic_core.magic.types import Transcript

    av = AutoVerifier()
    t = Task(id="qa1", prompt="Check version?", verify_cmd='qa:{"expected_terms": ["4.53"], "unanswerable": false}')
    good = av.score(t, ".", Transcript(task_id="qa1", final_message="Current APK is 4.53."))
    assert good.value == 1.0

    bad = av.score(
        Task(id="qa2", prompt="Secret?", verify_cmd='qa:{"unanswerable": true}'),
        ".",
        Transcript(task_id="qa2", final_message="It is definitely 42."),
    )
    assert bad.value == 0.0


def test_reply_stream_fallback_grounding_and_chat_agent_default(monkeypatch, tmp_path):
    assert reply._CHAT_AGENT is True

    class _StreamFailBackend(ModelBackend):
        def __init__(self):
            pass
        async def generate(self, prompt, **kw):
            return GenerationResult(text="", error="down", model="magic", ok=False)
        async def stream(self, prompt, **kw):
            raise RuntimeError("stream boom")
            yield ""
        async def info(self):
            return ModelInfo(name="magic", family="qwen", context_window=4096)

    class _FallbackBackend(ModelBackend):
        def __init__(self):
            pass
        async def generate(self, prompt, **kw):
            return GenerationResult(text="See nonexistent_dir/fake_file.py for details.", model="gemini")
        async def info(self):
            return ModelInfo(name="gemini", family="gemini", context_window=8192)

    monkeypatch.setattr(reply, "_PLAN", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", True)
    async def fake_gather(msg, history=""):
        return None, ""
    monkeypatch.setattr(autoresearch, "gather", fake_gather)
    monkeypatch.setattr(look, "wants", lambda q, r=".": [])
    monkeypatch.setattr(look, "workspace_probe", lambda q, r=".": ([], ""))

    async def _collect():
        deltas, done = [], None
        async for ev, data in reply.answer_stream(
            _StreamFailBackend(), "Where is foo?", str(tmp_path), fallback=_FallbackBackend()
        ):
            if ev == "delta":
                deltas.append(data)
            elif ev == "done":
                done = data
        return deltas, done

    deltas, done = asyncio.run(_collect())
    joined = "".join(deltas)
    assert "See nonexistent_dir/fake_file.py for details." in joined
    assert len(joined) > len("See nonexistent_dir/fake_file.py for details.")
    assert done["source"] == "gemini"


def test_llamacpp_ram_cache_attached(monkeypatch, tmp_path):
    from magic_core.backends import llamacpp_backend as lb

    attached = []

    class _FakeLLM:
        def set_cache(self, c):
            attached.append(c)

    class _FakeCache:
        def __init__(self, capacity_bytes=0):
            self.capacity_bytes = capacity_bytes

    monkeypatch.setattr(lb, "Llama", lambda **kw: _FakeLLM())
    monkeypatch.setattr(lb, "LlamaRAMCache", _FakeCache)
    monkeypatch.setenv("MAGIC_KV_RAM_MB", "128")

    be = lb.LlamaCppBackend(ModelInfo(name="t", kind="local"), str(tmp_path / "m.gguf"))
    asyncio.run(be.warmup())
    assert len(attached) == 1
    assert attached[0].capacity_bytes == 128 * 1024 * 1024


def test_tool_service_restart_checks_http_status(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from magic_core import service_control, tools as tools_mod

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("service_control:\n  systemd:\n    - magic.service\n")
    ctx = tools_mod.ExecContext(router=None, registry=None, project_root=str(tmp_path))

    async def fake_restart(kind, name):
        return SimpleNamespace(ok=True, stdout="restarted", stderr="", exit_code=0, timed_out=False)

    async def fake_status(kind, name):
        return SimpleNamespace(ok=True, stdout="Active: active (running)", stderr="", exit_code=0, timed_out=False)

    monkeypatch.setattr(service_control, "restart", fake_restart)
    monkeypatch.setattr(service_control, "get_status", fake_status)
    monkeypatch.setattr(tools_mod, "SERVICE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=4.0: (_ for _ in ()).throw(OSError("conn refused")))

    out = asyncio.run(tools_mod._tool_service_restart({"name": "magic.service"}, ctx))
    assert out["ok"] is False
    assert out["receipt"]["status"] != "verified"


def test_build_loop_self_improve_battle_blocks_protected_path(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace
    from magic_core import build_loop

    (tmp_path / "magic_core").mkdir()
    (tmp_path / "magic_core" / "pregate.py").write_text("SAFE = True\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    class _FakeRouter:
        class registry:
            @staticmethod
            def get(name):
                return SimpleNamespace(info=SimpleNamespace(kind="api"))

    async def sneaky_battle(router, model_names, root, step_goal, *, verify_cmd="", step_budget=8, on_event=None, store_root=None):
        (Path(root) / "magic_core" / "pregate.py").write_text("SAFE = False\n")
        subprocess.run(["git", "add", "magic_core/pregate.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "weaken pregate"], cwd=root, check=True)
        return True, "edited pregate"

    monkeypatch.setattr(build_loop, "run_step_with_battle", sneaky_battle)
    steps = [build_loop.BuildStep(index=1, title="sneaky", description="touch pregate", files=["magic_core/pregate.py"])]
    res = asyncio.run(build_loop.run_build_loop(
        _FakeRouter(), str(tmp_path), "touch pregate", ["fake"],
        mode="self_improve", steps=steps, use_battle=True, run_tests=False, skip_review=True,
    ))
    assert res.ok is False
    assert res.outcomes[0].status == "failed"
    assert (tmp_path / "magic_core" / "pregate.py").read_text() == "SAFE = True\n"


def test_battle_injects_error_ledger_pitfalls_and_magic_start_fallback(tmp_path):
    import subprocess
    from magic_core import error_ledger

    (tmp_path / "m.py").write_text("val = 1\n")
    error_ledger.record_watchdog(str(tmp_path), "check m", ["Claimed to inspect m.py when it could not have"])
    error_ledger.record_watchdog(str(tmp_path), "check m", ["Fabricated m.py read output"])

    responses = [
        'ACTION: read_file\n```json\n{"path": "m.py"}\n```',
        'ACTION: edit_file\n```json\n{"path": "m.py", "old": "val = 1", "new": "val = 999"}\n```',
        'ACTION: revert_to_best\n```json\n{}\n```',
        'DONE: Reverted safely to baseline.',
    ]

    class _FakeModel:
        def __init__(self):
            self.idx = 0
            self.openings = []
        def complete(self, messages, **kw):
            if not self.openings:
                self.openings.append(messages[0]["content"])
            t = responses[self.idx]; self.idx += 1
            return ModelReply(text=t)

    fm = _FakeModel()
    tr = run_battle(Task(id="t-start", prompt="Update val in m.py"), str(tmp_path), fm, [], [], plan=False, autocommit=True)
    assert "KNOWN PITFALLS ON THIS REPO" in fm.openings[0]
    assert "Reverted safely" in tr.final_message
    assert (tmp_path / "m.py").read_text() == "val = 1\n"
    tags = subprocess.run(["git", "-C", str(tmp_path), "tag", "-l", "magic-start"], capture_output=True, text=True)
    assert "magic-start" in tags.stdout


def test_step_discovery_digest_and_run_step_with_battle_wiring(tmp_path):
    from types import SimpleNamespace
    from magic_core import build_loop
    from magic_core.magic.store import Store
    from magic_core.magic.types import Fact, Skill

    (tmp_path / "helper_mod.py").write_text("class DataStore:\n    pass\n\ndef compute_z():\n    return 7\n")
    step1 = build_loop.BuildStep(index=1, title="add helper_mod", description="add helper_mod")
    step2 = build_loop.BuildStep(index=2, title="wire helper", description="use compute_z")
    o1 = build_loop.StepOutcome(step=step1, status="committed", commit_sha="abc", files_changed=["helper_mod.py"])
    prompt = build_loop._step_goal_prompt("build app", step2, 2, [o1], root=str(tmp_path))
    assert "helper_mod.py: class DataStore, def compute_z" in prompt

    # Verify run_step_with_battle loads real Store skills/facts + on_done verifier
    st = Store(tmp_path)
    st.write_facts([Fact(id="f1", text="project uses pytest -q")])
    st.write_skills([Skill(
        id="sk1", name="read-before-edit", purpose="read file before changing return values",
        trigger_when="when changing return values", trigger_matcher=r"return|pytest|m\.x",
        procedure=["read file first"], status="active",
    )])
    (tmp_path / "m.py").write_text("def x(): return 1\n")
    (tmp_path / "test_x.py").write_text("import m\ndef test_x(): assert m.x() == 2\n")

    seen_systems = []

    class _FakeBE:
        info = ModelInfo(name="fake", kind="api")
        async def generate(self, prompt, **kw):
            seen_systems.append(kw.get("system") or "")
            return GenerationResult(text='DONE: finished', model="fake")

    class _FakeRouter:
        class registry:
            @staticmethod
            def get(name):
                return _FakeBE() if name == "fake" else None

    # DONE is rejected when verify_cmd="pytest -q test_x.py" fails -> battle runs out budget -> returns False
    ok, msg = asyncio.run(build_loop.run_step_with_battle(
        _FakeRouter(), ["fake"], str(tmp_path), "make m.x return 2",
        verify_cmd="pytest -q test_x.py", step_budget=2, store_root=str(tmp_path),
    ))
    assert "project uses pytest -q" in "\n".join(seen_systems)
    assert "read-before-edit" in "\n".join(seen_systems)
    assert ok is False and "gave up" in msg
    assert ok is False and "gave up" in msg


def test_reply_stream_uses_shared_prepare_turn_plan_escalation(monkeypatch, tmp_path):
    from magic_core.magic import planner

    class _StreamBackend(ModelBackend):
        def __init__(self):
            self.histories = []
        async def generate(self, prompt, **kw):
            return GenerationResult(text="streamed fallback", model="magic")
        async def generate_stream(self, prompt, **kw):
            self.histories.append(kw.get("history") or [])
            yield "sample_magic_fn returns 42."
        async def info(self):
            return ModelInfo(name="magic", kind="local")

    be = _StreamBackend()
    probe_calls = []

    def fake_probe(q, root="."):
        probe_calls.append(q)
        return ([], "") if len(probe_calls) == 1 else (["repo"], "workspace probe: found 42")

    async def fake_gather(msg, history=""):
        return None, ""

    async def fake_triage(primary, question, evidence="", grounded=False):
        return "PLAN: inspect. Note: You do NOT actually have this current/live info yet."

    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_PLAN", True)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(autoresearch, "gather", fake_gather)
    monkeypatch.setattr(look, "wants", lambda q, r=".": [])
    monkeypatch.setattr(look, "workspace_probe", fake_probe)
    monkeypatch.setattr(planner, "plan_note", fake_triage)

    async def _go():
        deltas, done = [], None
        async for ev, d in reply.answer_stream(be, "Explain how `sample_magic_fn` works", str(tmp_path)):
            if ev == "delta":
                deltas.append(d)
            elif ev == "done":
                done = d
        return deltas, done

    deltas, done = asyncio.run(_go())
    assert len(probe_calls) >= 2
    assert "workspace probe: found 42" in "\n".join(str(m.get("content", "")) for m in be.histories[-1])
    assert "sample_magic_fn returns 42." in "".join(deltas)


def test_battle_baseline_preflight_and_ping_pong_guard(tmp_path):
    (tmp_path / "m.py").write_text("def a(): return 1\ndef b(): return 0\n")
    (tmp_path / "test_m.py").write_text(
        "import m\ndef test_a(): assert m.a() == 1\ndef test_b(): assert m.b() == 2\n"
    )

    responses = [
        'ACTION: read_file\n```json\n{"path": "m.py"}\n```',
        'ACTION: list_dir\n```json\n{"path": "."}\n```',
        'ACTION: read_file\n```json\n{"path": "m.py"}\n```',
        'ACTION: list_dir\n```json\n{"path": "."}\n```',  # triggers 2-cycle ping-pong guard!
        'ACTION: edit_file\n```json\n{"path": "m.py", "old": "def b(): return 0", "new": "def b(): return 2"}\n```',
        'DONE: Fixed b.',
    ]

    class _FakeModel:
        def __init__(self):
            self.idx = 0
            self.openings = []
            self.seen_oscillating = False

        def complete(self, messages, **kw):
            if not self.openings:
                self.openings.append(messages[0]["content"])
            if "OSCILLATING" in "\n".join(str(m.get("content", "")) for m in messages):
                self.seen_oscillating = True
            t = responses[self.idx]
            self.idx += 1
            return ModelReply(text=t)

    events = []
    fm = _FakeModel()
    tr = run_battle(
        Task(id="t-ping", prompt="Fix `b` in `m.py`.", verify_cmd="pytest -q test_m.py"),
        str(tmp_path), fm, [], [], plan=False, autocommit=False, on_step=events.append,
    )
    assert "BASELINE CHECK (`pytest -q test_m.py`)" in fm.openings[0]
    assert any(e["kind"] == "baseline" and e["best_pass"] == 1 for e in events)
    assert fm.seen_oscillating
    assert "Fixed b." in tr.final_message


def test_store_record_skill_outcome_and_unverified_clarification_nudge(monkeypatch, tmp_path):
    from magic_core.magic import planner
    from magic_core.magic.store import Store
    from magic_core.magic.types import Skill

    st = Store(tmp_path)
    st.write_skills([Skill(
        id="sk1", name="trace-first", purpose="trace",
        trigger_when="always", procedure=["step"], status="active",
    )])
    assert st.record_skill_outcome(["sk1"], won=True, battle_id="bl_99") == 1
    sk = st.read_skills()[0]
    assert sk.record.uses == 1 and sk.record.wins == 1 and sk.record.last_used_battle == "bl_99"

    class _FakeBE(ModelBackend):
        def __init__(self):
            self.prompts = []
        async def generate(self, prompt, **kw):
            self.prompts.append(prompt)
            return GenerationResult(text="Which file or log should I inspect?", model="fake")
        async def info(self):
            return ModelInfo(name="fake", kind="local")

    be = _FakeBE()
    async def fake_gather(msg, history=""):
        return None, ""
    async def fake_triage(primary, question, evidence="", grounded=False):
        return "PLAN: ask. Note: You do NOT actually have this current/live info yet."

    monkeypatch.setattr(reply, "_PLAN", True)
    monkeypatch.setattr(reply, "_CHAT_AGENT", False)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(autoresearch, "gather", fake_gather)
    monkeypatch.setattr(look, "wants", lambda q, r=".": [])
    monkeypatch.setattr(look, "workspace_probe", lambda q, r=".": ([], ""))
    monkeypatch.setattr(planner, "plan_note", fake_triage)

    res = asyncio.run(reply.answer(be, "Explain what the live router temperature error shows", str(tmp_path)))
    assert "ask ONE brief clarifying question" in be.prompts[-1]
    assert "Which file" in res["answer"]


def test_toolhost_find_symbol_multi_edit_git_diff_and_env_scrub(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-123")
    monkeypatch.setenv("MAGIC_ADMIN_TOKEN", "tok-secret-456")

    (tmp_path / "calc.py").write_text(
        "class Box:\n    def add(self, a, b):\n        return a + b\n\n\ndef mul(a, b):\n    return 0\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    host = ShellToolHost(str(tmp_path))
    assert "OPENAI_API_KEY" not in host._env
    assert "MAGIC_ADMIN_TOKEN" not in host._env

    sym = host.run(ToolCall(name="find_symbol", args={"symbol": "mul"}))
    assert sym.ok and "calc.py [" in sym.output and "def mul(a, b):" in sym.output

    # Bad multi_edit rejects overlapping hunks or syntax error atomically (no partial write)
    bad = host.run(ToolCall(name="multi_edit", args={
        "path": "calc.py",
        "edits": [
            {"search": "return a + b", "replace": "return a + b + ("},  # syntax error
            {"search": "return 0", "replace": "return a * b"},
        ],
    }))
    assert not bad.ok
    assert "return 0" in (tmp_path / "calc.py").read_text()

    good = host.run(ToolCall(name="multi_edit", args={
        "path": "calc.py",
        "edits": [
            {"search": "return a + b", "replace": "return a + b + 1"},
            {"search": "return 0", "replace": "return a * b"},
        ],
    }))
    assert good.ok
    diff = host.run(ToolCall(name="git_diff", args={}))
    assert diff.ok and "return a * b" in diff.output


def test_battle_traceback_scout_and_duplicate_def_check(tmp_path):
    from magic_core.magic import battle, code_check

    (tmp_path / "m.py").write_text("def x():\n    bad = 1\n    raise ValueError('boom')\n")
    host = ShellToolHost(str(tmp_path), allowed=ShellToolHost.EXPLORE_TOOLS)
    t = Task(id="tb", prompt='Fix Traceback File "m.py", line 3 ValueError')
    scout = battle._initial_scout(t, str(tmp_path), host)
    assert "TRACEBACK SLICES:" in scout and "raise ValueError" in scout

    dup_err = code_check.check_code_blocks("```python\ndef foo():\n    return 1\n\ndef foo():\n    return 2\n```")
    assert any("duplicate top-level definition" in e and "foo" in e for e in dup_err)

    overload_ok = code_check.check_code_blocks(
        "```python\nfrom typing import overload\n@overload\ndef f(x: int) -> int: ...\n@overload\ndef f(x: str) -> str: ...\n```"
    )
    assert overload_ok == []


def test_doctor_report_fact_dedupe_ledger_resolve_and_prune(tmp_path):
    import os
    from magic_core import error_ledger, resource, snapshots
    from magic_core.magic.store import Store

    proj = tmp_path / "repo"
    (proj / ".git").mkdir(parents=True)
    (proj / ".git" / "index.lock").write_text("")
    ok, summary, checks = resource.doctor_report(str(proj))
    assert ok is False and "git_lock" in summary and any(c["name"] == "git_lock" and not c["ok"] for c in checks)

    st = Store(proj)
    added1, fid1 = st.add_fact_safe("Desktop has an RTX 2070 Super with 8GB VRAM")
    added2, _ = st.add_fact_safe("Desktop has an RTX 2070 Super with 8GB VRAM!")
    assert added1 and not added2

    error_ledger.record_watchdog(str(proj), "q", ["Claimed it ran docker ps when it could not have"])
    assert len(error_ledger.top_recurring(str(proj), min_count=1)) == 1
    assert error_ledger.resolve(str(proj), "watchdog::watchdog_fabricated_execution") is True
    assert error_ledger.top_recurring(str(proj), min_count=1) == []

    stale = tmp_path / ".magic-tmp" / "magic-fix-old"
    stale.mkdir(parents=True)
    os.utime(stale, (1000, 1000))
    assert snapshots.prune_workdirs(str(proj), max_age_hours=1.0) >= 1
    assert not stale.exists()


def test_reply_casual_fast_path_and_stream_fence_close(monkeypatch, tmp_path):
    from magic_core.magic import planner

    web_called = []
    plan_called = []

    class _StreamBreakBE(ModelBackend):
        def __init__(self):
            pass
        async def generate(self, prompt, **kw):
            return GenerationResult(text="Hey there!", model="magic")
        async def generate_stream(self, prompt, **kw):
            yield "Here is code:\n```python\nx = 1"
            raise RuntimeError("mid-stream disconnect")
        async def info(self):
            return ModelInfo(name="magic", kind="local")

    async def fake_gather(msg, history=""):
        web_called.append(msg)
        return None, ""
    async def fake_triage(*a, **kw):
        plan_called.append(1)
        return "PLAN"

    monkeypatch.setattr(reply, "_PLAN", True)
    monkeypatch.setattr(reply, "_REVISE", False)
    monkeypatch.setattr(reply, "_GROUNDING_CHECK", False)
    monkeypatch.setattr(autoresearch, "gather", fake_gather)
    monkeypatch.setattr(planner, "plan_note", fake_triage)

    be = _StreamBreakBE()
    res = asyncio.run(reply.answer(be, "Hey thanks!", str(tmp_path)))
    assert res["answer"] == "Hey there!"
    assert web_called == [] and plan_called == []

    async def _collect():
        deltas = []
        async for ev, d in reply.answer_stream(be, "Write a python script for hello world", str(tmp_path)):
            if ev == "delta":
                deltas.append(d)
        return "".join(deltas)

    streamed = asyncio.run(_collect())
    assert streamed.count("```") % 2 == 0 and streamed.endswith("```\n")
