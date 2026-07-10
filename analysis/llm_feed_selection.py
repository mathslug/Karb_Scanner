#!/usr/bin/env python3
"""Reproduce the stats behind analysis/llm_feed_selection.md.

Usage:
    bash scripts/pull_prod.sh          # fresh prod DB first (optional)
    uv run analysis/llm_feed_selection.py [--db slonk_arb.db]

Everything reads from the local SQLite DB; no API calls, no LLM calls.
"""

import argparse
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, ".")
import db


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="slonk_arb.db")
    args = ap.parse_args()
    conn = db.get_connection(args.db)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prices_ticker_at ON prices(ticker, recorded_at)")
    conn.commit()

    print("== 1. Did arbitrage ever exist? (trade_evaluations: walked book incl. fees) ==")
    r = q(conn, """SELECT COUNT(DISTINCT pair_id), COUNT(*),
                   MIN(cost_per_pair), MIN(tob_cost) FROM trade_evaluations""")[0]
    print(f"pairs={r[0]} evals={r[1]} min_walked_cost={r[2]} min_tob_cost={r[3]}")

    print("\n== 2. Per-pair minimum top-of-book cost distribution ==")
    for r in q(conn, """
      WITH m AS (SELECT pair_id, MIN(tob_cost) mt FROM trade_evaluations
                 WHERE tob_cost IS NOT NULL GROUP BY pair_id)
      SELECT CASE WHEN mt < 1.0 THEN '<1.00' WHEN mt < 1.02 THEN '1.00-1.02'
                  WHEN mt < 1.05 THEN '1.02-1.05' WHEN mt < 1.10 THEN '1.05-1.10'
                  WHEN mt < 1.5 THEN '1.10-1.50' ELSE '>=1.50' END, COUNT(*)
      FROM m GROUP BY 1 ORDER BY MIN(mt)"""):
        print(f"  {r[0]:10} {r[1]:>5}")

    print("\n== 3. Reconstructed cost history (prices table, same-timestamp joins) ==")
    rows = q(conn, """
    WITH pairs AS (
      SELECT cp.id, cp.antecedent_ticker ant, cp.consequent_ticker con,
             CAST(julianday(replace(tb.expected_expiration_time,'Z','')) -
                  julianday(replace(ta.expected_expiration_time,'Z','')) AS INT) gap_days
      FROM candidate_pairs cp
      JOIN tickers ta ON ta.ticker = cp.antecedent_ticker
      JOIN tickers tb ON tb.ticker = cp.consequent_ticker
      WHERE cp.confidence IN ('high','medium') OR cp.human_review = 'confirmed')
    SELECT p.gap_days, COUNT(*) n_obs,
           MIN(CAST(p1.no_ask AS REAL) + CAST(p2.yes_ask AS REAL)) min_cost,
           SUM(CAST(p1.no_ask AS REAL) + CAST(p2.yes_ask AS REAL) < 1.0) n_sub1
    FROM pairs p
    JOIN prices p1 ON p1.ticker = p.ant AND p1.no_ask IS NOT NULL AND p1.no_ask != ''
    JOIN prices p2 ON p2.ticker = p.con AND p2.recorded_at = p1.recorded_at
                 AND p2.yes_ask IS NOT NULL AND p2.yes_ask != ''
    GROUP BY p.id""")
    print(f"pairs with joint observations: {len(rows)}")
    print(f"pairs ever sub-$1.00: {sum(1 for r in rows if r['n_sub1'])}")

    def bucket(g):
        if g is None: return "unknown"
        if g <= 1: return "same-settlement (<=1d)"
        if g <= 30: return "short gap (2-30d)"
        return "long gap (>30d)"
    b = defaultdict(list)
    for r in rows:
        if r["min_cost"] is not None and r["n_obs"] >= 5:
            b[bucket(r["gap_days"])].append(r["min_cost"])
    print("\n== 4. Pricing efficiency by settlement gap (per-pair MIN cost) ==")
    for k in ("same-settlement (<=1d)", "short gap (2-30d)", "long gap (>30d)"):
        v = b.get(k, [])
        if v:
            print(f"  {k:24} n={len(v):>4} min={min(v):.3f} "
                  f"median={statistics.median(v):.3f}")

    print("\n== 5. Screening volume per week by sport (LLM cost driver) ==")
    for r in q(conn, """
      SELECT strftime('%Y-%W', cp.screened_at) wk, COALESCE(ta.sub_sport,'?') s, COUNT(*)
      FROM candidate_pairs cp JOIN tickers ta ON ta.ticker = cp.ticker_a
      GROUP BY 1,2 HAVING COUNT(*) > 20 ORDER BY 1 DESC LIMIT 12"""):
        print(f"  {r[0]}  {r[1]:16} {r[2]:>5}")

    print("\n== 6. Screening yield by series family ==")
    for r in q(conn, """
      SELECT ta.series_ticker, tb.series_ticker, COUNT(*),
             SUM(cp.confidence='high'), SUM(cp.confidence='none')
      FROM candidate_pairs cp
      JOIN tickers ta ON ta.ticker = cp.ticker_a
      JOIN tickers tb ON tb.ticker = cp.ticker_b
      GROUP BY 1,2 ORDER BY COUNT(*) DESC LIMIT 14"""):
        print(f"  {r[0][:20]:20} x {r[1][:20]:20} n={r[2]:>4} high={r[3]:>4} none={r[4]:>4}")

    print("\n== 7. Gap distribution of ALL screened pairs (pre-LLM filter potential) ==")
    for r in q(conn, """
      SELECT CASE
        WHEN ta.expected_expiration_time = '' OR tb.expected_expiration_time = '' THEN 'unknown'
        WHEN ABS(julianday(replace(tb.expected_expiration_time,'Z','')) -
                 julianday(replace(ta.expected_expiration_time,'Z',''))) <= 1 THEN '<=1d'
        WHEN ABS(julianday(replace(tb.expected_expiration_time,'Z','')) -
                 julianday(replace(ta.expected_expiration_time,'Z',''))) <= 7 THEN '2-7d'
        ELSE '>7d' END g, COUNT(*),
        SUM(cp.confidence='high')
      FROM candidate_pairs cp
      JOIN tickers ta ON ta.ticker = cp.ticker_a
      JOIN tickers tb ON tb.ticker = cp.ticker_b
      GROUP BY 1"""):
        print(f"  gap {r[0]:8} pairs={r[1]:>5} high={r[2]:>5}")

    conn.close()


if __name__ == "__main__":
    main()
