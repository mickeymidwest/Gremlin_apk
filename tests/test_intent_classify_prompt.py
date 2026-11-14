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
from magic_core.intent import _CLASSIFY_PROMPT


def test_classify_prompt_distinguishes_code_in_reply_from_a_real_file_or_command():
    assert "CODE TEXT" in _CLASSIFY_PROMPT
    assert "write me a function" in _CLASSIFY_PROMPT.lower()


def test_action_hint_fires_on_inspect_phrasings():
    """Regression: inspect/reverse-engineer requests must trip the _ACTION_HINT
    pre-filter so classify() runs and the read-only inspect tool is reachable.
    Before this they fell straight through to plain chat (Magic role-played
    decompiling instead of running the tool)."""
    from magic_core.intent import _ACTION_HINT as H
    fire = [
        "inspect the apk at /home/mickey/Downloads/gremlin-streaming.apk",
        "is this apk asking for anything sketchy?",
        "reverse engineer /usr/bin/ls",
        "can you decompile this app",
        "what does this file do",
        "/home/mickey/Downloads/app.apk",
        "inspect https://example.com",
        "is it safe to install",
    ]
    for c in fire:
        assert H.search(c), f"_ACTION_HINT should fire on: {c!r}"
    for c in ["how was your day", "tell me a joke", "what time is it in Tokyo"]:
        assert not H.search(c), f"_ACTION_HINT should NOT fire on: {c!r}"
