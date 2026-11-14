"""The self-edit review gate must FAIL CLOSED: a prose disapproval that
happens to contain the word 'approve' must not be counted as an approval."""
import asyncio

from magic_core import review


class _Res:
    def __init__(self, text, ok=True, error=""):
        self.text, self.ok, self.error = text, ok, error


class _Router:
    def __init__(self, text, ok=True):
        self._t, self._ok = text, ok

    async def route(self, model, prompt, system=None, **kw):
        return _Res(self._t, self._ok)


def _round(text, ok=True):
    return asyncio.run(review._review_once(_Router(text, ok), "rev", "patch", "goal"))


def test_prose_disapproval_is_not_approved():
    for t in [
        "I can't approve this, it weakens a safety check.",
        "I would not approve this patch as written.",
        "This should not be applied; it removes error handling.",
        "Reject: the diff deletes the sandbox call.",
        "Cannot approve -- it disables the git revert safety.",
        "Changes requested: the patch is unrelated to the goal.",
        "I can’t approve this; it removes the sandbox.",   # curly apostrophe
        "I cant approve this, it drops error handling.",         # dropped apostrophe
    ]:
        assert _round(t).approved is False, t


def test_clean_json_verdicts():
    assert _round('{"verdict": "APPROVE", "feedback": ""}').approved is True
    assert _round('{"verdict": "REQUEST_CHANGES", "feedback": "x"}').approved is False


def test_genuine_prose_approval_still_passes():
    assert _round("APPROVE -- looks correct and achieves the goal.").approved is True


def test_unreachable_reviewer_fails_closed():
    r = _round("anything", ok=False)
    assert r.approved is False
