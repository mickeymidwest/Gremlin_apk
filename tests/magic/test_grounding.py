"""grounding.check / grounding.ground -- catching invented files, bad
line cites, phantom commands, and unbacked back-references."""
from gremlin_core.magic import grounding


def _repo(tmp_path):
    (tmp_path / "gremlin_core" / "magic").mkdir(parents=True)
    (tmp_path / "gremlin_core" / "magic" / "reply.py").write_text("a\nb\nc\n")
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "zoid-nightly.sh").write_text("#!/bin/sh\n")
    return tmp_path


def test_clean_answer_no_findings(tmp_path):
    r = _repo(tmp_path)
    txt = ("Look at `gremlin_core/magic/reply.py` and run "
           "`bash deploy/zoid-nightly.sh` -- that's the loop.")
    assert grounding.check(txt, r) == []


def test_flags_nonexistent_file(tmp_path):
    r = _repo(tmp_path)
    out = grounding.check("The fix is in `gremlin_core/magic/repair.py`.", r)
    assert out and "repair.py" in out[0]


def test_flags_bad_line_cite(tmp_path):
    r = _repo(tmp_path)
    out = grounding.check("See `gremlin_core/magic/reply.py:400` for the guard.", r)
    assert out and "only 3 lines" in out[0]


def test_accepts_in_range_line_cite(tmp_path):
    r = _repo(tmp_path)
    assert grounding.check("`gremlin_core/magic/reply.py:2` sets it.", r) == []


def test_flags_phantom_command(tmp_path):
    r = _repo(tmp_path)
    out = grounding.check("Just run `./fix-everything.sh` and wait.", r)
    assert out and "fix-everything.sh" in out[0]


def test_ignores_system_paths_and_urls(tmp_path):
    r = _repo(tmp_path)
    txt = ("Check `/etc/fstab` and https://example.com/a/b.html and the "
           "usr/lib/thing.so loader.")
    assert grounding.check(txt, r) == []


def test_ignores_prose_slashes(tmp_path):
    r = _repo(tmp_path)
    assert grounding.check("Use this and/or that, 60 km/h tops.", r) == []


def test_backref_without_context_flagged(tmp_path):
    r = _repo(tmp_path)
    out = grounding.check("As I mentioned earlier, the daemon restarts itself.", r)
    assert out and "earlier work" in out[0]


def test_backref_with_context_ok(tmp_path):
    r = _repo(tmp_path)
    out = grounding.check("As we discussed above, restart it.", r,
                          context="user asked about the daemon restart loop")
    assert out == []


def test_path_present_in_context_is_ok(tmp_path):
    r = _repo(tmp_path)
    out = grounding.check("Edit `services/api/main.go` there.", r,
                          context="the repo layout includes services/api/main.go")
    assert out == []


# ---- ground(): regenerate once -----------------------------------

def test_ground_retries_and_keeps_cleaner(tmp_path):
    r = _repo(tmp_path)
    drafts = iter([
        "Run `./ghost.sh` to fix it.",                 # 1 finding
        "Run `bash deploy/zoid-nightly.sh` instead.",   # clean
    ])
    seen_hints = []

    def gen(hint):
        seen_hints.append(hint)
        return next(drafts)

    text, findings = grounding.ground(gen, r)
    assert findings == []
    assert "zoid-nightly.sh" in text
    assert seen_hints[0] is None and seen_hints[1] is not None


def test_ground_keeps_first_if_retry_no_better(tmp_path):
    r = _repo(tmp_path)
    drafts = iter(["mentions `./a.sh`", "mentions `./b.sh` and `./c.sh`"])
    text, findings = grounding.ground(lambda h: next(drafts), r)
    assert "a.sh" in text and 0 < len(findings) < 3


def test_caveat_string():
    assert grounding.caveat([]) == ""
    assert "Heads up" in grounding.caveat(["refers to `x.py`"])
