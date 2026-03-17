"""Tests for evaluate_pair — binary search, yield calc, buy/pass logic.

Uses synthetic orderbooks patched over fetch_pair_books to avoid network I/O.
"""

from datetime import date, timedelta
from unittest.mock import patch

from main import evaluate_pair


def _pair(days_out=90):
    """Minimal pair dict with expiration `days_out` days from today."""
    exp = (date.today() + timedelta(days=days_out)).isoformat() + "Z"
    return {
        "id": 1,
        "antecedent_ticker": "ANT",
        "consequent_ticker": "CON",
        "antecedent_expiration": exp,
    }


def _books(ant_yes_bids, con_no_bids, ant_tob_no_ask=None, con_tob_yes_ask=None):
    """Build a books dict matching fetch_pair_books return format.

    ant_yes_bids: YES bids on antecedent (best-first). Walking these buys NO.
                  NO fill price = 1 - bid_price.
    con_no_bids:  NO bids on consequent (best-first). Walking these buys YES.
                  YES fill price = 1 - bid_price.
    """
    if ant_tob_no_ask is None:
        ant_tob_no_ask = round(1.0 - ant_yes_bids[0][0], 4) if ant_yes_bids else 0.99
    if con_tob_yes_ask is None:
        con_tob_yes_ask = round(1.0 - con_no_bids[0][0], 4) if con_no_bids else 0.99
    return {
        "ant_bids": ant_yes_bids,
        "con_bids": con_no_bids,
        "ant_tob_no_ask": ant_tob_no_ask,
        "con_tob_yes_ask": con_tob_yes_ask,
    }


# ── Clear arb: cheap pair, should recommend BUY ─────────────────────────────


@patch("main.fetch_pair_books")
def test_buy_cheap_pair(mock_books):
    # NO fill = $0.20, YES fill = $0.70 -> cost ~$0.90 + fees < $1.00
    mock_books.return_value = _books(
        ant_yes_bids=[(0.80, 50)],
        con_no_bids=[(0.30, 50)],
    )
    result = evaluate_pair(_pair(days_out=90), hurdle_yield=0.04, max_n=50)
    assert result["recommendation"] == "buy"
    assert result["n_contracts"] > 0
    assert result["annualized_yield"] > 0.04
    assert result["excess_yield"] > 0


# ── No arb: expensive pair, should PASS ──────────────────────────────────────


@patch("main.fetch_pair_books")
def test_pass_expensive_pair(mock_books):
    # NO fill = $0.60, YES fill = $0.70 -> cost ~$1.30 > $1.00
    mock_books.return_value = _books(
        ant_yes_bids=[(0.40, 50)],
        con_no_bids=[(0.30, 50)],
    )
    result = evaluate_pair(_pair(days_out=90), hurdle_yield=0.04, max_n=50)
    assert result["recommendation"] == "pass"


# ── Binary search finds optimal n with degrading depth ───────────────────────


@patch("main.fetch_pair_books")
def test_binary_search_degrading_depth(mock_books):
    # First 10 contracts are cheap, next 20 are more expensive, last 20 are bad
    mock_books.return_value = _books(
        ant_yes_bids=[(0.85, 10), (0.75, 20), (0.55, 20)],
        con_no_bids=[(0.40, 10), (0.30, 20), (0.15, 20)],
    )
    result = evaluate_pair(_pair(days_out=90), hurdle_yield=0.04, max_n=100)
    assert result["recommendation"] == "buy"
    # Should buy fewer than max available (50) since deep fills are expensive
    assert result["n_contracts"] < 50
    assert result["n_contracts"] >= 1
    assert result["annualized_yield"] >= 0.04


# ── Binary search: all depth is profitable → fills to max ────────────────────


@patch("main.fetch_pair_books")
def test_binary_search_fills_to_max(mock_books):
    # Uniformly cheap: $0.15 + $0.65 = $0.80 per pair at all depths
    mock_books.return_value = _books(
        ant_yes_bids=[(0.85, 100)],
        con_no_bids=[(0.35, 100)],
    )
    result = evaluate_pair(_pair(days_out=90), hurdle_yield=0.04, max_n=100)
    assert result["recommendation"] == "buy"
    assert result["n_contracts"] == 100
    assert result["max_fillable"] == 100


# ── Liquidity-constrained: one side shallow ──────────────────────────────────


@patch("main.fetch_pair_books")
def test_liquidity_constrained(mock_books):
    # Antecedent only has 5 contracts available
    mock_books.return_value = _books(
        ant_yes_bids=[(0.80, 5)],
        con_no_bids=[(0.30, 50)],
    )
    result = evaluate_pair(_pair(days_out=90), hurdle_yield=0.04, max_n=50)
    assert result["recommendation"] == "buy"
    assert result["n_contracts"] <= 5
    assert result["max_fillable"] == 5


# ── Empty orderbook → PASS ───────────────────────────────────────────────────


@patch("main.fetch_pair_books")
def test_empty_orderbook(mock_books):
    mock_books.return_value = _books(
        ant_yes_bids=[],
        con_no_bids=[(0.30, 50)],
    )
    result = evaluate_pair(_pair(days_out=90), hurdle_yield=0.04, max_n=50)
    assert result["recommendation"] == "pass"


# ── No expiration → PASS ─────────────────────────────────────────────────────


def test_no_expiration():
    pair = _pair()
    pair["antecedent_expiration"] = None
    result = evaluate_pair(pair, hurdle_yield=0.04)
    assert result["recommendation"] == "pass"
    assert result["days_to_maturity"] is None


# ── Past expiration → PASS ───────────────────────────────────────────────────


def test_past_expiration():
    result = evaluate_pair(_pair(days_out=-5), hurdle_yield=0.04)
    assert result["recommendation"] == "pass"


# ── Result shape: BUY result has all expected fields ─────────────────────────


@patch("main.fetch_pair_books")
def test_buy_result_fields(mock_books):
    mock_books.return_value = _books(
        ant_yes_bids=[(0.80, 50)],
        con_no_bids=[(0.30, 50)],
    )
    result = evaluate_pair(_pair(days_out=90), hurdle_yield=0.04, max_n=50)
    for key in (
        "pair_id", "recommendation", "n_contracts", "cost_per_pair",
        "total_cost", "ant_leg_cost", "ant_leg_fees", "con_leg_cost",
        "con_leg_fees", "annualized_yield", "hurdle_yield", "excess_yield",
        "days_to_maturity", "max_fillable", "tob_ant_no_ask",
        "tob_con_yes_ask", "tob_cost", "ant_fills", "con_fills",
    ):
        assert key in result, f"missing key: {key}"
    assert len(result["ant_fills"]) > 0
    assert len(result["con_fills"]) > 0


# ── High hurdle flips BUY to PASS on same books ─────────────────────────────


@patch("main.fetch_pair_books")
def test_high_hurdle_flips_to_pass(mock_books):
    # Cost ~$0.90 per pair, 90 days -> yield ~50%. Hurdle at 500% should reject.
    mock_books.return_value = _books(
        ant_yes_bids=[(0.80, 50)],
        con_no_bids=[(0.30, 50)],
    )
    result = evaluate_pair(_pair(days_out=90), hurdle_yield=5.0, max_n=50)
    assert result["recommendation"] == "pass"
