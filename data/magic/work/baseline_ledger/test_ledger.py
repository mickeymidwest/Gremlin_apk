import pytest

from ledger import Ledger


def test_balance_includes_opening_balance():
    lg = Ledger(opening_balance=100.0)
    lg.post("coffee", -4.50)
    lg.post("refund", 2.00)
    assert lg.balance() == pytest.approx(97.50)


def test_post_rejects_zero_amount():
    lg = Ledger()
    with pytest.raises(ValueError):
        lg.post("bogus", 0.0)
    assert lg.entries == []


def test_running_balance_is_after_each_entry():
    lg = Ledger(opening_balance=10.0)
    lg.post("a", 5.0)
    lg.post("b", -3.0)
    assert lg.running_balance() == [15.0, 12.0]


def test_biggest_expense_is_the_most_negative():
    lg = Ledger()
    lg.post("rent", -900.0)
    lg.post("snack", -3.0)
    lg.post("paycheck", 2000.0)
    assert lg.biggest_expense().desc == "rent"


def test_biggest_expense_none_when_no_debits():
    lg = Ledger()
    lg.post("gift", 50.0)
    assert lg.biggest_expense() is None


def test_reconcile_tolerates_floating_point_dust():
    lg = Ledger(opening_balance=0.0)
    for _ in range(3):
        lg.post("split", 0.1)
    # 0.1 * 3 == 0.30000000000000004 in float
    assert lg.reconcile(0.30) is True
