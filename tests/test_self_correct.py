"""Live-path self-correction: diagnose + enrich failed actions."""
from magic_core import self_correct as SC


def test_diagnose_common_failures():
    cases = {
        "systemd unit not on the allow-list": "can-do list",
        "bash: permission denied": "permission was denied",
        "ls: cannot access '/x': No such file or directory": "doesn't exist",
        "zsh: command not found: foo": "isn't installed",
        "Failed to stop magic-x.timer: Unit magic-x.timer not found": "unit name isn't right",
        "Error: No such container: sonar": "container name isn't right",
        "fork: Cannot allocate memory": "ran out of memory",
        "write error: No space left on device": "disk is full",
        "operation timed out after 60s": "timed out",
        "RESOURCE_EXHAUSTED (code 429): quota": "quota/rate limit",
        "3 failed, 5 passed": "still failing",
    }
    for err, expect in cases.items():
        d = SC.diagnose(err)
        assert d is not None, f"no diagnosis for {err!r}"
        assert expect in d[0], f"{err!r} -> {d[0]!r} (wanted {expect!r})"


def test_enrich_appends_steps():
    out = SC.enrich("Couldn't restart it: permission denied")
    assert "What went wrong:" in out and "To get it right:" in out
    assert "Override (sudo)" in out
    assert out.startswith("Couldn't restart it")   # original answer kept


def test_enrich_is_idempotent():
    once = SC.enrich("permission denied")
    twice = SC.enrich(once)
    assert once == twice                            # won't double-append


def test_enrich_noop_on_unrecognized_or_success():
    assert SC.enrich("Done -- restarted jellyfin, it's healthy.") == "Done -- restarted jellyfin, it's healthy."
    assert SC.enrich("") == ""


def test_specific_before_generic():
    # "unit not found" is a failure, but should map to the unit-name rule,
    # not the generic "still failing" rule
    d = SC.diagnose("Failed to start x: Unit x.service not found")
    assert "unit name" in d[0]
