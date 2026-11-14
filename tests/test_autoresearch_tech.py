"""Tech-lookup: a named CLI tool + a how-to/verify shape makes Magic web-check
the real docs instead of trusting the pilot's memory (the 2026-11-13 `apt
upgrade -f` miss). Generic coding and box questions stay offline."""
from magic_core import autoresearch as ar


def test_the_real_upgrr_case_now_searches():
    # the exact live message that went un-checked and stated -f wrong
    msg = ("If I was writing a termux bash script for a command called upgrr "
           "and it a run apt update && apt upgrade -y what would I put in the script")
    assert ar.should_search(msg) is True


def test_flag_and_howto_questions_search():
    assert ar.should_search("what does apt upgrade -f do") is True
    assert ar.should_search("how do I fix broken dependencies with apt") is True
    assert ar.should_search("what is the correct flag for git rebase") is True
    assert ar.should_search("how do I write a systemd service file") is True


def test_tech_lookup_overrides_script_for_creative_veto():
    # "script for <tool>" must not be vetoed as creative writing when it's a
    # real tech ask (a how-to verb + a concrete CLI tool present)
    assert ar._tech_lookup("write a bash script for apt upgrade") is True
    assert ar.should_search("write a bash script for docker compose cleanup") is True


def test_generic_coding_stays_offline():
    # no concrete CLI tool -> not a tech lookup; generic 'write' stays creative
    assert ar._tech_lookup("write a python function to sort a list") is False
    assert ar.should_search("write a bash script to loop over files") is False
    assert ar.should_search("how do I center a div in css") is False


def test_box_questions_still_never_search():
    # self/local veto still wins over tech
    assert ar.should_search("what's running in my docker containers") is False
    assert ar.should_search("restart my systemctl service magic") is False


def test_creative_writing_still_vetoed():
    assert ar.should_search("write me a short story about a dragon") is False
    assert ar.should_search("compose a poem about the sea") is False
