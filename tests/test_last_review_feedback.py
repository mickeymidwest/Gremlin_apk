"""_last_review_feedback -- self_edit's failure message used to say
THAT review failed but never WHY; the real reviewer feedback was
sitting unused in result["review_history"] the whole time."""
from gremlin_core.tools import _last_review_feedback


def test_surfaces_the_last_rejecting_rounds_feedback():
    result = {"review_history": [
        {"reviewer": "gemini", "approved": False, "feedback": "missing null check"},
        {"reviewer": "qwen2.5-14b", "approved": False, "feedback": "this changes the API contract"},
    ]}
    out = _last_review_feedback(result)
    assert "qwen2.5-14b" in out
    assert "API contract" in out


def test_empty_when_no_history():
    assert _last_review_feedback({}) == ""
    assert _last_review_feedback({"review_history": []}) == ""


def test_empty_when_history_has_no_feedback_text():
    result = {"review_history": [{"reviewer": "gemini", "approved": False, "feedback": ""}]}
    assert _last_review_feedback(result) == ""


def test_finds_the_most_recent_rejection_when_earlier_rounds_also_rejected():
    result = {"review_history": [
        {"reviewer": "gemini", "approved": False, "feedback": "first pass: needs work"},
        {"reviewer": "gemini", "approved": False, "feedback": "second pass: still off"},
    ]}
    out = _last_review_feedback(result)
    assert "second pass: still off" in out
    assert "first pass" not in out
