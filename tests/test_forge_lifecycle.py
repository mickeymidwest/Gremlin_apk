"""Forged-tool promotion / retirement -- earn-your-keep like skills."""
from magic_core import tool_forge as TF
from magic_core import forge_lifecycle as FL

CODE = 'def run(args):\n    return {"ok": True, "answer": "ok"}\n'


def _forge(tmp, name):
    TF.forge(name, "a tool", CODE, root=str(tmp), capability="read_only")


def test_useful_tool_is_promoted(tmp_path):
    _forge(tmp_path, "helper")
    # used in two passing battles -> promoted to active
    FL.record_usage(str(tmp_path), ["helper"], won=True, available_names=["helper"])
    FL.record_usage(str(tmp_path), ["helper"], won=True, available_names=["helper"])
    changes = FL.audit(str(tmp_path))
    assert any("helper: candidate -> active" in c for c in changes)


def test_never_used_tool_is_retired(tmp_path):
    _forge(tmp_path, "deadweight")
    # available for many battles, never used -> retired, files moved out
    for _ in range(FL.STALE_BATTLES):
        FL.record_usage(str(tmp_path), [], won=True, available_names=["deadweight"])
    changes = FL.audit(str(tmp_path))
    assert any("deadweight: retired (never used)" in c for c in changes)
    assert not (tmp_path / "data" / "forged_tools" / "deadweight.py").exists()
    assert (tmp_path / "data" / "forged_tools" / "_deprecated" / "deadweight.py").exists()


def test_losing_tool_is_retired(tmp_path):
    _forge(tmp_path, "loser")
    for _ in range(FL.DEPRECATE_LOSSES):
        FL.record_usage(str(tmp_path), ["loser"], won=False, available_names=["loser"])
    changes = FL.audit(str(tmp_path))
    assert any("loser: retired" in c for c in changes)


def test_retired_tool_is_not_loaded(tmp_path):
    from magic_core import tools as tools_mod
    _forge(tmp_path, "deadweight")
    for _ in range(FL.STALE_BATTLES):
        FL.record_usage(str(tmp_path), [], won=True, available_names=["deadweight"])
    FL.audit(str(tmp_path))
    reg = tools_mod.ToolRegistry()
    TF.load_into_registry(str(tmp_path), registry=reg)
    # the retired tool (now under _deprecated/) is not registered
    assert reg.get("forged_deadweight") is None


def test_record_survives_and_summary(tmp_path):
    _forge(tmp_path, "t1")
    FL.record_usage(str(tmp_path), ["t1"], won=True, available_names=["t1"])
    s = FL.summary(str(tmp_path))
    assert "forged_t1" in s and "1u" in s and "1W/0L" in s
