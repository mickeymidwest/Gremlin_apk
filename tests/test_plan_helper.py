"""Two-head planning: the GPU model + CPU helper both draft, best is kept."""
from magic_core.magic.battle import _plan
from magic_core.magic.model import ScriptedModel
from magic_core.magic.types import Task


def test_helper_draft_is_considered_and_best_wins():
    # the primary gives a vague draft; the helper gives a concrete, look-first
    # numbered plan that scores higher -> the helper's draft is chosen
    model = ScriptedModel(["just fix it"])
    helper = ScriptedModel(["1. read greet.py\n2. edit the return value\n3. run the tests"])
    p = _plan(Task(id="t", prompt="fix greet in greet.py"), model, [], helper=helper)
    assert "read greet.py" in p


def test_no_helper_keeps_serial_behavior():
    model = ScriptedModel(["1. read the file\n2. edit it\n3. test", "vague"])
    p = _plan(Task(id="t", prompt="fix it"), model, [], samples=2)
    assert "read the file" in p


def test_helper_failure_does_not_break_planning():
    class _Boom:
        def complete(self, *a, **k):
            raise RuntimeError("cpu down")
    model = ScriptedModel(["1. read it\n2. fix it\n3. test it"])
    # the helper blows up but planning still returns the model's draft
    p = _plan(Task(id="t", prompt="fix it"), model, [], helper=_Boom())
    assert "read it" in p
