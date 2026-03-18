"""Tests for fetch_yields.py and db.py yield functions."""

from datetime import date, timedelta

from fetch_yields import _parse_rate
from db import _compute_yield, interpolate_treasury_rate


# ── _parse_rate ──────────────────────────────────────────────────────────────


def test_parse_rate_number():
    assert _parse_rate("4.53") == 4.53


def test_parse_rate_integer():
    assert _parse_rate("5") == 5.0


def test_parse_rate_na():
    assert _parse_rate("N/A") is None


def test_parse_rate_empty():
    assert _parse_rate("") is None


def test_parse_rate_whitespace():
    assert _parse_rate("  ") is None


def test_parse_rate_none_like():
    assert _parse_rate("abc") is None


# ── interpolate_treasury_rate ────────────────────────────────────────────────


def test_interpolate_exact_match():
    yields = {"m3": 4.5, "m6": 5.0}
    assert interpolate_treasury_rate(yields, 91) == 4.5


def test_interpolate_between():
    yields = {"m3": 4.0, "m6": 5.0}
    rate = interpolate_treasury_rate(yields, 136)
    assert rate is not None
    # Halfway between 91d and 182d -> ~4.49
    assert 4.0 < rate < 5.0


def test_interpolate_clamp_low():
    yields = {"m3": 4.5}
    assert interpolate_treasury_rate(yields, 5) == 4.5


def test_interpolate_clamp_high():
    yields = {"m3": 4.5}
    assert interpolate_treasury_rate(yields, 99999) == 4.5


def test_interpolate_none_yields():
    assert interpolate_treasury_rate(None, 90) is None


def test_interpolate_empty_dict():
    assert interpolate_treasury_rate({}, 90) is None


def test_interpolate_zero_days():
    assert interpolate_treasury_rate({"m3": 4.5}, 0) is None


def test_interpolate_negative_days():
    assert interpolate_treasury_rate({"m3": 4.5}, -10) is None


# ── _compute_yield ───────────────────────────────────────────────────────────


def test_compute_yield_normal():
    # 90 days out, cost 0.95 -> (1/0.95)^(365/90) - 1
    future = (date.today() + timedelta(days=90)).isoformat() + "Z"
    ann_yield, days = _compute_yield(0.95, future)
    assert ann_yield is not None
    assert days == 90
    assert ann_yield > 0


def test_compute_yield_zero_cost():
    future = (date.today() + timedelta(days=90)).isoformat() + "Z"
    ann_yield, days = _compute_yield(0.0, future)
    assert ann_yield is None
    assert days is None


def test_compute_yield_negative_cost():
    future = (date.today() + timedelta(days=90)).isoformat() + "Z"
    ann_yield, days = _compute_yield(-0.05, future)
    assert ann_yield is None


def test_compute_yield_past_expiration():
    past = (date.today() - timedelta(days=5)).isoformat() + "Z"
    ann_yield, days = _compute_yield(0.95, past)
    assert ann_yield is None


def test_compute_yield_none_cost():
    future = (date.today() + timedelta(days=90)).isoformat() + "Z"
    assert _compute_yield(None, future) == (None, None)


def test_compute_yield_none_expiration():
    assert _compute_yield(0.95, None) == (None, None)


def test_compute_yield_empty_expiration():
    assert _compute_yield(0.95, "") == (None, None)


def test_compute_yield_overflow_returns_inf():
    """Extremely small cost with few days to expiration should return inf, not crash."""
    future = (date.today() + timedelta(days=1)).isoformat() + "Z"
    ann_yield, days = _compute_yield(0.0001, future)
    assert ann_yield == float("inf")
    assert days == 1
