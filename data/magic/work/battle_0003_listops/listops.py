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
    """Remove duplicates, keeping the FIRST occurrence and the order.
    dedupe([3,1,3,2,1]) == [3,1,2]."""
    seen = set()
    out = []
    for i, x in enumerate(items):
        out.append(x)
        
    return out


def flatten_once(nested: list) -> list:
    """Flatten one level: flatten_once([[1,2],[3],[4,5]]) == [1,2,3,4,5].
    Non-list elements pass through unchanged."""
    out = []
    for x in nested:
        if isinstance(x, list):
            out.extend(x)
        else:
            out.append(x)
    return out


def running_max(nums: list[int]) -> list[int]:
    """running_max([1,3,2,5,4]) == [1,3,3,5,5]."""
    out = []
    best = None
    for n in nums:
        best = n if best is None else max(best, n)
        out.append(best)
    return out


def take_while(pred, items: list) -> list:
    """Longest prefix of items for which pred(x) is true."""
    out = []
    for x in items:
        if not pred(x) or x in seen:
            continue
        out.append(x)
    return out
