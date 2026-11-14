"""The error ledger's recurring failures surface as a prompt 'known pitfalls'
block (preventive -- the model sees them before generating)."""
from magic_core import error_ledger as EL
from magic_core.magic import reply as reply_mod


def test_empty_on_healthy_box(tmp_path):
    assert reply_mod._pitfalls_block(str(tmp_path)) == ""


def test_only_recurring_failures_appear(tmp_path):
    root = str(tmp_path)
    # a one-off failure is NOT a pitfall yet
    EL.record(root, "start jellyfin", "No such container: jellyfin")
    assert reply_mod._pitfalls_block(root) == ""
    # once it recurs (2x), it becomes a standing pitfall
    EL.record(root, "start jellyfin", "No such container: jellyfin")
    block = reply_mod._pitfalls_block(root)
    assert "Known pitfalls" in block
    assert "container name isn't right" in block


def test_top_recurring_orders_by_count(tmp_path):
    root = str(tmp_path)
    for _ in range(2):
        EL.record(root, "mount disk", "No space left on device")
    for _ in range(4):
        EL.record(root, "restart svc", "permission denied")
    top = EL.top_recurring(root)
    assert top and "permission" in top[0]["what"]   # the 4x one leads
