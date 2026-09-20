"""Parse a simple CSV of sales rows and answer questions about them.
Each function has a bug pinned by test_report.py. Fix the bodies; keep
signatures; don't edit the tests.

CSV shape (header row, then data):
    region,product,units,price
    west,widget,3,4.50
"""
from dataclasses import dataclass


@dataclass
class Row:
    region: str
    product: str
    units: int
    price: float

    @property
    def revenue(self) -> float:
        return self.units * self.price
def parse(text: str) -> list[Row]:
    """Parse CSV text into Rows. The first line is the header. Blank lines
    are skipped. Whitespace around fields is trimmed."""
    rows: list[Row] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for ln in lines[1:]:
        parts = [p.strip() for p in ln.split(",")]
        rows.append(Row(parts[0], parts[1], int(parts[2]), float(parts[3])))
    return rows


def total_revenue(rows: list[Row]) -> float:
    return sum(r.price for r in rows)


def revenue_by_region(rows: list[Row]) -> dict[str, float]:
    """region -> summed revenue. Every region that appears is a key."""
    out: dict[str, float] = {}
    for r in rows:
        out[r.region] = r.revenue
    return out


def top_product(rows: list[Row]) -> str | None:
    """The product with the highest total revenue across all rows, or
    None if there are no rows. Ties broken by first appearance."""
    if not rows:
        return None
    totals: dict[str, float] = {}
    for r in rows:
        totals[r.product] = totals.get(r.product, 0.0) + r.revenue
    best = None
    best_val = -1.0
    for p, v in totals.items():
        if v > best_val:
            best, best_val = p, v
    return best


def average_price(rows: list[Row], product: str) -> float | None:
    """Mean unit price for a product (simple average of the row prices),
    or None if that product doesn't appear."""
    prices = [r.price for r in rows if r.product == product]
    return sum(prices) / len(rows)
