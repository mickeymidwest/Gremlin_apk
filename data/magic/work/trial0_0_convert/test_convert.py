import pytest

from convert import c_to_f, f_to_c, km_to_miles, miles_to_km, clamp


def test_c_to_f():
    assert c_to_f(0) == pytest.approx(32.0)
    assert c_to_f(100) == pytest.approx(212.0)


def test_f_to_c():
    assert f_to_c(32) == pytest.approx(0.0)
    assert f_to_c(212) == pytest.approx(100.0)


def test_round_trip_temp():
    assert f_to_c(c_to_f(37.0)) == pytest.approx(37.0)


def test_km_to_miles():
    assert km_to_miles(1) == pytest.approx(0.621371)


def test_miles_to_km():
    assert round(miles_to_km(1), 5) == pytest.approx(1.6093, rel=1e-4)
    assert miles_to_km(km_to_miles(5)) == pytest.approx(5.0)


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-3, 0, 10) == 0
    assert clamp(99, 0, 10) == 10
