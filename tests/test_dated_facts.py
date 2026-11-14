"""R4 step 2: dated facts parsed from the store + stale flagging in the block."""
import time

from magic_core import memory_select as M
from magic_core.magic.store import Store


def test_read_facts_parses_timestamp(tmp_path, monkeypatch):
    mem = tmp_path / "magic_memory.txt"
    mem.write_text(
        "# memory\n"
        "- [2024-03-01 09:30] the desktop had a GTX 1660 back then\n"
        "- a hand-added fact with no timestamp\n")
    monkeypatch.setenv("MAGIC_MEMORY_FILE", str(mem))
    facts = Store(str(tmp_path)).read_facts()
    byid = {f.text: f for f in facts}
    dated = byid["the desktop had a GTX 1660 back then"]
    assert abs(dated.created - time.mktime(time.strptime("2024-03-01 09:30", "%Y-%m-%d %H:%M"))) < 1
    # an undated fact keeps the default (~now), never a false old date
    undated = byid["a hand-added fact with no timestamp"]
    assert undated.created > time.time() - 60


def test_block_flags_stale_fact_only():
    now = time.time()
    facts = ["old gpu was a GTX 1660", "current gpu is an RTX 2070 Super"]
    created = [now - 400 * 86400, now - 1 * 86400]   # ~13 months vs 1 day
    out = M.block(facts, "what gpu", created=created, stale_days=120,
                  recent=0, matched=5)
    assert "GTX 1660" in out and "may be out of date" in out
    # the fresh fact is NOT flagged
    assert "RTX 2070 Super" in out
    assert out.count("may be out of date") == 1


def test_block_without_dates_is_unchanged():
    facts = ["some fact about the box"]
    out = M.block(facts, "box", recent=0, matched=5)
    assert "may be out of date" not in out and "some fact about the box" in out
