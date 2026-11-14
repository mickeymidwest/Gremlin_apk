"""HARNESS-SURVEY #10: best-of-2 plan selection by a pure structural score."""
from magic_core.magic import battle as B
from magic_core.magic.model import ScriptedModel
from magic_core.magic.types import Task


_TASK = Task(id="t", prompt="fix the retry() bug in net/client.py so it backs off")

_GOOD = ("1. repo_map then read net/client.py to find retry()\n"
         "2. identify the backoff bug in retry()\n"
         "3. edit_file to fix the backoff\n"
         "4. run pytest to confirm")
_VAGUE = "do the thing and make it work"
_INLINE = ("1. here is the fix\n```python\ndef retry():\n    pass\n```")


def test_score_prefers_concrete_stepped_plan():
    assert B._score_plan(_GOOD, _TASK) > B._score_plan(_VAGUE, _TASK)
    assert B._score_plan(_GOOD, _TASK) > B._score_plan(_INLINE, _TASK)
    assert B._score_plan("", _TASK) < 0


def test_score_rewards_look_first_and_named_file():
    look = "1. read net/client.py\n2. edit retry()\n3. run pytest"
    no_look = "1. edit retry() in a file\n2. edit more\n3. run pytest"
    assert B._score_plan(look, _TASK) > B._score_plan(no_look, _TASK)


def test_plan_picks_higher_scoring_of_two_samples():
    # ScriptedModel yields VAGUE first, GOOD second, then repeats GOOD.
    m = ScriptedModel([_VAGUE, _GOOD])
    out = B._plan(_TASK, m, skills=[], samples=2)
    assert out == _GOOD


def test_plan_single_sample_still_works():
    m = ScriptedModel([_GOOD])
    out = B._plan(_TASK, m, skills=[], samples=1)
    assert out == _GOOD


def test_plan_handles_all_empty():
    m = ScriptedModel([""])
    assert B._plan(_TASK, m, skills=[], samples=2) == ""
