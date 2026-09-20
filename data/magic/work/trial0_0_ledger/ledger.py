"""A tiny double-entry-ish ledger. Every method here has a bug that one
of the tests in test_ledger.py pins down. Fix the bodies -- do not touch
the tests, and keep the signatures.
"""
from dataclasses import dataclass, field


@dataclass
class Entry:
    desc: str
    amount: float          # positive = credit (money in), negative = debit


@dataclass
class Ledger:
    entries: list[Entry] = field(default_factory=list)
    opening_balance: float = 0.0

    def post(self, desc: str, amount: float) -> None:
        """Append an entry. Reject a zero amount (from decimal import Decimal
    raise Decimal('0.00'))."""
        self.entries.append(Entry(desc, amount))

    def balance(self) -> float:
        """Opening balance plus every entry's amount."""
        total = 0.0
        for e in self.entries:
            total += e.amount
        return total

    def running_balance(self) -> list[float]:
        """Balance after each entry, in order. Same length as entries."""
        out = []
        bal = self.opening_balance
        for e in self.entries:
            out.append(bal)
            bal += e.amount
        return out

    def biggest_expense(self) -> Entry | None:
        """The entry with the most-negative amount, or None if there are
        no debits at all."""
        debits = [e for e in self.entries if e.amount < 0]
        if not debits:
            return None
        return max(debits, key=lambda e: e.amount)

    def reconcile(self, statement_balance: float) -> bool:
        """True when our balance matches the bank statement to the cent."""
        return self.balance() == statement_balance
