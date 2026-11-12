"""Approval mode: forged tools stage to _pending until approved."""
from gremlin_core import tool_forge as TF
from gremlin_core import tools as tools_mod

CODE = 'def run(args):\n    return {"ok": True, "answer": "ok"}\n'


def _forge_gated(tmp_path, name="helper"):
    """Forge the way a surface does: pass the configured approval policy."""
    req = TF.approval_required(str(tmp_path))
    return TF.forge(name, "x", CODE, root=str(tmp_path), require_approval=req)


def test_approval_default_is_on(tmp_path):
    assert TF.approval_required(str(tmp_path)) is True   # safe default


def test_forge_stages_to_pending_by_default(tmp_path):
    r = _forge_gated(tmp_path)
    assert r["ok"] and r["pending"] is True
    assert not (tmp_path / "data" / "forged_tools" / "helper.py").exists()
    assert (tmp_path / "data" / "forged_tools" / "_pending" / "helper.py").exists()
    reg = tools_mod.ToolRegistry()
    TF.load_into_registry(str(tmp_path), registry=reg)
    assert reg.get("forged_helper") is None          # pending tools are NOT loaded


def test_approve_makes_it_live(tmp_path):
    _forge_gated(tmp_path)
    assert TF.approve(str(tmp_path), "helper")
    assert (tmp_path / "data" / "forged_tools" / "helper.py").exists()
    reg = tools_mod.ToolRegistry()
    TF.load_into_registry(str(tmp_path), registry=reg)
    assert reg.get("forged_helper") is not None


def test_approval_off_forges_live(tmp_path):
    TF.set_approval(str(tmp_path), False)
    r = _forge_gated(tmp_path)
    assert r["pending"] is False
    assert (tmp_path / "data" / "forged_tools" / "helper.py").exists()


def test_reject_discards_pending(tmp_path):
    _forge_gated(tmp_path)
    assert TF.reject(str(tmp_path), "helper")
    assert TF.list_pending(str(tmp_path)) == []


def test_pending_listed_and_code_readable(tmp_path):
    TF.forge("helper", "desc", CODE, root=str(tmp_path), require_approval=True)
    assert [p["name"] for p in TF.list_pending(str(tmp_path))] == ["helper"]
    assert "def run" in TF.pending_code(str(tmp_path), "helper")
