"""Self-edit reviewer: a CONDITIONAL approval is not an approval (2026-11-15).
Live hole Gemini found: the non-JSON prose fallback approved on 'I would
approve this once you fix X'. The gate's own rule is 'lean hard to reject on
ambiguity', so a hedged/conditional approve must fail closed."""
import re
import io


def _approves(text):
    flat = re.sub(r"['‘’]", "", text.lower())
    src = io.open("magic_core/review.py", encoding="utf-8").read()
    # mirror the term list from review.py (kept in sync by this test)
    terms = re.findall(r'"([^"]+)"', src.split("rejected = any(w in flat for w in (")[1].split("))")[0])
    rejected = any(w in flat for w in terms)
    return ("approve" in flat and not rejected)


def test_clean_approvals_pass():
    assert _approves("This looks correct. I approve.")
    assert _approves("Approved, ship it.")


def test_conditional_approvals_fail_closed():
    for t in [
        "I would approve this once you fix the error handling.",
        "I approve, but you should add a test.",
        "Approve if you also handle the empty case.",
        "Looks good, approve, as long as the test passes.",
        "This needs changes before Id approve.",
    ]:
        assert not _approves(t), t


def test_outright_rejections_fail():
    assert not _approves("I cannot approve this -- it weakens a safety check.")
    assert not _approves("REQUEST_CHANGES: add bounds checking.")
