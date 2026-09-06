"""The /skill command: human-driven skill authoring, Gemini fallback."""
import asyncio
import json

from gremlin_core.magic import reckoning
from gremlin_core.magic.commands import CommandContext, dispatch
from gremlin_core.magic.model import ScriptedModel
from gremlin_core.magic.store import Store
from gremlin_core.magic.types import Skill


class ModelReg:
    """registry stub whose .get returns a raw 'backend' that BackendModel
    wraps; here the backend just replays scripted JSON."""
    def __init__(self, replies):
        self.raw_config = {"persona": {"primary_model": "gremlin"}}
        self._replies = list(replies)
        self._i = 0

    def get(self, name):
        if name not in ("gremlin", "qwen2.5-7b"):
            return None
        reg = self

        class BE:
            async def generate(self, prompt, system=None, max_tokens=1024, temperature=0.3):
                r = reg._replies[min(reg._i, len(reg._replies) - 1)]
                reg._i += 1

                class R:
                    text = r
                    error = None
                    model = "gremlin"
                    ok = True
                return R()
        return BE()


def _ctx(tmp_path, replies):
    cfg = tmp_path / "m.yaml"
    cfg.write_text("models: []\npersona:\n  primary_model: gremlin\n")
    return CommandContext(registry=ModelReg(replies), project_root=str(tmp_path),
                          config_path=str(cfg))


def test_skill_new_drafts_gates_and_saves(tmp_path):
    draft = json.dumps({"name": "run tests before done", "purpose": "never DONE on red",
                        "trigger_when": "any bugfix", "trigger_matcher": "fix|bug",
                        "procedure": ["make the smallest edit", "run the tests", "DONE only if green"]})
    accept = json.dumps({"accept": True, "reason": "actionable"})
    ctx = _ctx(tmp_path, [draft, accept])

    r = asyncio.run(dispatch("/skill new when fixing a bug, always run the tests first", ctx))
    assert r["ok"] and "candidate" in r["answer"]
    cards = list((tmp_path / "data" / "skills").glob("*.yaml"))
    assert [c.stem for c in cards] == ["run-tests-before-done"]


def test_skill_new_rejected_by_gate(tmp_path):
    draft = json.dumps({"name": "vague thing", "purpose": "", "trigger_when": "",
                        "procedure": ["do stuff"]})
    reject = json.dumps({"accept": False, "reason": "vague"})
    ctx = _ctx(tmp_path, [draft, reject, reject])   # primary + gemini both reject
    r = asyncio.run(dispatch("/skill new do stuff", ctx))
    assert not r["ok"] and "rejected" in r["answer"]
    assert not list((tmp_path / "data" / "skills").glob("*.yaml"))


def test_skill_list_and_show(tmp_path):
    st = Store(tmp_path)
    st.write_skills([Skill(id="s1", name="flush-run", purpose="flush the trailing run",
                           trigger_when="loop encode", procedure=["emit last run"], status="active")])
    ctx = _ctx(tmp_path, [])
    r = asyncio.run(dispatch("/skill list", ctx))
    assert "flush-run" in r["answer"] and "[active/card]" in r["answer"]
    r2 = asyncio.run(dispatch("/skill show flush-run", ctx))
    assert "emit last run" in r2["answer"]


def test_skill_improve_revises(tmp_path):
    st = Store(tmp_path)
    st.write_skills([Skill(id="s1", name="flush-run", purpose="p", trigger_when="loop",
                           procedure=["old broken step"], status="active", provenance=["b0"])])
    draft = json.dumps({"name": "flush-run", "purpose": "p", "trigger_when": "loop",
                        "procedure": ["run the loop", "after it, emit the final run"]})
    accept = json.dumps({"accept": True, "reason": "better"})
    ctx = _ctx(tmp_path, [draft, accept])
    r = asyncio.run(dispatch("/skill improve flush-run | it forgets to emit the last run", ctx))
    assert r["ok"] and "emit the final run" in r["answer"]
    back = Store(tmp_path).read_skills()
    assert [s.status for s in back if s.name == "flush-run"].count("candidate") == 1
    assert any(s.status == "deprecated" for s in back)
