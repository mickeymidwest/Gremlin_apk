"""Folding old tool outputs keeps the error CORE (pruning without losing
signal) -- the anti-"drown in logs" law."""
from magic_core.magic import battle


def test_result_gist_keeps_error_core_not_banner():
    dump = ("=================================== FAILURES ====================\n"
            "    def test_add():\n"
            ">       assert add(2, 2) == 4\n"
            "E       assert 5 == 4\n"
            "tests/test_math.py:12: AssertionError\n"
            "========================= 1 failed in 0.02s ===================\n")
    g = battle._result_gist(dump)
    assert "assert 5 == 4" in g          # the core survives the fold
    assert "FAILURES" not in g           # the banner does not
    assert "chars]" in g                 # size tag kept


def test_result_gist_plain_output_uses_first_line():
    g = battle._result_gist("magic_core/server.py\nmagic_core/reply.py\n")
    assert g.startswith("magic_core/server.py")


def test_condense_folds_middle_and_preserves_error_core():
    # a long transcript: task, then many steps, then a recent tail
    msgs = [{"role": "user", "content": "TASK: fix add()\nPLAN\n- do it"}]
    for i in range(20):
        msgs.append({"role": "assistant", "content": f'ACTION: run_shell\n```json\n{{"cmd": "pytest {i}"}}\n```'})
        body = "noise\n" * 500 + "E       assert 5 == 4\n1 failed in 0.1s\n"
        msgs.append({"role": "user", "content": f"RESULT (check):\n{body}"})
    folded, n = battle._condense_messages(msgs)
    assert n > 0                                   # it actually folded
    note = folded[1]["content"]
    assert "assert 5 == 4" in note                 # error core survived folding
    assert len("".join(m["content"] for m in folded)) < len("".join(m["content"] for m in msgs))
