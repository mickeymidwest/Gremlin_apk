"""The deterministic pre-gate must sit IN FRONT of the two LLM reviewers:
an unappliable / unsafe patch is auto-rejected and fixed before any reviewer
is consulted, so a 14B never gets to rubber-stamp a patch that isn't green."""
import asyncio
import subprocess

from gremlin_core import review


class _Res:
    def __init__(self, text, ok=True, error=""):
        self.text, self.ok, self.error = text, ok, error


class _RecordingRouter:
    def __init__(self):
        self.calls = []

    async def route(self, model, prompt, system=None, **kw):
        self.calls.append(model)
        # If a reviewer were (wrongly) reached, it would APPROVE -- making the
        # test fail loudly if the pre-gate isn't actually gating.
        return _Res('{"verdict": "APPROVE", "feedback": ""}')


def _mkrepo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=path, check=True, capture_output=True)


def test_pregate_rejects_before_reviewers(tmp_path):
    repo = tmp_path / "r"; repo.mkdir(); _mkrepo(repo)
    bad = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-nonexistent line\n+y = 2\n"
    rr = _RecordingRouter()
    outcome = asyncio.run(review.review_and_revise(
        rr, bad, "goal", reviewer_a="A", reviewer_b="B", fixer="FIX",
        root=str(repo), gemini_advisory=False, run_tests=False, max_rounds=2))
    assert outcome.approved is False
    # The two reviewers must NEVER have been consulted -- only the fixer, once
    # per round, as the pre-gate kept auto-rejecting an unappliable patch.
    assert "A" not in rr.calls and "B" not in rr.calls, rr.calls
    assert rr.calls == ["FIX", "FIX"], rr.calls
    assert any(r.reviewer == "pre-gate" for r in outcome.history)


def test_no_pregate_when_root_missing(tmp_path):
    # Back-compat: with no root (build_project bootstrap path), the pre-gate is
    # skipped and the reviewers run as before.
    rr = _RecordingRouter()
    outcome = asyncio.run(review.review_and_revise(
        rr, "any patch text", "goal", reviewer_a="A", reviewer_b="B", fixer="FIX",
        gemini_advisory=False, max_rounds=1))
    assert outcome.approved is True
    assert rr.calls == ["A", "B"]  # both reviewers ran, no pre-gate in the way
