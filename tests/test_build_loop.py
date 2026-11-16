"""Tests for Magic's disciplined 7-stage build & self-edit loop (`magic_core.build_loop`)."""
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass

from magic_core import build_loop


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _init_mini_repo(path, with_tests: bool = True):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    pkg = path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    if with_tests:
        tdir = path / "tests"
        tdir.mkdir()
        (tdir / "test_calc.py").write_text(
            "from mypkg.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
        )
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "initial baseline")


def _make_diff(repo, rel_path: str, content: str) -> str:
    return build_loop.files_to_unified_diff({rel_path: content}, str(repo))


# ------------------------------------------------------------ 1. PLAN

def test_split_goal_deterministically_numbered_and_semicolon():
    steps_num = build_loop.split_goal_deterministically(
        "1. Add multiply in mypkg/calc.py 2. Add divide in mypkg/calc.py 3. Add tests/test_calc.py"
    )
    assert len(steps_num) == 3
    assert steps_num[0].index == 1 and "mypkg/calc.py" in steps_num[0].files
    assert steps_num[1].index == 2
    assert steps_num[2].index == 3 and "tests/test_calc.py" in steps_num[2].files

    steps_semi = build_loop.split_goal_deterministically("create foo.py; add tests/test_foo.py")
    assert len(steps_semi) == 2
    assert "foo.py" in steps_semi[0].files


def test_parse_plan_rejects_noop_verify_and_parses_json():
    raw = """```json
    {"steps": [
      {"title": "Add sub", "description": "Add sub(a, b)", "files": ["mypkg/calc.py"],
       "verify_cmd": "true", "expect_output": ""},
      {"title": "Add mul", "description": "Add mul(a, b)", "files": ["mypkg/calc.py"],
       "verify_cmd": "python3 -c \\"from mypkg.calc import mul; print(mul(3, 4))\\"",
       "expect_output": "12"}
    ]}
    ```"""
    steps = build_loop.parse_plan(raw, goal="add sub and mul")
    assert len(steps) == 2
    assert steps[0].verify_cmd == ""  # no-op `true` stripped
    assert "mul(3, 4)" in steps[1].verify_cmd
    assert steps[1].expect_output == "12"


# ------------------------------------------------------------ 4. VERIFY

def test_verify_step_sync_rejects_noop_and_missing_output(tmp_path):
    repo = tmp_path / "r"
    _init_mini_repo(repo)

    noop_step = build_loop.BuildStep(index=1, title="noop", verify_cmd="echo ok")
    res_noop = build_loop.verify_step_sync(noop_step, str(repo))
    assert not res_noop.ok
    assert "no-op" in res_noop.reason.lower()

    patch = _make_diff(repo, "mypkg/calc.py", "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    bad_expect = build_loop.BuildStep(
        index=1,
        title="check mul",
        verify_cmd="python3 -c \"from mypkg.calc import mul; print(mul(3, 4))\"",
        expect_output="999",
    )
    res_bad = build_loop.verify_step_sync(bad_expect, str(repo), patch=patch)
    assert not res_bad.ok
    assert "expected" in res_bad.reason.lower()

    good = build_loop.BuildStep(
        index=1,
        title="check mul",
        verify_cmd="python3 -c \"from mypkg.calc import mul; print(mul(3, 4))\"",
        expect_output="12",
    )
    res_good = build_loop.verify_step_sync(good, str(repo), patch=patch)
    assert res_good.ok, res_good.reason
    assert "12" in res_good.output


def test_synthesize_verify_cmd_handles_package_relative_imports(tmp_path):
    repo = tmp_path / "r"
    _init_mini_repo(repo)
    patch = _make_diff(
        repo,
        "mypkg/ops.py",
        "from .calc import add\n\ndef add_one(x):\n    return add(x, 1)\n",
    )
    step = build_loop.BuildStep(index=1, title="add ops", files=["mypkg/ops.py"])
    res = build_loop.verify_step_sync(step, str(repo), patch=patch)
    assert res.ok, res.reason
    assert "VERIFY_OK:" in res.output and "mypkg/ops.py:add_one" in res.output


# ------------------------------------------------------------ FULL LOOP + STOP-AND-FIX + COMMIT PER STEP

def test_build_loop_multi_step_commits_and_verifies(tmp_path):
    repo = tmp_path / "r"
    _init_mini_repo(repo)
    base_count = int(_git(repo, "rev-list", "--count", "HEAD").strip())

    steps = [
        build_loop.BuildStep(
            index=1,
            title="Add sub function",
            files=["mypkg/calc.py"],
            verify_cmd="python3 -c \"from mypkg.calc import sub; print('SUB:', sub(10, 3))\"",
            expect_output="SUB: 7",
        ),
        build_loop.BuildStep(
            index=2,
            title="Add sub unit test",
            files=["tests/test_calc_sub.py"],
            verify_cmd="python3 -m pytest -q tests/test_calc_sub.py",
            expect_output="1 passed",
        ),
    ]

    def proposer(step, _total, _outcomes):
        if step.index == 1:
            return _make_diff(
                repo, "mypkg/calc.py",
                "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
            )
        return _make_diff(
            repo, "tests/test_calc_sub.py",
            "from mypkg.calc import sub\n\ndef test_sub():\n    assert sub(10, 3) == 7\n",
        )

    res = asyncio.run(build_loop.run_build_loop(
        router=None,
        root=str(repo),
        goal="Add sub and test it",
        model_names=["fake"],
        mode="self_improve",
        steps=steps,
        step_proposer=proposer,
        run_tests=True,
        skip_review=True,
    ))

    assert res.ok, res.render_trace()
    assert len(res.commits) == 2
    assert res.verdict_line.startswith("VERDICT: APPROVE")
    new_count = int(_git(repo, "rev-list", "--count", "HEAD").strip())
    assert new_count == base_count + 2
    assert (repo / ".magic" / "last_build_plan.md").exists()
    trace = res.render_trace()
    assert "PLAN (2 steps)" in trace
    assert "SUB: 7" in trace
    assert "VERDICT: APPROVE" in trace


def test_stop_and_fix_recovers_on_retry(tmp_path):
    repo = tmp_path / "r"
    _init_mini_repo(repo)

    steps = [
        build_loop.BuildStep(
            index=1,
            title="Add mul function",
            files=["mypkg/calc.py"],
            verify_cmd="python3 -c \"from mypkg.calc import mul; print('MUL:', mul(4, 5))\"",
            expect_output="MUL: 20",
        ),
    ]

    # First proposal has a logic bug (returns a + b instead of a * b):
    # pytest passes, so TEST passes, but functional VERIFY catches it!
    def bad_proposer(_step, _total, _outcomes):
        return _make_diff(
            repo, "mypkg/calc.py",
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a + b\n",
        )

    fix_calls = []

    def fixer(step, attempt_num, _patch, last_error):
        fix_calls.append((step.index, attempt_num, last_error))
        return _make_diff(
            repo, "mypkg/calc.py",
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n",
        )

    res = asyncio.run(build_loop.run_build_loop(
        router=None,
        root=str(repo),
        goal="Add mul",
        model_names=["fake"],
        mode="self_improve",
        steps=steps,
        step_proposer=bad_proposer,
        step_fixer=fixer,
        run_tests=True,
        skip_review=True,
        max_step_retries=3,
    ))

    assert res.ok, res.render_trace()
    assert len(fix_calls) == 1
    assert "VERIFY failed" in fix_calls[0][2]
    assert len(res.outcomes[0].attempts) == 2
    assert res.outcomes[0].attempts[0].verify_ok is False
    assert res.outcomes[0].attempts[1].verify_ok is True
    assert "stop-and-fix engaged" in res.render_trace()


def test_stop_on_red_halts_and_preserves_earlier_green_commit(tmp_path):
    repo = tmp_path / "r"
    _init_mini_repo(repo)
    base_count = int(_git(repo, "rev-list", "--count", "HEAD").strip())

    steps = [
        build_loop.BuildStep(
            index=1,
            title="Good step",
            files=["mypkg/calc.py"],
            verify_cmd="python3 -c \"from mypkg.calc import sub; print(sub(5, 2))\"",
            expect_output="3",
        ),
        build_loop.BuildStep(
            index=2,
            title="Always broken step",
            files=["mypkg/calc.py"],
            verify_cmd="python3 -c \"print('ok')\"",
        ),
        build_loop.BuildStep(
            index=3,
            title="Never reached step",
            files=["mypkg/calc.py"],
        ),
    ]

    proposer_calls = []

    def proposer(step, _total, _outcomes):
        proposer_calls.append(step.index)
        if step.index == 1:
            return _make_diff(
                repo, "mypkg/calc.py",
                "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
            )
        return _make_diff(repo, "mypkg/calc.py", "def broken(:\n")

    res = asyncio.run(build_loop.run_build_loop(
        router=None,
        root=str(repo),
        goal="Three steps with middle failure",
        model_names=["fake"],
        mode="self_improve",
        steps=steps,
        step_proposer=proposer,
        step_fixer=lambda *a: _make_diff(repo, "mypkg/calc.py", "def broken(:\n"),
        run_tests=True,
        skip_review=True,
        max_step_retries=2,
    ))

    assert not res.ok
    assert res.stopped_on_red is True
    assert res.halted_at_step == 2
    assert len(res.commits) == 1  # step 1 stayed committed!
    assert proposer_calls == [1, 2]  # step 3 was NEVER proposed
    assert [o.status for o in res.outcomes] == ["committed", "failed", "skipped"]
    assert res.verdict_line.startswith("VERDICT: BLOCK")
    assert int(_git(repo, "rev-list", "--count", "HEAD").strip()) == base_count + 1


def test_pregate_protected_paths_enforced_per_step(tmp_path):
    repo = tmp_path / "r"
    _init_mini_repo(repo)
    (repo / "magic_core").mkdir()
    (repo / "magic_core" / "pregate.py").write_text("# gate\n")
    (repo / "magic_core" / "build_loop.py").write_text("# loop\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add protected files")

    steps = [
        build_loop.BuildStep(index=1, title="Try to weaken pregate", files=["magic_core/pregate.py"]),
    ]

    def evil_proposer(_step, _total, _outcomes):
        return _make_diff(repo, "magic_core/pregate.py", "# disabled\n")

    res = asyncio.run(build_loop.run_build_loop(
        router=None,
        root=str(repo),
        goal="Weaken pregate",
        model_names=["fake"],
        mode="self_improve",
        steps=steps,
        step_proposer=evil_proposer,
        step_fixer=lambda *a: _make_diff(repo, "magic_core/build_loop.py", "# hacked\n"),
        run_tests=False,
        skip_review=True,
        max_step_retries=2,
    ))

    assert not res.ok
    assert res.stopped_on_red is True
    assert res.commits == []
    assert "safety machinery" in res.outcomes[0].error.lower() or "off-limits" in res.outcomes[0].error.lower()


def test_self_review_llm_reviewer_can_block_and_approve(tmp_path):
    repo = tmp_path / "r"
    _init_mini_repo(repo)
    patch = _make_diff(
        repo, "mypkg/calc.py",
        "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
    )

    @dataclass
    class _FakeRes:
        ok: bool
        text: str
        error: str = ""

    class _BlockRouter:
        async def route(self, model, prompt, system=None, **kw):
            return _FakeRes(ok=True, text='{"verdict": "REQUEST_CHANGES", "feedback": "missing docstring"}')

    class _ApproveRouter:
        async def route(self, model, prompt, system=None, **kw):
            return _FakeRes(ok=True, text='{"verdict": "APPROVE", "feedback": ""}')

    steps = [build_loop.BuildStep(index=1, title="Add sub", files=["mypkg/calc.py"])]
    outcomes = [
        build_loop.StepOutcome(
            step=steps[0],
            status="committed",
            commit_sha="abc1234",
            test_ok=True,
            verify_result=build_loop.VerifyResult(ok=True, command="python3 -c ...", output="VERIFY_OK: ok"),
        )
    ]

    rep_block = asyncio.run(build_loop.self_review(
        "Add sub", str(repo), steps, outcomes, patch,
        mode="self_improve", router=_BlockRouter(), reviewer_a="gemini", reviewer_b="",
    ))
    assert not rep_block.approved
    assert rep_block.verdict_line.startswith("VERDICT: BLOCK")

    rep_ok = asyncio.run(build_loop.self_review(
        "Add sub", str(repo), steps, outcomes, patch,
        mode="self_improve", router=_ApproveRouter(), reviewer_a="gemini", reviewer_b="",
    ))
    assert rep_ok.approved
    assert rep_ok.verdict_line.startswith("VERDICT: APPROVE")


# ------------------------------------------------------------ WIRING: self_improve & build_project

def test_self_improve_routes_through_build_loop_when_enabled(tmp_path, monkeypatch):
    from magic_core import self_improve

    repo = tmp_path / "r"
    _init_mini_repo(repo)
    called = []

    async def _fake_loop(router, root, goal, model_names, **kw):
        called.append((root, goal, model_names, kw))
        return {"ok": True, "applied": True, "committed": True, "commits": ["abc"], "verdict": "VERDICT: APPROVE"}

    monkeypatch.setattr(build_loop, "run_self_improve_loop", _fake_loop)
    monkeypatch.setenv("MAGIC_BUILD_LOOP", "1")
    res = asyncio.run(self_improve.run_self_edit(
        router=None, root=str(repo), goal="Add a helper", model_names=["m"],
    ))
    assert res["applied"] is True
    assert len(called) == 1 and called[0][1] == "Add a helper"


def test_build_project_routes_through_build_loop_when_enabled(tmp_path, monkeypatch):
    from magic_core import build_project

    called = []

    async def _fake_proj_loop(router, magic_root, target_root, goal, model_names, **kw):
        called.append((magic_root, target_root, goal, model_names, kw))
        return {"ok": True, "applied": True, "committed": True, "commits": ["def"], "verdict": "VERDICT: APPROVE"}

    monkeypatch.setattr(build_loop, "run_project_build_loop", _fake_proj_loop)
    res = asyncio.run(build_project.run_build(
        router=None, magic_root=str(tmp_path), target_root=str(tmp_path / "proj"),
        goal="Build CLI tool", model_names=["m"], use_build_loop=True,
    ))
    assert res["applied"] is True
    assert len(called) == 1 and called[0][2] == "Build CLI tool"


# ------------------------------------------------------------ WIRING: /build, /fix, /do & main CLI

def test_commands_build_fix_do_dispatch_to_build_loop(tmp_path, monkeypatch):
    from magic_core.magic.commands import CommandContext, dispatch

    class _FakeReg:
        raw_config = {"persona": {"primary_model": "qwen3-8b"}}
        def get(self, name):
            return None
        def names(self):
            return []

    ctx = CommandContext(registry=_FakeReg(), project_root=str(tmp_path), config_path=str(tmp_path / "models.yaml"))
    seen = []

    async def _fake_proj_loop(*a, **kw):
        seen.append(("build", a[3]))
        return {"ok": True, "applied": True, "commits": ["111"], "trace": "BUILD_TRACE", "verdict": "VERDICT: APPROVE"}

    async def _fake_fix_loop(args, _ctx):
        seen.append(("fix", args))
        return {"ok": True, "action": "fix", "answer": "FIX_TRACE", "verdict": "VERDICT: APPROVE"}

    async def _fake_do_loop(args, _ctx):
        seen.append(("do", args))
        return {"ok": True, "action": "do", "answer": "DO_TRACE", "verdict": "VERDICT: APPROVE"}

    monkeypatch.setattr(build_loop, "run_project_build_loop", _fake_proj_loop)
    monkeypatch.setattr(build_loop, "run_fix_loop", _fake_fix_loop)
    monkeypatch.setattr(build_loop, "run_do_loop", _fake_do_loop)

    r_build = asyncio.run(dispatch("/build --loop demo: a hello script", ctx))
    r_fix = asyncio.run(dispatch("/fix --loop tighten error check", ctx))
    r_do = asyncio.run(dispatch("/do --loop check disk; check memory", ctx))

    assert r_build["ok"] and "BUILD_TRACE" in r_build["answer"]
    assert r_fix["ok"] and "FIX_TRACE" in r_fix["answer"]
    assert r_do["ok"] and "DO_TRACE" in r_do["answer"]
    assert [s[0] for s in seen] == ["build", "fix", "do"]


def test_run_do_loop_runs_steps_and_verifies_live_output(tmp_path, monkeypatch):
    from magic_core.magic import commands as cmd_mod
    from magic_core.magic.commands import CommandContext

    class _FakeReg:
        raw_config = {"persona": {"primary_model": "qwen3-8b"}}
        def get(self, name):
            return None

    ctx = CommandContext(registry=_FakeReg(), project_root=str(tmp_path), config_path=str(tmp_path / "models.yaml"))

    calls = []

    async def _fake_do_single(q, _ctx):
        calls.append(q)
        return {"ok": True, "action": "do", "answer": f"checked {q}\n\n$ df -h"}

    monkeypatch.setattr(cmd_mod, "_do_single", _fake_do_single)
    res = asyncio.run(cmd_mod.dispatch("/do --loop learn check disk; check ram", ctx))
    assert res["ok"] is True
    assert calls == ["learn check disk", "learn check ram"]
    assert "VERDICT: APPROVE" in res["answer"]
    assert "$ df -h" in res["answer"]


def test_main_cmd_improve_and_cmd_build_loop_flag(tmp_path, monkeypatch, capsys):
    import main as main_mod

    class _FakeReg:
        def primary_model_name(self):
            return "m"
        def names(self):
            return ["m"]
        def consult_models(self):
            return []

    called = []

    async def _fake_run_self_edit(*a, **kw):
        called.append(("improve", kw.get("use_build_loop")))
        return {"applied": True, "committed": True, "trace": "IMPROVE_LOOP_OK", "verdict": "VERDICT: APPROVE"}

    async def _fake_run_build(*a, **kw):
        called.append(("build", kw.get("use_build_loop")))
        return {"applied": True, "committed": True, "trace": "BUILD_LOOP_OK", "verdict": "VERDICT: APPROVE"}

    monkeypatch.setattr(main_mod.self_improve, "run_self_edit", _fake_run_self_edit)
    monkeypatch.setattr(main_mod.build_project, "run_build", _fake_run_build)

    asyncio.run(main_mod.cmd_improve(
        router=None, model_names=["m"], goal="add doc", do_apply=True,
        run_tests=True, reviewer_a="gemini", reviewer_b="gemini", use_loop=True,
    ))
    asyncio.run(main_mod.cmd_build(
        registry=_FakeReg(), router=None, name="demo", goal="build app", do_apply=True,
        run_tests=True, reviewer_a="gemini", reviewer_b="gemini",
        target_dir=str(tmp_path / "demo"), use_loop=True,
    ))

    out = capsys.readouterr().out
    assert "IMPROVE_LOOP_OK" in out
    assert "BUILD_LOOP_OK" in out
    assert called == [("improve", True), ("build", True)]
