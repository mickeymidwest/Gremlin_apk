from listops import chunk, dedupe, flatten_once, running_max, take_while


def test_chunk():
    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert chunk([], 3) == []


def test_dedupe_keeps_first_and_order():
    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]
    assert dedupe([]) == []


def test_flatten_once():
    assert flatten_once([[1, 2], [3], [4, 5]]) == [1, 2, 3, 4, 5]
    assert flatten_once([1, [2, 3], 4]) == [1, 2, 3, 4]


def test_running_max():
    assert running_max([1, 3, 2, 5, 4]) == [1, 3, 3, 5, 5]


def test_take_while_is_a_prefix():
    assert take_while(lambda x: x < 3, [1, 2, 5, 1, 2]) == [1, 2]
    assert take_while(lambda x: x > 0, [1, 2, 3]) == [1, 2, 3]
    assert take_while(lambda x: x > 0, [-1, 2, 3]) == []
