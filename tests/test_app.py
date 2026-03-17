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
