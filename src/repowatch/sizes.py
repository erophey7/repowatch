"""Parse and format byte-rate sizes ("10 MiB", "512 KB/s") for
prefetch_bandwidth_limit fields (global, per-repository, and each
bandwidth schedule window's limit — see bandwidth.py) — the one shared
place config.yaml, the dashboard's global settings API and the
repository add/edit API funnel a human-typed size through before it
reaches RepoConfig/Config's own strict int/float-only
bandwidth.validate_limit().

This is not a reversal of the 2026-09-16 B01/B02 audit hardening (see
CLAUDE.md), which deliberately removed silent numeric-string coercion in
the YAML loader (a bare quoted "5000000" is still rejected there,
unchanged) — the unit suffix here is mandatory in parse_byte_rate(), so
an ambiguous bare numeric string still has to stay a real YAML/JSON
number. parse_byte_rate_loose() is the one exception, and only at the
HTML-form-shaped API boundary (web/settings.py, operations/repositories.py)
where a plain numeric string has always been an accepted, unambiguous
form (an HTML form field is inherently text) — see SAFE_CONFIG_FIELDS'
own casters for the established precedent."""

from __future__ import annotations

import re

# Both decimal (KB/MB/GB/TB, 1000-based) and binary (KiB/MiB/GiB/TiB,
# 1024-based) are accepted on input — an operator typing a size rarely
# means to distinguish the two precisely. Display (format_bytes) always
# uses the binary ones, matching this project's own documentation
# convention (docs_dev/*, e.g. "13.3 GiB", "165.78 MiB").
_UNITS = {
    'B': 1,
    'KB': 1000, 'KIB': 1024,
    'MB': 1000 ** 2, 'MIB': 1024 ** 2,
    'GB': 1000 ** 3, 'GIB': 1024 ** 3,
    'TB': 1000 ** 4, 'TIB': 1024 ** 4,
}
# An optional trailing "/s" or "/sec" is accepted and ignored — every
# field this is used for is itself a rate (bytes/sec), so "10 MiB/s"
# reads naturally; the unit conversion is identical either way.
_SIZE_RE = re.compile(r'^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*(?:/\s*s(?:ec)?)?\s*$')


def parse_byte_rate(value: object) -> object:
    """int/float/None pass through unchanged — the existing plain-bytes
    contract used by bandwidth.validate_limit(). A str must carry an
    explicit unit suffix ("10 MiB", "512 KB/s", "1.5 GiB"); anything else
    (including a bare numeric string) raises ValueError with an
    actionable message rather than guessing. Any other type is returned
    unchanged too, so the caller's own type check (bandwidth.validate_limit)
    produces the error, not a confusing one from here."""
    if value is None or isinstance(value, (int, float)) or not isinstance(value, str):
        return value
    match = _SIZE_RE.fullmatch(value)
    if not match:
        raise ValueError(
            f'{value!r} is not a valid size — use a unit such as "10 MiB" or "512 KB/s"'
        )
    unit = match.group(2).upper()
    if unit not in _UNITS:
        raise ValueError(
            f'unknown unit {match.group(2)!r} in {value!r} — use B/KB/KiB/MB/MiB/GB/GiB/TB/TiB'
        )
    return float(match.group(1)) * _UNITS[unit]


def parse_byte_rate_loose(value: object) -> object:
    """Same as parse_byte_rate(), but a bare numeric string ("5000000",
    with no unit) is also accepted — for the HTML-form-shaped API
    boundary only (see this module's own docstring for why the strict
    YAML-facing parse_byte_rate() does not do this)."""
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    return parse_byte_rate(value)


_DISPLAY_UNITS = (('TiB', 1024 ** 4), ('GiB', 1024 ** 3), ('MiB', 1024 ** 2), ('KiB', 1024))


def format_bytes(n: float | int) -> str:
    """Human-readable binary size for display (CLI text output; the
    dashboard has its own equivalent in static/js/storage.js so it can
    format without a round trip). Values under 1 KiB are shown as a
    whole number of bytes."""
    value = float(n)
    sign = '-' if value < 0 else ''
    value = abs(value)
    for unit, size in _DISPLAY_UNITS:
        if value >= size:
            return f'{sign}{value / size:.1f} {unit}'
    return f'{sign}{value:.0f} B'


def format_byte_rate(n: float | int | None) -> str:
    """Same as format_bytes(), for a bytes/sec rate — appends "/s", and
    reports "unlimited" for None (the existing "no limit" sentinel for
    prefetch_bandwidth_limit and a schedule window's limit)."""
    if n is None:
        return 'unlimited'
    return f'{format_bytes(n)}/s'
