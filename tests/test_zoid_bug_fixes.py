"""Regression tests for nightly Zoid battle / verifier / toolhost bugs."""
from pathlib import Path

from magic_core.magic.battle import _assemble_system, run_battle
from magic_core.magic.model import ModelReply
from magic_core.magic.toolhost import ShellToolHost, ToolCall
from magic_core.magic.types import Task
from magic_core.magic.verifier import PytestVerifier
import zoid_loop


def test_pytest_verifier_scores_under_parent_addopts_quiet(tmp_path):
    """Parent pytest.ini `addopts = -q` must not double into `-qq` and swallow counts."""
    parent = tmp_path / "repo"
    parent.mkdir()
    (parent / "pytest.ini").write_text("[pytest]\naddopts = -q\n")
    work = parent / "work" / "b1"
    work.mkdir(parents=True)
    (work / "test_sample.py").write_text(
        "def test_pass():\n    assert True\n\n"
        "def test_fail():\n    assert False\n"
    )
    sc = PytestVerifier().score(Task(id="s", prompt="x"), str(work))
    assert sc.value == 0.5
    assert "test_fail" in sc.failure_signal


def test_cfuzz_targets_set_clang_verify_cmd(monkeypatch, tmp_path):
    """C fuzz targets must tell the system rules to compile with clang, not pytest."""
    monkeypatch.setattr(zoid_loop, "HOME", tmp_path)
    dl = tmp_path / "Downloads" / "fuzz-practice"
    dl.mkdir(parents=True)
    (dl / "README.md").write_text("# fuzz")
    cfuzz = [t for t in zoid_loop.targets() if t["name"] == "nsfuzz"]
    assert cfuzz
    cmd = cfuzz[0]["task"].verify_cmd
    assert "clang" in cmd and "pytest" not in cmd
    th = ShellToolHost(dl)
    sys_msg, _ = _assemble_system(cfuzz[0]["task"], [], [], th)
    assert "clang" in sys_msg and "The check for this task:  python -m pytest" not in sys_msg


def test_write_file_rejects_empty_text_and_edit_guards(tmp_path):
    p = tmp_path / "convert.py"
    p.write_text(
        "def km_to_miles(km: float) -> float:\n"
        "    return km * 0.621371\n\n"
        "def miles_to_km(miles: float) -> float:\n"
        "    return miles * 0.621371\n"
    )
    th = ShellToolHost(tmp_path)

    # Empty/missing text on write_file must error instead of zeroing the file
    r_empty = th.run(ToolCall("write_file", {"path": "harness_fuzz.c"}))
    assert not r_empty.ok
    assert not (tmp_path / "harness_fuzz.c").exists()

    # After km_to_miles is edited, searching "return km * 0.621371" must NOT fuzzy-hit miles_to_km
    r_ok = th.run(ToolCall("edit_file", {
        "path": "convert.py", "search": "return km * 0.621371", "replace": "return km * 1.609344"}))
    assert r_ok.ok
    r_cross = th.run(ToolCall("edit_file", {
        "path": "convert.py", "search": "return km * 0.621371", "replace": "return km * 1.609344"}))
    assert not r_cross.ok and "return miles * 0.621371" in p.read_text()

    # Prepending a whole function via search="" onto non-empty file is refused
    h = tmp_path / "harness_fuzz.c"
    h.write_text("#include \"src/netstring.h\"\nint LLVMFuzzerTestOneInput(const uint8_t *d, size_t s) { return 0; }\n")
    r_prep = th.run(ToolCall("edit_file", {
        "path": "harness_fuzz.c",
        "search": "",
        "replace": "#include <stdint.h>\nint LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {\n    return 0;\n}",
    }))
    assert not r_prep.ok and "write_file" in r_prep.output


def test_battle_refuses_identical_failed_action_beyond_recent_window(tmp_path):
    """Repeated failing identical edits interleaved with reads must still trip loop guard."""
    (tmp_path / "m.py").write_text("x = 1\n")

    class _LoopingModel:
        name = "loop"
        def __init__(self):
            self.i = 0
        def complete(self, messages, system=None, max_tokens=4096, grammar=None):
            # Interleave unique view_file offsets so _recent[-8:] would miss repeated bad edits
            seq = [
                'ACTION: read_file\n```json\n{"path": "m.py"}\n```',
                'ACTION: edit_file\n```json\n{"path": "m.py", "search": "NOPE", "replace": "y"}\n```',
                'ACTION: view_file\n```json\n{"path": "m.py", "start": 1, "count": 1}\n```',
                'ACTION: list_dir\n```json\n{"path": "."}\n```',
                'ACTION: grep\n```json\n{"pattern": "x", "path": "m.py"}\n```',
                'ACTION: repo_map\n```json\n{"query": "x"}\n```',
                'ACTION: edit_file\n```json\n{"path": "m.py", "search": "NOPE", "replace": "y"}\n```',
                'ACTION: view_file\n```json\n{"path": "m.py", "start": 1, "count": 2}\n```',
                'ACTION: edit_file\n```json\n{"path": "m.py", "search": "NOPE", "replace": "y"}\n```',
            ]
            txt = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return ModelReply(text=txt)

    tr = run_battle(Task(id="lf", prompt="fix m.py", verify_cmd="true"),
                    str(tmp_path), _LoopingModel(), skills=[], facts=[],
                    step_budget=14, plan=False, autocommit=False)
    notes = [s.content for s in tr.steps if s.kind == "note"]
    assert any("refused a repeated edit_file" in n for n in notes)
