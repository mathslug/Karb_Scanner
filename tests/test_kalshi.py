"""Tests for kalshi.py — fee model and book walking."""

from kalshi import Fill, LegResult, taker_fee, walk_book


# ── taker_fee ────────────────────────────────────────────────────────────────


def test_taker_fee_midpoint():
    # P=0.50: ceil(0.07 * 10 * 0.50 * 0.50 * 100) / 100 = ceil(17.5)/100 = 0.18
    assert taker_fee(10, 0.50) == 0.18


def test_taker_fee_low_price():
    # P=0.10: ceil(0.07 * 5 * 0.10 * 0.90 * 100) / 100 = ceil(3.15)/100 = 0.04
    assert taker_fee(5, 0.10) == 0.04


def test_taker_fee_high_price():
    # P=0.90: ceil(0.07 * 5 * 0.90 * 0.10 * 100) / 100 = ceil(3.15)/100 = 0.04
    assert taker_fee(5, 0.90) == 0.04


def test_taker_fee_symmetry():
    # Fee is symmetric around 0.50
    assert taker_fee(10, 0.30) == taker_fee(10, 0.70)


def test_taker_fee_price_zero():
    assert taker_fee(10, 0.0) == 0.0


def test_taker_fee_price_one():
    assert taker_fee(10, 1.0) == 0.0


def test_taker_fee_zero_contracts():
    assert taker_fee(0, 0.50) == 0.0


def test_taker_fee_single_contract():
    # P=0.50: ceil(0.07 * 1 * 0.25 * 100) / 100 = ceil(1.75)/100 = 0.02
    assert taker_fee(1, 0.50) == 0.02


# ── walk_book ────────────────────────────────────────────────────────────────


def test_walk_book_single_level():
    # Opposite bids at $0.60 with qty 10 -> fill price = 1 - 0.60 = 0.40
    bids = [(0.60, 10)]
    result = walk_book(bids, 5)
    assert result.filled == 5
    assert result.requested == 5
    assert result.sufficient
    assert len(result.fills) == 1
    assert result.fills[0].price == 0.40
    assert result.fills[0].qty == 5
    assert result.cost == 0.40 * 5


def test_walk_book_multiple_levels():
    # Two levels: best bid 0.70 qty 3, next bid 0.60 qty 5
    bids = [(0.70, 3), (0.60, 5)]
    result = walk_book(bids, 6)
    assert result.filled == 6
    assert result.sufficient
    assert len(result.fills) == 2
    assert result.fills[0].price == 0.30  # 1 - 0.70
    assert result.fills[0].qty == 3
    assert result.fills[1].price == 0.40  # 1 - 0.60
    assert result.fills[1].qty == 3
    expected_cost = 0.30 * 3 + 0.40 * 3
    assert abs(result.cost - expected_cost) < 1e-10


def test_walk_book_partial_fill():
    bids = [(0.60, 3)]
    result = walk_book(bids, 10)
    assert result.filled == 3
    assert result.requested == 10
    assert not result.sufficient


def test_walk_book_empty():
    result = walk_book([], 5)
    assert result.filled == 0
    assert result.requested == 5
    assert not result.sufficient
    assert result.fills == []
    assert result.cost == 0.0
    assert result.fees == 0.0


def test_walk_book_zero_contracts():
    bids = [(0.60, 10)]
    result = walk_book(bids, 0)
    assert result.filled == 0
    assert result.sufficient  # 0 >= 0
    assert result.fills == []


def test_walk_book_exact_fill():
    bids = [(0.70, 5), (0.60, 5)]
    result = walk_book(bids, 10)
    assert result.filled == 10
    assert result.sufficient


def test_walk_book_fees_accumulated():
    bids = [(0.60, 5)]
    result = walk_book(bids, 5)
    fill_price = 0.40
    expected_fee = taker_fee(5, fill_price)
    assert result.fees == expected_fee
    assert result.fills[0].fee == expected_fee


# ── LegResult.sufficient ─────────────────────────────────────────────────────


def test_leg_result_sufficient_exact():
    lr = LegResult(fills=[], cost=0, fees=0, filled=10, requested=10)
    assert lr.sufficient


def test_leg_result_sufficient_over():
    lr = LegResult(fills=[], cost=0, fees=0, filled=15, requested=10)
    assert lr.sufficient


def test_leg_result_insufficient():
    lr = LegResult(fills=[], cost=0, fees=0, filled=5, requested=10)
    assert not lr.sufficient
