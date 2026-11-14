"""Completion verdicts (MAGIC.md section 7): a verifier Score is classified
into verified | partial | failed | inconclusive, and a 0.0 that means
'the check never ran' must NOT be reported as a failure."""
from magic_core.magic.types import Score, BattleResult, Transcript
from magic_core.magic.verdict import (
    classify_outcome, CompletionVerdict,
    VERIFIED, PARTIAL, FAILED, INCONCLUSIVE,
)
from magic_core.magic import reckoning


def _v(value, signal="", detail=""):
    return classify_outcome(Score(value=value, failure_signal=signal, detail=detail))


# --- the four states -------------------------------------------------

def test_full_pass_is_verified():
    v = _v(1.0, detail="3 passed in 0.12s")
    assert v.status == VERIFIED and v.is_verified
    assert v.score == 1.0

def test_partial_score_is_partial():
    v = _v(0.5, signal="1 failing: test_clamp_high")
    assert v.status == PARTIAL
    assert v.is_actionable_failure          # partial is a real, learnable outcome

def test_real_failure_is_failed():
    v = _v(0.0, signal="2 failing: test_clamp_high, test_clamp_low")
    assert v.status == FAILED
    assert v.is_actionable_failure

def test_zero_with_no_signal_is_inconclusive():
    # 0.0 with nothing to say = we have no evidence it was actually tested
    v = _v(0.0)
    assert v.status == INCONCLUSIVE
    assert not v.is_actionable_failure      # must NOT teach a 'fix' lesson

def test_no_tests_collected_is_inconclusive_not_failed():
    # the classic trap: pytest collected nothing -> exit non-zero -> 0.0,
    # but the work was never tested.
    for sig in ("no tests ran", "collected 0 items",
                "error collecting tests", "command not found: gradlew",
                "verifier error: timed out after 120s"):
        v = _v(0.0, signal=sig)
        assert v.status == INCONCLUSIVE, f"{sig!r} should be inconclusive, got {v.status}"
        assert not v.is_actionable_failure

def test_full_score_but_check_didnt_run_is_inconclusive():
    # a 1.0 is not trustworthy if the signal says nothing actually ran
    v = _v(1.0, signal="no tests collected")
    assert v.status == INCONCLUSIVE and not v.is_verified


# --- BattleResult.verdict property -----------------------------------

def _result(value, signal=""):
    return BattleResult(
        battle_id="b1", task_id="t1",
        transcript=Transcript(task_id="t1", final_message="done"),
        score=Score(value=value, failure_signal=signal),
    )

def test_battleresult_verdict_property():
    assert _result(1.0).verdict.status == VERIFIED
    assert _result(0.0, "3 failing: a, b, c").verdict.status == FAILED
    assert _result(0.0, "no tests collected").verdict.status == INCONCLUSIVE
    # won stays a blunt threshold; verdict is the richer read
    assert _result(1.0).won is True
    assert _result(0.6).won is False
    assert isinstance(_result(1.0).verdict, CompletionVerdict)


# --- the reckoning digest surfaces the verdict -----------------------

def test_digest_labels_the_verdict_and_warns_on_inconclusive():
    inconclusive = _result(0.0, "no tests collected")
    d = reckoning._transcript_digest(inconclusive)
    assert "inconclusive" in d
    assert "never tested" in d or "do not" in d     # the don't-learn warning

    failed = _result(0.0, "2 failing: test_x, test_y")
    d2 = reckoning._transcript_digest(failed)
    assert "failed" in d2
    assert "NOTE: the check did not" not in d2       # no false warning on a real failure
