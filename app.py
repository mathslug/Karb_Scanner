#!/usr/bin/env python3
"""Flask webapp for reviewing Kalshi arbitrage candidate pairs."""

import os
import sqlite3
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, url_for, Response
from flask_wtf.csrf import CSRFProtect

import db as db_mod

load_dotenv()

DB_PATH = os.environ.get("SLONK_DB") or os.environ.get("KALSHI_DB", "slonk_arb.db")
ADMIN_PASSWORD = os.environ.get("SLONK_ADMIN_PASSWORD", "")


def create_app(db_path: str = DB_PATH) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))
    CSRFProtect(app)

    # :memory: doesn't persist across connections, so tests (which use it)
    # need the every-request path. A real file only needs migrations once.
    memory_db = db_path == ":memory:"
    if not memory_db:
        db_mod.get_connection(db_path).close()

    def get_conn():
        if memory_db:
            return db_mod.get_connection(app.config["DB_PATH"])
        return db_mod.connect(app.config["DB_PATH"])

    def _check_auth():
        """Return True if the request has valid admin credentials."""
        if not ADMIN_PASSWORD:
            return False
        auth = request.authorization
        return auth and auth.password == ADMIN_PASSWORD

    def _safe_next(target):
        """Only allow same-site relative redirect targets ("//" is scheme-relative)."""
        if target and target.startswith("/") and target[1:2] not in ("/", "\\"):
            return target
        return None

    def admin_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not ADMIN_PASSWORD:
                return Response("Admin access not configured.", 403)
            if not _check_auth():
                return Response(
                    "Unauthorized", 401,
                    {"WWW-Authenticate": 'Basic realm="Admin"'},
                )
            return f(*args, **kwargs)
        return decorated

    @app.context_processor
    def inject_is_admin():
        return {"is_admin": _check_auth()}

    @app.route("/login")
    @admin_required
    def login():
        return redirect(_safe_next(request.args.get("next")) or url_for("index"))

    @app.route("/healthz")
    def healthz():
        """Liveness plus database reachability.

        Read by the container health check every 30s and by the Pi's dashboard,
        so it is deliberately cheap: one trivial query, no template, no LLM, no
        outbound network. `/` looks like the obvious probe and is not — it runs
        the full pair-stats aggregate, which would turn monitoring into steady
        load on a 700MB database.

        Opens sqlite3 directly, read-only, rather than going through
        get_conn() — independent of the request-connection pool and cannot be
        the thing that mutates the schema.
        """
        try:
            conn = sqlite3.connect(f"file:{app.config['DB_PATH']}?mode=ro", uri=True)
            conn.execute("SELECT 1").fetchone()
            conn.close()
        except Exception:
            return Response("database unavailable", 503, {"Content-Type": "text/plain"})
        return Response("ok", 200, {"Content-Type": "text/plain"})

    @app.route("/")
    def index():
        conn = get_conn()
        stats = db_mod.get_pair_stats(conn)
        conn.close()
        return render_template("base.html", page="dashboard", stats=stats)

    def _filter_by_confidence(pairs, confidence):
        if confidence and confidence in ("high", "medium", "low", "need_more_info", "none"):
            return [p for p in pairs if p.get("confidence") == confidence]
        # Default "All" excludes none-confidence pairs
        return [p for p in pairs if p.get("confidence") != "none"]

    @app.route("/review")
    def review():
        conn = get_conn()
        pairs = db_mod.get_pairs_for_review(conn, "unreviewed", exclude_expired=True)
        need_info = db_mod.get_pairs_for_review(conn, "need_more_info", exclude_expired=True)
        conn.close()
        pairs = pairs + need_info
        conf = request.args.get("confidence")
        pairs = _filter_by_confidence(pairs, conf)
        return render_template("review.html", pairs=pairs, status="unreviewed", title="Unreviewed Pairs", confidence=conf)

    @app.route("/reviewed")
    def reviewed():
        conn = get_conn()
        confirmed = db_mod.get_pairs_for_review(conn, "confirmed")
        rejected = db_mod.get_pairs_for_review(conn, "rejected")
        conn.close()
        conf = request.args.get("confidence")
        pairs = _filter_by_confidence(confirmed + rejected, conf)
        return render_template("review.html", pairs=pairs, status="reviewed", title="Reviewed Pairs", confidence=conf)

    @app.route("/pair/<int:pair_id>")
    def pair_detail(pair_id):
        conn = get_conn()
        pair = db_mod.get_pair_detail(conn, pair_id)
        conn.close()
        if not pair:
            return "Pair not found", 404
        return render_template("detail.html", pair=pair)

    @app.route("/trades")
    def trades():
        conn = get_conn()
        evals = db_mod.get_latest_evaluations(conn)
        conn.close()
        return render_template("trades.html", evals=evals)

    @app.route("/evaluations")
    def evaluations():
        conn = get_conn()
        days = request.args.get("days", 2, type=int)
        evals = db_mod.get_recent_evaluations(conn, days=days)
        conn.close()
        return render_template("evaluations.html", evals=evals, days=days)

    @app.route("/settings")
    def settings():
        conn = get_conn()
        all_settings = db_mod.get_all_settings(conn)
        latest_yields = db_mod.get_latest_yields(conn)
        conn.close()
        return render_template("settings.html", settings=all_settings, latest_yields=latest_yields)

    @app.route("/settings", methods=["POST"])
    @admin_required
    def update_settings():
        conn = get_conn()
        # compute_hurdle_yield() does int() on these; storing a non-integer
        # would 500 every review page and crash the evaluate cron.
        for key, default in (("buffer_bps", "100"), ("borrow_rate_bps", "600")):
            try:
                value = int(request.form.get(key, default))
            except (TypeError, ValueError):
                continue  # invalid input: keep the existing value
            if value >= 0:
                db_mod.set_setting(conn, key, str(value))
        conn.close()
        return redirect(url_for("settings"))

    @app.route("/pair/<int:pair_id>/review", methods=["POST"])
    @admin_required
    def submit_review(pair_id):
        decision = request.form.get("decision")
        if decision not in ("confirmed", "rejected", "reversed"):
            return "Invalid decision", 400
        conn = get_conn()
        if decision == "reversed":
            db_mod.reverse_and_confirm(conn, pair_id)
        else:
            db_mod.set_review(conn, pair_id, decision)
        conn.close()
        next_url = _safe_next(request.form.get("next")) or url_for("pair_detail", pair_id=pair_id)
        return redirect(next_url)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5001)
