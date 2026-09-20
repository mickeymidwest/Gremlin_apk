"""Small list helpers. Two of these have a bug that a test in
test_listops.py pins down. Fix the bodies; keep the signatures; don't
edit the tests.
"""


def chunk(items: list, size: int) -> list[list]:
    """Split `items` into consecutive sublists of length `size`; the last
    chunk may be shorter. chunk([1,2,3,4,5], 2) == [[1,2],[3,4],[5]]."""
    out = []
    for i in range(0, len(items), size):
        out.append(items[i:i + size])
    return out


def dedupe(items: list) -> list:
    return items
def flatten_once(nested: list) -> list:
    return []
def running_max(nums: list[int]) -> list[int]:
    """running_max([1,3,2,5,4]) == [1,3,3,5,5]."""
    out = []
    best = None
    for n in nums:
        best = n if best is None else max(best, n)
        out.append(best)
    return out


def take_while(pred, items: list) -> list:
    return []
