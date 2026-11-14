"""Live bug 2026-11-14: in normal chat, how-to QUESTIONS kept firing an action
(build_project / read_file) instead of being answered. "How would I write a
bash script...", "tell me how to build it", "show me the script" all proposed
building a project. The weak CPU classifier read the action word and acted.
_QUESTION_HINT now forces chat for question phrasings, before the classifier."""
from magic_core import intent


def test_how_to_questions_force_chat():
    for msg in [
        "How would I write a bash script for a loop that auto takes pictures",
        "No just tell me how to build it",
        "Tell me how to build the script",
        "Show me the script",
        "No don't build it. Just tell me how I would build it",
        "how do i loop termux-camera-photo",
        "what's the best way to rename files in bash",
        "can you explain how systemd timers work",
        "show me an example of a for loop",
    ]:
        assert intent._QUESTION_HINT.search(msg), msg


def test_real_commands_not_swallowed():
    for msg in [
        "build me a tip calculator app",
        "fix the crash in server.py",
        "restart the daemon",
        "delete the old backups",
        "deploy the new build",
    ]:
        assert not intent._QUESTION_HINT.search(msg), msg


def test_code_authoring_requests_force_chat():
    # "write/show/give me a function/script/regex/..." wants the code TEXT,
    # not a file written or a project built.
    for msg in [
        "Write a Python function that returns the nth Fibonacci number",
        "write me a bash script that auto takes pictures",
        "show me a regex for emails",
        "give me an example of a for loop",
        "make a function to reverse a string",
        "create a sql query to count users",
    ]:
        assert __import__("magic_core.intent", fromlist=["_CODE_TEXT_HINT"])._CODE_TEXT_HINT.search(msg), msg


def test_real_projects_still_build():
    from magic_core import intent
    for msg in ["build me a tip calculator app", "make me a website for my band"]:
        assert not intent._CODE_TEXT_HINT.search(msg), msg
        assert not intent._QUESTION_HINT.search(msg), msg
