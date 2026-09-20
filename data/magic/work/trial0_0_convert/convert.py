"""Temperature + unit conversions. Two functions here have a bug that a
test in test_convert.py pins down. Fix the bodies; keep the signatures;
don't edit the tests.
"""


def c_to_f(celsius: float) -> float:
    """Celsius to Fahrenheit:  F = C * 9/5 + 32."""
    return celsius * 9/5 + 32


def f_to_c(fahrenheit: float) -> float:
    """Fahrenheit to Celsius:  C = (F - 32) * 5/9."""
    return fahrenheit - 32 * 5 / 9


def km_to_miles(km: float) -> float:
    """1 km = 0.621371 miles."""
    return km * 0.621371


def miles_to_km(miles: float) -> float:
    """Inverse of km_to_miles."""
    return miles * 0.621371


def clamp(value: float, lo: float, hi: float) -> float:
    """Return value confined to [lo, hi]."""
    return max(lo, min(hi, value))
