"""Self-building: the model writes a tool, the forge validates + persists it."""
import asyncio

from magic_core import tool_forge as TF
from magic_core.magic.model import ScriptedModel


def test_author_writes_and_forges_a_tool(tmp_path):
    model = ScriptedModel(['Here you go:\n```python\n'
                           'def run(args):\n'
                           '    n = int(args.get("n", 0))\n'
                           '    return {"ok": True, "answer": f"double = {n*2}"}\n'
                           '```'])
    r = TF.author("double a number n", model, str(tmp_path), name="doubler")
    assert r["ok"] and r["name"] == "doubler"
    assert "def run" in r["code"]
    # it is now a real, runnable forged tool
    out = asyncio.run(TF.run_forged("doubler", {"n": 21}, str(tmp_path)))
    assert out["ok"] and "double = 42" in out["answer"]


def test_author_rejects_unsafe_model_output(tmp_path):
    model = ScriptedModel(['```python\n'
                           'def run(args):\n'
                           '    import os\n'
                           '    os.system("rm -rf /")\n'
                           '    return {"ok": True}\n'
                           '```'])
    r = TF.author("delete stuff", model, str(tmp_path), name="bad")
    assert not r["ok"] and "blocked" in r


def test_author_handles_no_code(tmp_path):
    model = ScriptedModel(["I can't do that."])
    r = TF.author("do a thing", model, str(tmp_path))
    assert not r["ok"]


def test_name_slug():
    assert TF._name_from("count the files in a directory") == "count_the_files"
