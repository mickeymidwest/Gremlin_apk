"""HARNESS-SURVEY #7: battle-transcript condensation (deterministic fold)."""
from gremlin_core.magic import battle as B


def _big(role, content):
    return {"role": role, "content": content}


def _transcript(n_steps, body_chars=6000):
    """opening + n_steps of (assistant action, bulky tool result)."""
    msgs = [_big("user", "TASK: fix the bug\n\nYour plan:\n1. look\n2. edit\n\nBegin.")]
    for i in range(n_steps):
        msgs.append(_big("assistant",
            f'ACTION: edit_file\n```json\n{{"path": "mod_{i}.py", "search": "a", "replace": "b"}}\n```'))
        msgs.append(_big("user", f"RESULT (ok):\n" + ("x" * body_chars)))
    return msgs


def test_short_transcript_is_untouched():
    msgs = _transcript(2)
    out, folded = B._condense_messages(msgs, budget=10 ** 9)
    assert folded == 0 and out is msgs


def test_long_transcript_folds_middle_and_shrinks():
    msgs = _transcript(12, body_chars=6000)
    before = B._msgs_chars(msgs)
    out, folded = B._condense_messages(msgs)
    after = B._msgs_chars(out)
    assert folded > 0
    assert after < before
    assert B._msgs_chars(out) <= B._CONDENSE_CTX_CHARS + 20000  # well under the raw size
    # opening (task + plan) survives verbatim at the front
    assert out[0] is msgs[0] and "TASK: fix the bug" in out[0]["content"]
    # the fold note names what was already done so the model won't restart
    assert "ALREADY did" in out[1]["content"]
    assert "edit_file mod_0.py" in out[1]["content"]
    # the last turn is still the latest user result (BackendModel prompt)
    assert out[-1] is msgs[-1] and out[-1]["role"] == "user"
    # the most recent turns are kept verbatim, not digested
    assert out[-B._CONDENSE_KEEP_RECENT:] == msgs[-B._CONDENSE_KEEP_RECENT:]


def test_fold_count_matches_middle_size():
    msgs = _transcript(12, body_chars=6000)
    out, folded = B._condense_messages(msgs)
    # everything between head and the kept tail was folded
    assert folded == len(msgs) - 1 - B._CONDENSE_KEEP_RECENT


def test_gists_are_one_line_each():
    a = B._action_gist('think think\nACTION: grep\n```json\n{"pattern": "def foo", "path": "."}\n```')
    assert a == "grep def foo"
    assert B._action_gist("DONE\nall fixed") == "DONE"
    r = B._result_gist("RESULT (error):\nTraceback...\n  File x\n" + "y" * 5000)
    assert "\n" not in r and "chars]" in r


def test_recondense_preserves_prior_fold_lines():
    """A second fold must keep the first fold's trajectory lines, not
    truncate the fold note down to its header (Gemini BLOCK #1)."""
    msgs = _transcript(12, body_chars=6000)
    once, f1 = B._condense_messages(msgs)
    assert f1 > 0 and once[1]["content"].startswith(B._FOLD_MARK)
    # grow it again past budget, then fold a second time
    for i in range(12, 20):
        once.append(_big("assistant",
            f'ACTION: edit_file\n```json\n{{"path": "mod_{i}.py", "search": "a", "replace": "b"}}\n```'))
        once.append(_big("user", "RESULT (ok):\n" + ("x" * 6000)))
    twice, f2 = B._condense_messages(once)
    assert f2 > 0 and twice[1]["content"].startswith(B._FOLD_MARK)
    # earliest step from the FIRST fold still present in the note
    assert "edit_file mod_0.py" in twice[1]["content"]
    # and a step that was folded on the second pass too
    assert "edit_file mod_13.py" in twice[1]["content"]


def test_action_gist_ignores_brace_in_think():
    g = B._action_gist('<think>maybe {"x": 1} ?</think>\nACTION: read_file\n```json\n{"path": "z.py"}\n```')
    assert g == "read_file z.py"
