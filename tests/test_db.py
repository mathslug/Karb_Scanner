"""Tests for db.py — SQLite persistence with in-memory database."""

import pytest

import db


@pytest.fixture
def conn():
    c = db.get_connection(":memory:")
    yield c
    c.close()


def _make_market(**overrides):
    """Helper to create a minimal market dict."""
    defaults = {
        "ticker": "TICK-A",
        "series_ticker": "SERIES-1",
        "event_ticker": "EVENT-1",
        "title": "Test Market",
        "yes_sub_title": "Entity A",
        "rules_primary": "Some rules",
        "expected_expiration_time": "2026-06-01T00:00:00Z",
        "close_time": "2026-06-01T00:00:00Z",
        "last_price_dollars": "0.50",
        "yes_ask_dollars": "0.55",
        "no_ask_dollars": "0.48",
        "volume": 500,
        "sport_tag": "Tennis",
        "sub_sport": "Tennis",
    }
    defaults.update(overrides)
    return defaults


# ── upsert_tickers ───────────────────────────────────────────────────────────


def test_upsert_tickers_insert(conn):
    markets = [_make_market(ticker="T1"), _make_market(ticker="T2")]
    new, updated = db.upsert_tickers(conn, markets)
    assert new == 2
    assert updated == 0


def test_upsert_tickers_update(conn):
    db.upsert_tickers(conn, [_make_market(ticker="T1", volume=100)])
    new, updated = db.upsert_tickers(conn, [_make_market(ticker="T1", volume=999)])
    assert new == 0
    assert updated == 1
    row = conn.execute("SELECT volume FROM tickers WHERE ticker = 'T1'").fetchone()
    assert row["volume"] == 999


# ── record_prices ────────────────────────────────────────────────────────────


def test_record_prices(conn):
    db.upsert_tickers(conn, [_make_market(ticker="T1")])
    count = db.record_prices(conn, [_make_market(ticker="T1")])
    assert count == 1
    rows = conn.execute("SELECT * FROM prices WHERE ticker = 'T1'").fetchall()
    assert len(rows) == 1

    # Record again — should append
    db.record_prices(conn, [_make_market(ticker="T1", last_price_dollars="0.60")])
    rows = conn.execute("SELECT * FROM prices WHERE ticker = 'T1'").fetchall()
    assert len(rows) == 2


# ── deactivate_missing_tickers ───────────────────────────────────────────────


def test_deactivate_missing_tickers(conn):
    db.upsert_tickers(conn, [
        _make_market(ticker="T1"),
        _make_market(ticker="T2"),
        _make_market(ticker="T3"),
    ])
    deactivated = db.deactivate_missing_tickers(conn, {"T1", "T3"})
    assert deactivated == 1
    row = conn.execute("SELECT is_active FROM tickers WHERE ticker = 'T2'").fetchone()
    assert row["is_active"] == 0


def test_deactivate_empty_set(conn):
    db.upsert_tickers(conn, [_make_market(ticker="T1")])
    assert db.deactivate_missing_tickers(conn, set()) == 0


# ── get_tickers_by_entity ────────────────────────────────────────────────────


def test_get_tickers_by_entity_groups(conn):
    # Two tickers with same entity in different series -> grouped
    db.upsert_tickers(conn, [
        _make_market(ticker="T1", series_ticker="S1", event_ticker="E1", yes_sub_title="Alcaraz"),
        _make_market(ticker="T2", series_ticker="S2", event_ticker="E2", yes_sub_title="Alcaraz"),
    ])
    groups = db.get_tickers_by_entity(conn)
    assert "Alcaraz" in groups
    assert len(groups["Alcaraz"]) == 2


def test_get_tickers_by_entity_requires_two_series(conn):
    # Same series -> not grouped
    db.upsert_tickers(conn, [
        _make_market(ticker="T1", series_ticker="S1", yes_sub_title="Alcaraz"),
        _make_market(ticker="T2", series_ticker="S1", yes_sub_title="Alcaraz"),
    ])
    groups = db.get_tickers_by_entity(conn)
    assert "Alcaraz" not in groups


def test_get_tickers_by_entity_min_volume(conn):
    db.upsert_tickers(conn, [
        _make_market(ticker="T1", series_ticker="S1", yes_sub_title="Alcaraz", volume=50),
        _make_market(ticker="T2", series_ticker="S2", yes_sub_title="Alcaraz", volume=50),
    ])
    groups = db.get_tickers_by_entity(conn, min_volume=100)
    assert "Alcaraz" not in groups


# ── bulk_upsert_pair_results + get_screened_pair_keys ────────────────────────


def test_bulk_upsert_and_screened_keys(conn):
    db.upsert_tickers(conn, [_make_market(ticker="A"), _make_market(ticker="B")])
    results = [{
        "ticker_a": "A",
        "ticker_b": "B",
        "antecedent_ticker": "A",
        "consequent_ticker": "B",
        "confidence": "high",
        "reasoning": "A implies B",
    }]
    count = db.bulk_upsert_pair_results(conn, results, "test-model")
    assert count == 1

    screened = db.get_screened_pair_keys(conn)
    assert ("A", "B") in screened


def test_bulk_upsert_sorted_order(conn):
    db.upsert_tickers(conn, [_make_market(ticker="A"), _make_market(ticker="Z")])
    results = [{"ticker_a": "Z", "ticker_b": "A", "confidence": "none"}]
    db.bulk_upsert_pair_results(conn, results, "test-model")
    screened = db.get_screened_pair_keys(conn)
    assert ("A", "Z") in screened  # stored in sorted order


def test_bulk_upsert_auto_confirm_high(conn):
    db.upsert_tickers(conn, [_make_market(ticker="A"), _make_market(ticker="B"),
                             _make_market(ticker="C"), _make_market(ticker="D")])
    db.bulk_upsert_pair_results(conn, [
        {"ticker_a": "A", "ticker_b": "B", "antecedent_ticker": "A",
         "consequent_ticker": "B", "confidence": "high", "reasoning": "rule"},
        {"ticker_a": "C", "ticker_b": "D", "confidence": "none", "reasoning": "rule"},
    ], "rule-screener-v1", auto_confirm_high=True)
    rows = {(r["ticker_a"], r["ticker_b"]): r for r in conn.execute(
        "SELECT ticker_a, ticker_b, human_review, reviewed_at FROM candidate_pairs"
    ).fetchall()}
    assert rows[("A", "B")]["human_review"] == "confirmed"
    assert rows[("A", "B")]["reviewed_at"] is not None
    assert rows[("C", "D")]["human_review"] is None
    assert rows[("C", "D")]["reviewed_at"] is None


def test_bulk_upsert_auto_confirm_preserves_existing_review(conn):
    pair_id = _seed_pair(conn, "A", "B", "high", "rejected")
    db.bulk_upsert_pair_results(conn, [{
        "ticker_a": "A", "ticker_b": "B", "antecedent_ticker": "A",
        "consequent_ticker": "B", "confidence": "high", "reasoning": "rule",
    }], "rule-screener-v1", auto_confirm_high=True)
    row = conn.execute(
        "SELECT human_review FROM candidate_pairs WHERE id = ?", (pair_id,)
    ).fetchone()
    assert row["human_review"] == "rejected"


def test_bulk_upsert_code_version(conn):
    db.upsert_tickers(conn, [_make_market(ticker="A"), _make_market(ticker="B")])
    result = [{"ticker_a": "A", "ticker_b": "B", "confidence": "none"}]
    db.bulk_upsert_pair_results(conn, result, "test-model", code_version="abc1234")
    row = conn.execute("SELECT code_version FROM candidate_pairs").fetchone()
    assert row["code_version"] == "abc1234"
    # Re-screen updates it; omitting code_version stores NULL
    db.bulk_upsert_pair_results(conn, result, "test-model")
    row = conn.execute("SELECT code_version FROM candidate_pairs").fetchone()
    assert row["code_version"] is None


def test_bulk_upsert_without_auto_confirm_leaves_unreviewed(conn):
    db.upsert_tickers(conn, [_make_market(ticker="A"), _make_market(ticker="B")])
    db.bulk_upsert_pair_results(conn, [{
        "ticker_a": "A", "ticker_b": "B", "antecedent_ticker": "A",
        "consequent_ticker": "B", "confidence": "high", "reasoning": "llm",
    }], "test-model")
    row = conn.execute("SELECT human_review FROM candidate_pairs").fetchone()
    assert row["human_review"] is None


# ── get_pairs_for_review ─────────────────────────────────────────────────────


def _seed_pair(conn, ticker_a="A", ticker_b="B", confidence="high", human_review=None):
    """Insert tickers and a candidate pair, return pair id."""
    db.upsert_tickers(conn, [
        _make_market(ticker=ticker_a, series_ticker="S1"),
        _make_market(ticker=ticker_b, series_ticker="S2"),
    ])
    db.bulk_upsert_pair_results(conn, [{
        "ticker_a": ticker_a,
        "ticker_b": ticker_b,
        "antecedent_ticker": ticker_a,
        "consequent_ticker": ticker_b,
        "confidence": confidence,
        "reasoning": "test",
    }], "test-model")
    if human_review:
        pair_id = conn.execute(
            "SELECT id FROM candidate_pairs ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        db.set_review(conn, pair_id, human_review)
    return conn.execute(
        "SELECT id FROM candidate_pairs ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]


def test_get_pairs_unreviewed(conn):
    _seed_pair(conn, "A", "B", "high")
    pairs = db.get_pairs_for_review(conn, "unreviewed")
    assert len(pairs) == 1


def test_get_pairs_confirmed(conn):
    _seed_pair(conn, "A", "B", "high", "confirmed")
    assert len(db.get_pairs_for_review(conn, "confirmed")) == 1
    assert len(db.get_pairs_for_review(conn, "unreviewed")) == 0


def test_get_pairs_rejected(conn):
    _seed_pair(conn, "A", "B", "high", "rejected")
    assert len(db.get_pairs_for_review(conn, "rejected")) == 1


def test_get_pairs_yield_includes_est_fees(conn):
    exp = _iso(30)
    db.upsert_tickers(conn, [
        _make_market(ticker="ANT", series_ticker="S1", no_ask_dollars="0.988",
                     expected_expiration_time=exp),
        _make_market(ticker="CON", series_ticker="S2", yes_ask_dollars="0.01",
                     expected_expiration_time=exp),
    ])
    db.bulk_upsert_pair_results(conn, [{
        "ticker_a": "ANT", "ticker_b": "CON",
        "antecedent_ticker": "ANT", "consequent_ticker": "CON",
        "confidence": "high", "reasoning": "test",
    }], "test-model")
    pair = db.get_pairs_for_review(conn, "unreviewed")[0]
    assert pair["arb_cost"] == 0.998
    # amortized fee: 0.07 * [0.988*0.012 + 0.01*0.99] per pair
    fees = 0.07 * (0.988 * 0.012 + 0.01 * 0.99)
    assert pair["est_fees"] == pytest.approx(fees, abs=1e-4)
    days = pair["days_to_maturity"]
    fee_free = (1.0 / 0.998) ** (365.0 / days) - 1.0
    expected = (1.0 / (0.998 + fees)) ** (365.0 / days) - 1.0
    assert pair["annualized_yield"] == pytest.approx(expected, rel=1e-6)
    assert pair["annualized_yield"] < fee_free


# ── exclude_expired ──────────────────────────────────────────────────────────


def _iso(days_from_now):
    from datetime import datetime, timedelta, timezone
    dt = datetime.now(timezone.utc) + timedelta(days=days_from_now)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_pair_with_expiration(conn, ticker_a, ticker_b, expiration,
                               confidence="high", human_review=None,
                               model="test-model", con_expiration=None):
    """Seed a pair whose antecedent ticker has the given expiration."""
    db.upsert_tickers(conn, [
        _make_market(ticker=ticker_a, series_ticker="S1",
                     expected_expiration_time=expiration),
        _make_market(ticker=ticker_b, series_ticker="S2",
                     expected_expiration_time=con_expiration or _iso(365)),
    ])
    db.bulk_upsert_pair_results(conn, [{
        "ticker_a": ticker_a, "ticker_b": ticker_b,
        "antecedent_ticker": ticker_a, "consequent_ticker": ticker_b,
        "confidence": confidence, "reasoning": "test",
    }], model)
    pair_id = conn.execute(
        "SELECT id FROM candidate_pairs ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    if human_review:
        db.set_review(conn, pair_id, human_review)
    return pair_id


def test_exclude_expired_drops_past_antecedent(conn):
    _seed_pair_with_expiration(conn, "OLD-A", "OLD-B", _iso(-5))
    _seed_pair_with_expiration(conn, "NEW-A", "NEW-B", _iso(30))
    live = db.get_pairs_for_review(conn, "unreviewed", exclude_expired=True)
    assert [p["ticker_a"] for p in live] == [db._sorted_pair("NEW-A", "NEW-B")[0]]
    # Default keeps both
    assert len(db.get_pairs_for_review(conn, "unreviewed")) == 2


def test_exclude_expired_applies_to_confirmed_and_high(conn):
    _seed_pair_with_expiration(conn, "C-A", "C-B", _iso(-5), human_review="confirmed")
    assert db.get_pairs_for_review(conn, "confirmed", exclude_expired=True) == []
    assert len(db.get_pairs_for_review(conn, "confirmed")) == 1

    _seed_pair_with_expiration(conn, "H-A", "H-B", _iso(-5), confidence="high")
    assert db.get_pairs_for_review(conn, "high_unreviewed", exclude_expired=True) == []


def test_exclude_expired_keeps_unknown_expiration(conn):
    _seed_pair_with_expiration(conn, "E-A", "E-B", "")
    assert len(db.get_pairs_for_review(conn, "unreviewed", exclude_expired=True)) == 1


def test_exclude_expired_drops_inactive_leg(conn):
    """A market that vanished from Kalshi is not tradeable, whatever its
    expiration says. deactivate_missing_tickers marks it is_active = 0."""
    _seed_pair_with_expiration(conn, "GONE", "LIVE", _iso(30))
    assert len(db.get_pairs_for_review(conn, "unreviewed", exclude_expired=True)) == 1
    db.deactivate_missing_tickers(conn, {"LIVE"})
    assert db.get_pairs_for_review(conn, "unreviewed", exclude_expired=True) == []


def test_exclude_expired_covers_none_confidence_pairs(conn):
    """A "none" pair is filtered on its own two legs.

    bulk_upsert_pair_results defaults a missing antecedent to ticker_a, so
    these rows are not actually NULL-legged — this pins that the liveness
    filter keeps working if that defaulting ever changes."""
    db.upsert_tickers(conn, [
        _make_market(ticker="EXP", series_ticker="S1",
                     expected_expiration_time=_iso(-1)),
        _make_market(ticker="OK", series_ticker="S2",
                     expected_expiration_time=_iso(365)),
    ])
    db.bulk_upsert_pair_results(conn, [{
        "ticker_a": "EXP", "ticker_b": "OK",
        "antecedent_ticker": None, "consequent_ticker": None,
        "confidence": "none", "reasoning": "no implication",
    }], "test-model")
    assert len(db.get_pairs_for_review(conn, "unreviewed")) == 1
    assert db.get_pairs_for_review(conn, "unreviewed", exclude_expired=True) == []


def test_exclude_expired_drops_past_consequent(conn):
    # Antecedent still live, consequent market already settled -> pair is dead
    _seed_pair_with_expiration(conn, "X-A", "X-B", _iso(30), con_expiration=_iso(-5))
    assert db.get_pairs_for_review(conn, "unreviewed", exclude_expired=True) == []
    # Default keeps it
    assert len(db.get_pairs_for_review(conn, "unreviewed")) == 1


# ── set_review + reverse_and_confirm ─────────────────────────────────────────


def test_set_review(conn):
    pair_id = _seed_pair(conn, "A", "B", "high")
    db.set_review(conn, pair_id, "confirmed")
    row = conn.execute("SELECT human_review FROM candidate_pairs WHERE id = ?", (pair_id,)).fetchone()
    assert row["human_review"] == "confirmed"


def test_set_review_invalid(conn):
    pair_id = _seed_pair(conn, "A", "B", "high")
    with pytest.raises(ValueError):
        db.set_review(conn, pair_id, "maybe")


def test_reverse_and_confirm(conn):
    pair_id = _seed_pair(conn, "A", "B", "high")
    db.reverse_and_confirm(conn, pair_id)
    row = conn.execute(
        "SELECT antecedent_ticker, consequent_ticker, human_review FROM candidate_pairs WHERE id = ?",
        (pair_id,),
    ).fetchone()
    assert row["antecedent_ticker"] == "B"
    assert row["consequent_ticker"] == "A"
    assert row["human_review"] == "confirmed"


# ── get_pair_stats ───────────────────────────────────────────────────────────


def test_get_pair_stats(conn):
    _seed_pair_with_expiration(conn, "A", "B", _iso(30), confidence="high")
    _seed_pair_with_expiration(conn, "C", "D", _iso(30), confidence="none")
    _seed_pair_with_expiration(conn, "E", "F", _iso(30), confidence="high",
                               human_review="confirmed")
    stats = db.get_pair_stats(conn)
    assert stats["queue"] == 1  # 'none' and confirmed excluded
    assert stats["confirmed_live"] == 1


def test_get_hot_pair_ids_uses_latest_evaluation(conn):
    pid = _seed_pair_with_expiration(conn, "A", "B", _iso(30), human_review="confirmed")
    # Older eval near parity, latest eval far from parity -> not hot
    db.insert_trade_evaluation(conn, {"pair_id": pid, "recommendation": "pass", "tob_cost": 1.01})
    conn.execute("UPDATE trade_evaluations SET evaluated_at = '2020-01-01T00:00:00Z'")
    conn.commit()
    db.insert_trade_evaluation(conn, {"pair_id": pid, "recommendation": "pass", "tob_cost": 1.20})
    assert db.get_hot_pair_ids(conn, 1.03) == set()
    # Latest eval near parity -> hot
    db.insert_trade_evaluation(conn, {"pair_id": pid, "recommendation": "pass", "tob_cost": 1.005})
    assert db.get_hot_pair_ids(conn, 1.03) == {pid}


def test_get_hot_pair_ids_ignores_null_tob(conn):
    pid = _seed_pair_with_expiration(conn, "A", "B", _iso(30), human_review="confirmed")
    db.insert_trade_evaluation(conn, {"pair_id": pid, "recommendation": "pass"})
    assert db.get_hot_pair_ids(conn, 1.03) == set()


def test_get_pair_stats_excludes_expired(conn):
    _seed_pair_with_expiration(conn, "A", "B", _iso(-5), confidence="high")
    _seed_pair_with_expiration(conn, "C", "D", _iso(30), confidence="high")
    _seed_pair_with_expiration(conn, "E", "F", _iso(-5), confidence="high",
                               human_review="confirmed")
    stats = db.get_pair_stats(conn)
    assert stats["queue"] == 1  # matches the review queue
    assert stats["confirmed_live"] == 0
    assert stats["expired"] == 2  # any status counts


def test_get_pair_stats_expired_consequent_counts_as_expired(conn):
    _seed_pair_with_expiration(conn, "A", "B", _iso(30), con_expiration=_iso(-5),
                               human_review="confirmed")
    stats = db.get_pair_stats(conn)
    assert stats["confirmed_live"] == 0
    assert stats["expired"] == 1


def test_get_pair_stats_rejected_breakdown(conn):
    _seed_pair_with_expiration(conn, "A", "B", _iso(30), confidence="none",
                               model="rule-screener-v1")
    _seed_pair_with_expiration(conn, "C", "D", _iso(30), confidence="none")
    _seed_pair_with_expiration(conn, "E", "F", _iso(30), confidence="high",
                               human_review="rejected")
    # Expired 'none' pairs count only toward expired
    _seed_pair_with_expiration(conn, "G", "H", _iso(-5), confidence="none",
                               model="rule-screener-v1")
    stats = db.get_pair_stats(conn)
    assert stats["rules_rejected"] == 1
    assert stats["llm_rejected"] == 1
    assert stats["review_rejected"] == 1
    assert stats["expired"] == 1


def test_get_pair_stats_human_rejection_wins_over_none(conn):
    # A human-rejected 'none' pair counts as review_rejected, not llm_rejected
    _seed_pair_with_expiration(conn, "A", "B", _iso(30), confidence="none",
                               human_review="rejected")
    stats = db.get_pair_stats(conn)
    assert stats["review_rejected"] == 1
    assert stats["llm_rejected"] == 0


# ── settings ─────────────────────────────────────────────────────────────────


def test_get_set_setting(conn):
    db.set_setting(conn, "foo", "bar")
    assert db.get_setting(conn, "foo") == "bar"


def test_get_setting_default(conn):
    assert db.get_setting(conn, "nonexistent", "fallback") == "fallback"


def test_settings_default_values(conn):
    # get_connection seeds buffer_bps and borrow_rate_bps
    assert db.get_setting(conn, "buffer_bps") == "50"
    assert db.get_setting(conn, "borrow_rate_bps") == "400"


# ── treasury yields ──────────────────────────────────────────────────────────


def test_upsert_treasury_yields(conn):
    rows = [{"date": "2026-03-15", "m1": 4.0, "m3": 4.2, "y1": 4.5}]
    count = db.upsert_treasury_yields(conn, rows)
    assert count == 1
    latest = db.get_latest_yields(conn)
    assert latest["date"] == "2026-03-15"
    assert latest["m1"] == 4.0


def test_upsert_treasury_yields_update(conn):
    db.upsert_treasury_yields(conn, [{"date": "2026-03-15", "m1": 4.0}])
    db.upsert_treasury_yields(conn, [{"date": "2026-03-15", "m1": 4.5}])
    latest = db.get_latest_yields(conn)
    assert latest["m1"] == 4.5


# ── interpolate_treasury_rate ────────────────────────────────────────────────


def test_interpolate_exact_tenor():
    yields = {"m3": 4.5}
    assert db.interpolate_treasury_rate(yields, 91) == 4.5


def test_interpolate_between_tenors():
    yields = {"m3": 4.0, "m6": 5.0}
    # 91 days (m3) to 182 days (m6), midpoint at ~136 days
    rate = db.interpolate_treasury_rate(yields, 136)
    assert rate is not None
    assert 4.0 < rate < 5.0


def test_interpolate_clamp_below():
    yields = {"m3": 4.5, "y1": 5.0}
    assert db.interpolate_treasury_rate(yields, 10) == 4.5


def test_interpolate_clamp_above():
    yields = {"m3": 4.5, "y1": 5.0}
    assert db.interpolate_treasury_rate(yields, 9999) == 5.0


def test_interpolate_none_yields():
    assert db.interpolate_treasury_rate(None, 90) is None


def test_interpolate_zero_days():
    assert db.interpolate_treasury_rate({"m3": 4.5}, 0) is None


def test_interpolate_no_data():
    assert db.interpolate_treasury_rate({}, 90) is None


# ── compute_hurdle_yield ─────────────────────────────────────────────────────


def test_compute_hurdle_yield(conn):
    # With default settings: buffer=50bps, borrow=400bps (4%)
    # No treasury data -> falls back to borrow rate
    hurdle = db.compute_hurdle_yield(conn, 90)
    assert hurdle == 0.04  # borrow_rate_bps=400 -> 4%


def test_compute_hurdle_yield_with_treasury(conn):
    db.upsert_treasury_yields(conn, [{"date": "2026-03-15", "m3": 4.0}])
    # treasury_rate=4.0% -> 0.04 + buffer(0.005) = 0.045
    # max(0.045, 0.04) = 0.045
    hurdle = db.compute_hurdle_yield(conn, 91)
    assert hurdle == 0.045


def test_compute_hurdle_yield_none_days(conn):
    assert db.compute_hurdle_yield(conn, None) is None


def test_compute_hurdle_yield_zero_days(conn):
    assert db.compute_hurdle_yield(conn, 0) is None


# ── get_signature_verdicts ───────────────────────────────────────────────────


def _mk_se(t, s, e):
    return _make_market(ticker=t, series_ticker=s, event_ticker=e)


def test_get_signature_verdicts_unanimous(conn):
    db.upsert_tickers(conn, [
        _mk_se("FO-X", "FO", "FO-26"), _mk_se("GS-X", "GS", "GS-26"),
        _mk_se("A-X", "A", "A-26"), _mk_se("B-X", "B", "B-26"),
        _mk_se("A-Y", "A", "A-26"), _mk_se("B-Y", "B", "B-26"),
        _mk_se("C-X", "C", "C-26"), _mk_se("D-X", "D", "D-26"),
    ])
    db.bulk_upsert_pair_results(conn, [
        {"ticker_a": "FO-X", "ticker_b": "GS-X", "antecedent_ticker": "FO-X",
         "consequent_ticker": "GS-X", "confidence": "high", "reasoning": "r1"},
        # conflicting signature: one high, one none — must be omitted
        {"ticker_a": "A-X", "ticker_b": "B-X", "antecedent_ticker": "A-X",
         "consequent_ticker": "B-X", "confidence": "high", "reasoning": "r2"},
        {"ticker_a": "A-Y", "ticker_b": "B-Y", "confidence": "none", "reasoning": "r3"},
        # need_more_info: never reused
        {"ticker_a": "C-X", "ticker_b": "D-X", "confidence": "need_more_info",
         "reasoning": "r4"},
    ], "test-model")
    v = db.get_signature_verdicts(conn)
    key = tuple(sorted([("FO", "FO-26"), ("GS", "GS-26")]))
    assert v[key]["confidence"] == "high"
    assert v[key]["antecedent_se"] == ("FO", "FO-26")
    assert v[key]["llm_model"] == "test-model"
    assert not v[key]["confirmed"]
    assert tuple(sorted([("A", "A-26"), ("B", "B-26")])) not in v
    assert tuple(sorted([("C", "C-26"), ("D", "D-26")])) not in v


def test_get_signature_verdicts_human_review(conn):
    db.upsert_tickers(conn, [
        _mk_se("FO-X", "FO", "FO-26"), _mk_se("GS-X", "GS", "GS-26"),
        _mk_se("P-X", "P", "P-26"), _mk_se("Q-X", "Q", "Q-26"),
    ])
    db.bulk_upsert_pair_results(conn, [
        {"ticker_a": "FO-X", "ticker_b": "GS-X", "antecedent_ticker": "FO-X",
         "consequent_ticker": "GS-X", "confidence": "high", "reasoning": "ok"},
        {"ticker_a": "P-X", "ticker_b": "Q-X", "antecedent_ticker": "P-X",
         "consequent_ticker": "Q-X", "confidence": "high", "reasoning": "wrong"},
    ], "test-model")
    ids = {(r["ticker_a"], r["ticker_b"]): r["id"] for r in
           conn.execute("SELECT id, ticker_a, ticker_b FROM candidate_pairs")}
    db.set_review(conn, ids[("FO-X", "GS-X")], "confirmed")
    db.set_review(conn, ids[("P-X", "Q-X")], "rejected")
    v = db.get_signature_verdicts(conn)
    fo = v[tuple(sorted([("FO", "FO-26"), ("GS", "GS-26")]))]
    assert fo["confidence"] == "high" and fo["confirmed"]
    pq = v[tuple(sorted([("P", "P-26"), ("Q", "Q-26")]))]
    # human rejection outranks the stored high verdict
    assert pq["confidence"] == "none" and pq["antecedent_se"] is None
