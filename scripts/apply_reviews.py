#!/usr/bin/env python3
"""Review the live high-confidence queue by structural rule.

    uv run scripts/apply_reviews.py [--db PATH] [--apply]

Without --apply, prints the plan and changes nothing.

Each rule below is a nesting relation that holds by the rules of the
competition. A pair is only decided when its two legs are in the SAME
competition instance — the tournament or season token must match — because the
same structure across two tournaments implies nothing. Anything not matched by
a rule is left for a human.
"""
import argparse
import re
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import db as db_mod  # noqa: E402

# Depth within one knockout draw. Winning the tournament is deepest; each entry
# implies every shallower one for the same player in the same tournament.
_DEPTH = {"win": 6, "FIN": 5, "SEMI": 4, "QUAR": 3, "match": 1, "compete": 0}


def _tournament(rules: str) -> str | None:
    """The competition instance both legs must share."""
    m = re.search(r"(20\d\d)[- ](\d\d)?\s*(NHL|Stanley Cup)", rules, re.I)
    if m:
        return f"nhl-{m.group(1)}-{m.group(2)}"
    m = re.search(r"(20\d\d)\s+(US Open|French Open|Wimbledon|Australian Open)"
                  r"\s*(Men|Women)?", rules, re.I)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{(m.group(3) or '')}".lower()
    return None


def _depth(series: str, event: str, rules: str) -> int | None:
    """How far through the draw this market's YES condition reaches."""
    if "COMPETE" in series:
        return _DEPTH["compete"]
    if "MATCH" in series or "SETWINNER" in series:
        return _DEPTH["match"]
    if "ADVANCE" in series or "NATSTAGE" in series:
        # From the rules text, not the event ticker: the round is glued to the
        # tournament there (KXATPADVANCE-26USOSEMI), so a delimiter-anchored
        # match on the ticker finds nothing.
        for word, tag in (("final", "FIN"), ("semifinal", "SEMI"),
                          ("quarterfinal", "QUAR")):
            if re.search(rf"(?:qualifies for|reach)(?:es)?\s+the\s+{word}s?\b",
                         rules, re.I):
                return _DEPTH[tag]
        return None
    if "PLAYOFF" in series:
        return _DEPTH["compete"]          # making the playoffs = being in the draw
    # "wins the Stanley Cup Finals" but "win the Eastern Conference Finals" —
    # Kalshi's subject-verb agreement varies with the entity name.
    if re.search(r"\bwins?\b", rules, re.I):
        return _DEPTH["win"]              # wins the tournament / conference / cup
    return None


def decide(a: dict, b: dict, antecedent: str) -> tuple[str, str] | None:
    """(decision, why), or None to leave the pair alone."""
    ta, tb = _tournament(a["rules_primary"]), _tournament(b["rules_primary"])
    if not ta or not tb or ta != tb:
        return None
    da = _depth(a["series_ticker"], a["event_ticker"], a["rules_primary"])
    dbp = _depth(b["series_ticker"], b["event_ticker"], b["rules_primary"])
    if da is None or dbp is None or da == dbp:
        return None
    deeper, shallower = (a, b) if da > dbp else (b, a)
    if antecedent != deeper["ticker"]:
        # The stored direction points the wrong way: the shallower condition
        # does not imply the deeper one.
        return ("rejected", f"direction reversed: {shallower['ticker']} does not "
                            f"imply {deeper['ticker']}")
    return ("confirmed", f"{deeper['ticker']} reaches further in the same draw "
                         f"than {shallower['ticker']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="slonk_arb.db")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = db_mod.get_connection(args.db)
    pairs = db_mod.get_pairs_for_review(conn, "high_unreviewed", exclude_expired=True)
    info = lambda t: dict(conn.execute(
        "select ticker, series_ticker, event_ticker, rules_primary "
        "from tickers where ticker=?", (t,)).fetchone())

    counts = {"confirmed": 0, "rejected": 0, "left": 0}
    for p in pairs:
        a, b = info(p["ticker_a"]), info(p["ticker_b"])
        d = decide(a, b, p["antecedent_ticker"])
        if d is None:
            counts["left"] += 1
            print(f"  LEAVE   [{p['id']}] {a['ticker']} x {b['ticker']}")
            continue
        decision, why = d
        counts[decision] += 1
        print(f"  {decision.upper():8s}[{p['id']}] {why}")
        if args.apply:
            db_mod.set_review(conn, p["id"], decision)

    print(f"\n  confirmed={counts['confirmed']}  rejected={counts['rejected']}  "
          f"left={counts['left']}  (of {len(pairs)})")
    print("  dry run — pass --apply to write" if not args.apply else "  written")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
