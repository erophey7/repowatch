import pytest

from repowatch.sizes import format_byte_rate, format_bytes, parse_byte_rate, parse_byte_rate_loose


@pytest.mark.parametrize("value", [5000000, 5000000.5, None])
def test_parse_byte_rate_passes_through_non_strings_unchanged(value):
    assert parse_byte_rate(value) is value


@pytest.mark.parametrize("text,expected", [
    ("10 MiB", 10 * 1024 ** 2),
    ("10MiB", 10 * 1024 ** 2),
    ("10 MiB/s", 10 * 1024 ** 2),
    ("10 MiB/sec", 10 * 1024 ** 2),
    ("1.5 GiB", 1.5 * 1024 ** 3),
    ("512 KiB", 512 * 1024),
    ("512 KB", 512 * 1000),
    ("10 MB", 10 * 1000 ** 2),
    ("1 GB", 1000 ** 3),
    ("1 TiB", 1024 ** 4),
    ("1 TB", 1000 ** 4),
    ("100 B", 100),
    ("  10   MiB  ", 10 * 1024 ** 2),
    ("10 mib", 10 * 1024 ** 2),
])
def test_parse_byte_rate_accepts_unit_strings(text, expected):
    assert parse_byte_rate(text) == expected


@pytest.mark.parametrize("text", ["5000000", "5000000.5", "", "10", "MiB", "10 XiB", "10 MiB extra", "ten MiB"])
def test_parse_byte_rate_rejects_bare_numbers_and_malformed_strings(text):
    with pytest.raises(ValueError):
        parse_byte_rate(text)


def test_parse_byte_rate_loose_accepts_bare_numeric_strings():
    assert parse_byte_rate_loose("5000000") == 5000000.0
    assert parse_byte_rate_loose("5000000.5") == 5000000.5


def test_parse_byte_rate_loose_still_accepts_unit_strings():
    assert parse_byte_rate_loose("10 MiB") == 10 * 1024 ** 2


def test_parse_byte_rate_loose_still_rejects_malformed_strings():
    with pytest.raises(ValueError):
        parse_byte_rate_loose("ten MiB")


def test_parse_byte_rate_loose_passes_through_non_strings_unchanged():
    assert parse_byte_rate_loose(5000000) == 5000000
    assert parse_byte_rate_loose(None) is None


@pytest.mark.parametrize("n,expected", [
    (0, "0 B"),
    (999, "999 B"),
    (1024, "1.0 KiB"),
    (1536, "1.5 KiB"),
    (10 * 1024 ** 2, "10.0 MiB"),
    (1.5 * 1024 ** 3, "1.5 GiB"),
    (1024 ** 4, "1.0 TiB"),
    (-1024, "-1.0 KiB"),
])
def test_format_bytes(n, expected):
    assert format_bytes(n) == expected


def test_format_byte_rate_appends_per_second():
    assert format_byte_rate(10 * 1024 ** 2) == "10.0 MiB/s"


def test_format_byte_rate_reports_unlimited_for_none():
    assert format_byte_rate(None) == "unlimited"


@pytest.mark.parametrize("n", [0, 1, 999, 1024, 1536, 10 * 1024**2, 1.5 * 1024**3, 5 * 1024**4, 123456789])
def test_format_and_parse_byte_rate_round_trip_within_rounding_tolerance(n):
    """format_bytes rounds to one decimal place, so the round trip isn't
    exact — but must stay within that rounding's own error bound, proving
    the two use the same unit thresholds/multipliers."""
    formatted = format_bytes(n)
    text, unit = formatted.rsplit(" ", 1)
    reparsed = parse_byte_rate(f"{text} {unit}")
    tolerance = {"B": 0, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}[unit] * 0.05
    assert abs(reparsed - n) <= tolerance
