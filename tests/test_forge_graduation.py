"""Graduation: a promoted forged tool becomes a first-class (bare-name) tool."""
from gremlin_core import tool_forge as TF
from gremlin_core import forge_lifecycle as FL
from gremlin_core import tools as tools_mod

CODE = 'def run(args):\n    return {"ok": True, "answer": "ok"}\n'


def _forge_live(tmp_path, name):
    TF.forge(name, "does a thing", CODE, root=str(tmp_path), capability="read_only",
             require_approval=False)


def _promote(tmp_path, name):
    FL.record_usage(str(tmp_path), [name], won=True, available_names=[name])
    FL.record_usage(str(tmp_path), [name], won=True, available_names=[name])
    FL.audit(str(tmp_path))   # -> active


def test_candidate_is_forged_prefixed(tmp_path):
    _forge_live(tmp_path, "mytool")
    reg = tools_mod.ToolRegistry()
    TF.load_into_registry(str(tmp_path), registry=reg)
    assert reg.get("forged_mytool") is not None
    assert reg.get("mytool") is None
    assert "[forged]" in reg.get("forged_mytool").description


def test_promoted_tool_graduates_to_bare_name(tmp_path):
    _forge_live(tmp_path, "mytool")
    _promote(tmp_path, "mytool")
    reg = tools_mod.ToolRegistry()
    TF.load_into_registry(str(tmp_path), registry=reg)
    # first-class: bare name, no forged_ prefix, no [forged] tag
    assert reg.get("mytool") is not None
    assert reg.get("forged_mytool") is None
    assert "[forged]" not in reg.get("mytool").description


def test_graduation_avoids_name_collision(tmp_path):
    _forge_live(tmp_path, "web_search")
    _promote(tmp_path, "web_search")
    reg = tools_mod.ToolRegistry()

    async def _h(args, ctx):
        return {"ok": True, "answer": "native"}
    reg.register(tools_mod.Tool(name="web_search", description="native",
                                parameters={"type": "object", "properties": {}},
                                handler=_h))
    TF.load_into_registry(str(tmp_path), registry=reg)
    # the native tool is not clobbered; the forged one keeps its prefix
    assert reg.get("web_search").description == "native"
    assert reg.get("forged_web_search") is not None
