"""Regression guard for a real bug hit live 2026-09-13: "write a python
function that returns the nth fibonacci number" -- an ordinary "show me
some code" request -- was getting classified as script_fix/run_command
(the model tried to search for a file, then tried to offer running a
shell command) instead of chat. Root cause: the classify prompt's chat
bullet didn't distinguish "write code IN THE REPLY" from "write code TO
A FILE" -- both just say "write". A full live-model regression test
isn't practical here (non-deterministic, needs a loaded GGUF); this
guards the actual fix -- that the clarifying guidance stays in the
prompt every classifier call is built from -- so it can't silently get
edited away."""
from gremlin_core.intent import _CLASSIFY_PROMPT


def test_classify_prompt_distinguishes_code_in_reply_from_a_real_file_or_command():
    assert "CODE TEXT" in _CLASSIFY_PROMPT
    assert "write me a function" in _CLASSIFY_PROMPT.lower()
