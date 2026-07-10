"""Tests for app.py — Flask route smoke tests."""

import os

import pytest

import db as db_mod
from app import create_app


@pytest.fixture
def client():
    # Ensure no admin password so auth-required routes return 403/401 as expected
    os.environ.pop("SLONK_ADMIN_PASSWORD", None)
    import app as app_mod
    app_mod.ADMIN_PASSWORD = ""

    application = create_app(":memory:")
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.test_client() as c:
        yield c


@pytest.fixture
def authed_client():
    os.environ["SLONK_ADMIN_PASSWORD"] = "testpass"
    import app as app_mod
    app_mod.ADMIN_PASSWORD = "testpass"

    application = create_app(":memory:")
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.test_client() as c:
        yield c

    os.environ.pop("SLONK_ADMIN_PASSWORD", None)
    app_mod.ADMIN_PASSWORD = ""


# ── GET routes ───────────────────────────────────────────────────────────────


def test_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Dashboard" in resp.data


def test_review(client):
    resp = client.get("/review")
    assert resp.status_code == 200


def test_reviewed(client):
    resp = client.get("/reviewed")
    assert resp.status_code == 200


def test_trades(client):
    resp = client.get("/trades")
    assert resp.status_code == 200


def test_evaluations(client):
    resp = client.get("/evaluations")
    assert resp.status_code == 200


def test_settings(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert b"Yield Benchmark" in resp.data


def test_pair_not_found(client):
    resp = client.get("/pair/999")
    assert resp.status_code == 404


# ── Auth-required POST routes (no auth) ─────────────────────────────────────


def test_post_review_no_auth(client):
    resp = client.post("/pair/1/review", data={"decision": "confirmed"})
    # No ADMIN_PASSWORD configured -> 403
    assert resp.status_code == 403


def test_post_settings_no_auth(client):
    resp = client.post("/settings", data={"buffer_bps": "100"})
    assert resp.status_code == 403


# ── Auth-required POST routes (with auth) ───────────────────────────────────


def test_post_settings_with_auth(authed_client):
    resp = authed_client.post(
        "/settings",
        data={"buffer_bps": "100", "borrow_rate_bps": "500"},
        headers={"Authorization": "Basic dGVzdDp0ZXN0cGFzcw=="},  # test:testpass
    )
    # Should redirect to settings page
    assert resp.status_code == 302


def test_login_redirect(authed_client):
    resp = authed_client.get(
        "/login",
        headers={"Authorization": "Basic dGVzdDp0ZXN0cGFzcw=="},
    )
    assert resp.status_code == 302


def test_login_rejects_external_redirect(authed_client):
    resp = authed_client.get(
        "/login?next=https://evil.example.com/",
        headers={"Authorization": "Basic dGVzdDp0ZXN0cGFzcw=="},
    )
    assert resp.status_code == 302
    assert "evil.example.com" not in resp.headers["Location"]


def test_review_hides_expired_but_reviewed_keeps_them(tmp_path):
    """Expired pairs leave the review queue; /reviewed history still shows them."""
    from datetime import datetime, timedelta, timezone

    db_path = str(tmp_path / "test.db")
    conn = db_mod.get_connection(db_path)
    past = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db_mod.upsert_tickers(conn, [
        {"ticker": "EXP-A", "series_ticker": "S1", "event_ticker": "E1",
         "title": "Expired A", "yes_sub_title": "X", "rules_primary": "",
         "expected_expiration_time": past, "close_time": past,
         "last_price_dollars": "0.5", "yes_ask_dollars": "0.5",
         "no_ask_dollars": "0.5", "volume": 500,
         "sport_tag": "Tennis", "sub_sport": "Tennis"},
        {"ticker": "EXP-B", "series_ticker": "S2", "event_ticker": "E2",
         "title": "Expired B", "yes_sub_title": "X", "rules_primary": "",
         "expected_expiration_time": past, "close_time": past,
         "last_price_dollars": "0.5", "yes_ask_dollars": "0.5",
         "no_ask_dollars": "0.5", "volume": 500,
         "sport_tag": "Tennis", "sub_sport": "Tennis"},
    ])
    db_mod.bulk_upsert_pair_results(conn, [{
        "ticker_a": "EXP-A", "ticker_b": "EXP-B",
        "antecedent_ticker": "EXP-A", "consequent_ticker": "EXP-B",
        "confidence": "high", "reasoning": "expired pair",
    }], "test-model")

    application = create_app(db_path)
    application.config["TESTING"] = True
    with application.test_client() as c:
        assert b"EXP-A" not in c.get("/review").data

        pair_id = conn.execute("SELECT id FROM candidate_pairs LIMIT 1").fetchone()["id"]
        db_mod.set_review(conn, pair_id, "confirmed")
        assert b"EXP-A" in c.get("/reviewed").data
        assert c.get(f"/pair/{pair_id}").status_code == 200
    conn.close()


def test_post_settings_invalid_value_kept_out_of_db(authed_client):
    # Empty/garbage input must not be stored: compute_hurdle_yield() int()s
    # these settings, so a bad value would 500 every review page.
    resp = authed_client.post(
        "/settings",
        data={"buffer_bps": "", "borrow_rate_bps": "abc"},
        headers={"Authorization": "Basic dGVzdDp0ZXN0cGFzcw=="},
    )
    assert resp.status_code == 302
    resp = authed_client.get("/review")
    assert resp.status_code == 200
