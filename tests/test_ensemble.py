"""Best-of-N multi-head ensemble (Hydra P1.4)."""
import asyncio
from magic_core.magic import ensemble


class _R:
    def __init__(self, t, ok=True):
        self.text, self.ok = t, ok


class Head:
    def __init__(self, answer):
        self.answer = answer
    async def generate(self, prompt, *, system=None, max_tokens=1024, temperature=0.4,
                       history=None, grammar=None):
        return _R(self.answer)


class Judge:
    def __init__(self, pick):
        self.pick = pick
    async def generate(self, prompt, *, system=None, max_tokens=120, temperature=0.1,
                       history=None, grammar=None):
        return _R('{"best": %d, "why": "clearer"}' % self.pick)


def _run(c):
    return asyncio.run(c)


def test_ensemble_picks_the_judged_winner():
    heads = [("9b", Head("a shallow answer")), ("moe", Head("a deep correct answer"))]
    text, label, trace = _run(ensemble.ensemble_answer(heads, "hard q", Judge(2)))
    assert text == "a deep correct answer" and label == "moe"
    assert trace["candidates"] == 2 and trace["winner"] == "moe"


def test_single_candidate_skips_the_judge():
    heads = [("9b", Head("only answer")), ("moe", Head(""))]  # moe returns empty
    text, label, trace = _run(ensemble.ensemble_answer(heads, "q", Judge(1)))
    assert text == "only answer" and trace["candidates"] == 1


def test_no_candidates_fails_open_empty():
    heads = [("9b", Head("")), ("moe", Head(""))]
    text, label, trace = _run(ensemble.ensemble_answer(heads, "q", Judge(1)))
    assert text == "" and trace["candidates"] == 0


def test_judge_garbage_falls_open_to_first():
    class BadJudge:
        async def generate(self, prompt, **k):
            return _R("i have no idea")
    heads = [("9b", Head("first")), ("moe", Head("second"))]
    text, label, trace = _run(ensemble.ensemble_answer(heads, "q", BadJudge()))
    assert text == "first"  # unparseable judge -> fail open to candidate 1


def test_gated_off_by_default():
    # it must ship DARK -- only the RAM upgrade flips MAGIC_ENSEMBLE on
    assert ensemble.ENABLED is False
