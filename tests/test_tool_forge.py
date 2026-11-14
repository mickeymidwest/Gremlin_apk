"""The Tool Forge: author -> validate -> sandbox-run -> register a new tool."""
import asyncio

from magic_core import tool_forge as TF
from magic_core import tools as tools_mod

GOOD = '''
def run(args):
    a = int(args.get("a", 0))
    b = int(args.get("b", 0))
    return {"ok": True, "answer": f"sum = {a + b}"}
'''


def test_forge_validates_and_persists(tmp_path):
    r = TF.forge("adder", "add two ints", GOOD, root=str(tmp_path),
                 parameters={"type": "object", "properties": {"a": {}, "b": {}}})
    assert r["ok"] and r["name"] == "adder"
    assert (tmp_path / "data" / "forged_tools" / "adder.py").exists()
    assert "adder" in [t["name"] for t in TF.list_forged(str(tmp_path))]


def test_security_scan_blocks_destructive():
    assert "recursive delete" in TF.security_scan("def run(a):\n import shutil; shutil.rmtree('/')\n")
    assert "os.system" in TF.security_scan("def run(a):\n import os; os.system('x')\n")
    assert "secret-path access" in TF.security_scan("def run(a):\n open('~/.ssh/id_rsa')\n")
    assert TF.security_scan(GOOD) == []


def test_forge_refuses_unsafe_code(tmp_path):
    bad = "def run(args):\n import os\n os.system('rm -rf /')\n return {}\n"
    r = TF.forge("nuke", "bad", bad, root=str(tmp_path))
    assert not r["ok"] and "blocked" in r


def test_forge_refuses_without_run_def(tmp_path):
    r = TF.forge("norun", "x", "x = 1\n", root=str(tmp_path))
    assert not r["ok"] and "run(" in r["error"]


def test_forge_refuses_bad_name(tmp_path):
    assert not TF.forge("Bad Name", "x", GOOD, root=str(tmp_path))["ok"]


def test_run_forged_executes_sandboxed(tmp_path):
    TF.forge("adder", "add", GOOD, root=str(tmp_path))
    out = asyncio.run(TF.run_forged("adder", {"a": 2, "b": 3}, str(tmp_path)))
    assert out["ok"] and "sum = 5" in out["answer"]


def test_run_missing_tool(tmp_path):
    out = asyncio.run(TF.run_forged("ghost", {}, str(tmp_path)))
    assert out["ok"] is False


def test_load_into_registry_and_invoke(tmp_path):
    TF.forge("adder", "add", GOOD, root=str(tmp_path), capability="read_only")
    reg = tools_mod.ToolRegistry()
    n = TF.load_into_registry(str(tmp_path), registry=reg)
    assert n == 1
    t = reg.get("forged_adder")
    assert t is not None and t.capability == "read_only"
    r = asyncio.run(t.handler({"a": 10, "b": 1}, None))
    assert r["ok"] and "sum = 11" in r["answer"]


def test_remove(tmp_path):
    TF.forge("adder", "add", GOOD, root=str(tmp_path))
    assert TF.remove("adder", str(tmp_path))
    assert TF.list_forged(str(tmp_path)) == []
