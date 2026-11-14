"""Regression tests for the bugs the 2026-11-08 Gemini code sweep found in
the auto-research gate (several introduced that night)."""
import datetime

from magic_core import autoresearch as a


def test_bug3_figure_check_uses_exact_values_not_substring():
    # a fabricated $20M / 26% must be flagged even though "20"/"26" are
    # substrings of the seeded year "2026" (the blob+substring bug).
    ev = '1. Unrelated title -- https://example.com\n   No numbers here.'
    figs = a.unverified_figures("It made $20 million and holds a 26% score.", ev)
    joined = " ".join(figs)
    assert "20" in joined and "26" in joined, figs
    # the real current year it may echo is still never flagged
    yr = str(datetime.date.today().year)
    assert yr not in " ".join(a.unverified_figures(f"As of {yr} it's new.", ev))


def test_bug6_the_box_office_and_generic_nouns_are_searchable():
    assert a.should_search("how much did the movie make at the box office today?") is True
    assert a.should_search("who invented the computer?") is True
    assert a.should_search("how tall is michael jordan?") is True


def test_bug7_creative_and_local_ops_do_not_search():
    assert a.should_search("write a story about who is the real killer") is False
    assert a.should_search("convert this python dict to json for me") is False
    assert a.should_search("compose a poem about who won the war") is False


def test_bug8_personal_checkins_do_not_search():
    assert a.should_search("how was your day today?") is False
    assert a.should_search("are you busy right now?") is False
    assert a.should_search("hows your weekend going so far") is False


def test_bug13_yesterday_tomorrow_not_stamped_with_today():
    assert a.search_query("who won the game yesterday") == "who won the game yesterday"
    assert a.search_query("whats on tomorrow night") == "whats on tomorrow night"
    # present-tense still gets stamped
    d = datetime.date.today()
    assert a.search_query("who plays tonight").endswith(f"{d.strftime('%B')} {d.day} {d.year}")


def test_bug14_sigfig_does_not_eat_following_word():
    figs = a.unverified_figures("It cost $500 kitchen remodel", "no relevant digits")
    assert "$500" in " ".join(figs)
    assert not any("k" in f.split("$500")[-1] for f in figs)  # not "$500 k"


def test_bug4_date_note_in_hist_not_in_grounding_ctx():
    from magic_core.magic import reply
    _msg, hist, ctx = reply._build_prompt("hi there", ".", "", "")
    assert "Today's date is" not in ctx                         # kept out of grounding ctx
    assert any("Today's date is" in h.get("content", "") for h in hist)  # model still told


def test_evidence_number_in_any_format_is_not_flagged():
    ev = 'Results.\n1. Box office -- https://x.com\n   The film grossed 58M on 599 screens.'
    # model reformats 58M as "$58 million" -- still supported (58 is in evidence)
    assert a.unverified_figures("It made $58 million at the box office.", ev) == []


def test_still_searches_the_real_embedded_question():
    # the thing the opinion-override was built for must still work
    assert a.should_search("so i wonder how many ppl liked the new resident evil i thought it was awesome") is True
    assert a.should_search("do you know who won the super bowl this year") is True
