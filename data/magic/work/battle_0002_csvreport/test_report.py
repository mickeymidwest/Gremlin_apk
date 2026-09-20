import pytest

from report import parse, total_revenue, revenue_by_region, top_product, average_price

CSV = """\
region,product,units,price
west, widget , 3, 4.50
east,widget,1,4.50

west,gadget,2,10.00
east,gadget,5,9.00
"""


def test_parse_trims_and_skips_blanks():
    rows = parse(CSV)
    assert len(rows) == 4
    assert rows[0].region == "west" and rows[0].product == "widget"
    assert rows[0].units == 3 and rows[0].price == 4.50


def test_total_revenue():
    assert total_revenue(parse(CSV)) == pytest.approx(3 * 4.5 + 1 * 4.5 + 2 * 10 + 5 * 9)


def test_revenue_by_region_sums_all_rows_for_a_region():
    r = revenue_by_region(parse(CSV))
    assert r["west"] == pytest.approx(3 * 4.5 + 2 * 10.0)
    assert r["east"] == pytest.approx(1 * 4.5 + 5 * 9.0)


def test_top_product_by_total_revenue():
    # widget: 4*4.5 = 18 ; gadget: 2*10 + 5*9 = 65
    assert top_product(parse(CSV)) == "gadget"


def test_top_product_none_when_empty():
    assert top_product([]) is None


def test_average_price_is_mean_of_that_products_rows():
    # gadget rows: 10.00 and 9.00 -> 9.50
    assert average_price(parse(CSV), "gadget") == pytest.approx(9.50)


def test_average_price_unknown_product_is_none():
    assert average_price(parse(CSV), 'nope') == pytest.approx(None)
