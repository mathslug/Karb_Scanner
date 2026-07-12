#!/usr/bin/env python3
"""Kalshi cross-market arbitrage scanner.

Scans Kalshi sports markets to discover pairs where one contract resolving YES
logically guarantees another also resolves YES (e.g., winning the French Open
implies winning a Grand Slam). Uses programmatic pre-filtering to narrow
candidates, then Claude Sonnet for implication checking.

Results are persisted to a SQLite database for incremental scanning and human
review via the companion Flask webapp (app.py).
"""

import argparse
import functools
import json
import logging
import os
import subprocess
import sys
import time
from itertools import combinations

import anthropic
import requests
from dotenv import load_dotenv

import db as db_mod
from kalshi import KALSHI_BASE

load_dotenv()

log = logging.getLogger("scan")

@functools.cache
def code_version() -> str | None:
    """Git commit of the running code (cached), or None if git is unavailable."""
    code_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        return subprocess.run(
            # -c safe.directory: production cron runs as a different user than
            # the repo owner; without it git refuses with "dubious ownership".
            ["git", "-C", code_dir, "-c", f"safe.directory={code_dir}",
             "describe", "--always", "--dirty"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None

# Filter values that map to a different Kalshi API tag (e.g. "Pro Football" -> "Football")
_FILTER_TO_API_TAG = {
    "pro football": "Football",
    "college football": "Football",
}


# ── Fetching ─────────────────────────────────────────────────────────────────


def fetch_series(category: str, filter_tags: list[str] | None = None) -> list[dict]:
    """Fetch series for a category, optionally filtering by Kalshi API tags.

    When filter_tags is provided, makes a separate paginated API call per tag
    and merges results (deduped by series ticker).
    """
    tags_to_query = filter_tags or [None]
    seen: set[str] = set()
    series: list[dict] = []
    for tag in tags_to_query:
        cursor = None
        while True:
            params = {"limit": 200, "category": category}
            if tag:
                params["tags"] = tag
            if cursor:
                params["cursor"] = cursor
            resp = requests.get(f"{KALSHI_BASE}/series", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("series", [])
            for s in batch:
                if s["ticker"] not in seen:
                    seen.add(s["ticker"])
                    series.append(s)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
    return series


def fetch_events_with_markets(series_ticker: str) -> list[dict]:
    """Fetch events (with nested markets) for a series."""
    events = []
    cursor = None
    while True:
        params = {
            "limit": 200,
            "series_ticker": series_ticker,
            "with_nested_markets": "true",
        }
        if cursor:
            params["cursor"] = cursor
        for attempt in range(3):
            resp = requests.get(f"{KALSHI_BASE}/events", params=params, timeout=15)
            if resp.status_code == 429 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            break
        data = resp.json()
        batch = data.get("events", [])
        events.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
        time.sleep(0.2)
    return events


def fetch_and_store_markets(category: str, conn, filter_tags: list[str] | None = None) -> set[str]:
    """Fetch open markets for a category and upsert into DB incrementally.

    Upserts each series' markets immediately so we never hold all 17K+
    markets in memory at once. Returns the set of active ticker strings
    (for deactivation tracking).
    """
    print(f"Fetching {category} series...", flush=True)
    all_series = fetch_series(category, filter_tags)
    print(f"  {len(all_series)} series")

    active_tickers: set[str] = set()
    total_markets = 0
    new_total = 0
    updated_total = 0
    recorded_total = 0
    for i, s in enumerate(all_series):
        sticker = s["ticker"]
        tags = s.get("tags", [])
        sport_tag = tags[0] if tags else ""
        for attempt in range(3):
            try:
                events = fetch_events_with_markets(sticker)
                break
            except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as e:
                if attempt < 2 and ("429" in str(e) or "timed out" in str(e).lower() or "No route" in str(e)):
                    time.sleep(2 ** attempt)  # 1s, 2s backoff
                    continue
                log.warning("Failed to fetch events for %s: %s", sticker, e)
                print(f"  Warning: failed to fetch events for {sticker}: {e}")
                events = []
                break
        time.sleep(0.2)  # rate limit between series
        batch = []
        for event in events:
            # Derive sub_sport from event competition metadata when the sport
            # has meaningful sub-categories (e.g. Football -> Pro/College,
            # Hockey -> NHL/College Hockey). Other sports fall back to sport_tag.
            competition = event.get("product_metadata", {}).get("competition", "")
            if sport_tag in ("Football", "Hockey", "Basketball", "Baseball") and competition:
                sub_sport = competition
            else:
                sub_sport = sport_tag
            for m in event.get("markets", []):
                if m.get("status") not in ("open", "active"):
                    continue
                vol = int(float(m.get("volume") or m.get("volume_fp") or 0))
                batch.append({
                    "ticker": m["ticker"],
                    "series_ticker": sticker,
                    "event_ticker": event["event_ticker"],
                    "title": m.get("title", ""),
                    "yes_sub_title": m.get("yes_sub_title", ""),
                    "rules_primary": m.get("rules_primary", ""),
                    "expected_expiration_time": m.get("expected_expiration_time", ""),
                    "close_time": m.get("close_time", ""),
                    "last_price_dollars": m.get("last_price_dollars"),
                    "yes_ask_dollars": m.get("yes_ask_dollars"),
                    "no_ask_dollars": m.get("no_ask_dollars"),
                    "volume": vol,
                    "sport_tag": sport_tag,
                    "sub_sport": sub_sport,
                })
        if batch:
            new, updated = db_mod.upsert_tickers(conn, batch)
            recorded = db_mod.record_prices(conn, batch)
            new_total += new
            updated_total += updated
            recorded_total += recorded
            active_tickers.update(m["ticker"] for m in batch)
            total_markets += len(batch)
        if (i + 1) % 10 == 0 or i + 1 == len(all_series):
            print(f"  Processed {i + 1}/{len(all_series)} series ({total_markets} markets)", flush=True)

    print(f"  Total open markets: {total_markets}")
    print(f"  DB: {new_total} new tickers, {updated_total} updated, {recorded_total} price snapshots")
    return active_tickers


# ── Pre-filtering ────────────────────────────────────────────────────────────


ENTITY_BLOCKLIST = {
    "Tie", "Yes",
    "Before 2025", "Before 2026", "Before 2027", "Before 2028",
    "Before 2029", "Before 2030", "Before 2031", "Before 2032",
    "Before 2033", "Before 2034", "Before 2035",
}


def filter_groups_by_sport(
    groups: dict[str, list[dict]], filter_tags: list[str],
) -> dict[str, list[dict]]:
    """Filter entity groups to only keep markets matching the given sport tags.

    Filters at the market level: only markets whose sub_sport or sport_tag
    matches are retained. Entities with no matching markets are dropped.
    """
    filter_lower = [t.strip().lower() for t in filter_tags]
    filtered = {}
    for entity, markets in groups.items():
        matching = [
            m for m in markets
            if not m.get("sub_sport") and not m.get("sport_tag")
            or m.get("sub_sport", "").lower() in filter_lower
            or m.get("sport_tag", "").lower() in filter_lower
        ]
        if matching:
            filtered[entity] = matching
    return filtered


def generate_candidate_pairs(groups: dict[str, list[dict]]) -> list[tuple[dict, dict]]:
    """Generate candidate pairs for each entity.

    Pairs all markets within an entity. Skips pairs where both markets have
    a known sport and the sports differ (cross-sport noise). Also skips
    entities in the ENTITY_BLOCKLIST and purely numeric entities (e.g. game
    totals thresholds like "6"), which pair independent games, never real
    implications.
    """
    pairs = []
    filtered_count = 0
    blocklist_count = 0
    for entity, entity_markets in groups.items():
        if entity in ENTITY_BLOCKLIST or entity.replace(".", "").isdigit():
            blocklist_count += len(list(combinations(entity_markets, 2)))
            continue
        for a, b in combinations(entity_markets, 2):
            sub_a = a.get("sub_sport") or a.get("sport_tag") or None
            sub_b = b.get("sub_sport") or b.get("sport_tag") or None
            if sub_a and sub_b and sub_a != sub_b:
                filtered_count += 1
                continue
            pairs.append((a, b))
    if blocklist_count:
        log.info("Skipped %d pairs from blocklisted entities", blocklist_count)
        print(f"  Skipped {blocklist_count} pairs from blocklisted entities")
    if filtered_count:
        log.info("Filtered %d cross-sport pairs", filtered_count)
        print(f"  Filtered {filtered_count} cross-sport pairs")
    return pairs


# ── Rule-based screening ─────────────────────────────────────────────────────
#
# Finish-position lattices are deterministic from the series tickers: for the
# same player in the same tournament, winning implies top-5 implies top-10
# implies top-20 implies making the cut. Screening these with an LLM is pure
# token waste (~85% of historical LLM volume; verified against 5,200+
# LLM-screened PGA pairs with full agreement on the encoded families).
# Anything not covered falls through to the LLM — including judgment
# families like KXPGAMAJORWIN and KXPGAR2LEAD (round-2 leader DOES imply
# making the cut, but that's cut-timing domain knowledge we leave to the LLM).

RULE_SCREENER_MODEL = "rule-screener-v1"

# Ordered narrow -> broad: entry i implies entry j (i < j) for the same
# player in the same tournament.
_LATTICES = [
    ["KXPGATOUR", "KXPGATOP5", "KXPGATOP10", "KXPGATOP20", "KXPGAMAKECUT"],
    ["KXPGAR1LEAD", "KXPGAR1TOP5", "KXPGAR1TOP10"],
    ["KXLIVTOUR", "KXLIVTOP5", "KXLIVTOP10"],
]
_LATTICE_RANK = {
    series: (fam_idx, rank)
    for fam_idx, fam in enumerate(_LATTICES)
    for rank, series in enumerate(fam)
}


def _tournament_suffix(event_ticker: str) -> str:
    """Event tickers are SERIES-TOURNAMENT (e.g. KXPGATOP5-MAST26)."""
    return event_ticker.split("-", 1)[1] if "-" in event_ticker else ""


def rule_screen_pair(a: dict, b: dict) -> dict | None:
    """Screen a pair by lattice rules. Returns a result dict (same shape as
    an LLM result) when the rules decide it, or None to defer to the LLM."""
    ra = _LATTICE_RANK.get(a["series_ticker"])
    rb = _LATTICE_RANK.get(b["series_ticker"])
    if ra is None or rb is None:
        return None
    ta, tb = _tournament_suffix(a["event_ticker"]), _tournament_suffix(b["event_ticker"])
    if not ta or not tb:
        return None
    base = {"ticker_a": a["ticker"], "ticker_b": b["ticker"]}
    if ta != tb:
        # Finish positions in different tournaments never imply each other.
        return {**base, "antecedent_ticker": None, "consequent_ticker": None,
                "confidence": "none",
                "reasoning": "rule: finish-position markets in different tournaments"}
    if ra[0] != rb[0]:
        # Same tournament, different lattice (e.g. round-1 position vs final
        # result): neither direction is a logical necessity.
        return {**base, "antecedent_ticker": None, "consequent_ticker": None,
                "confidence": "none",
                "reasoning": "rule: round-position and final-result markets don't imply each other"}
    if ra[1] == rb[1]:
        return None  # same series + tournament: shouldn't occur, defer
    ant, con = (a, b) if ra[1] < rb[1] else (b, a)
    return {**base, "antecedent_ticker": ant["ticker"], "consequent_ticker": con["ticker"],
            "confidence": "high",
            "reasoning": (f"rule: {ant['series_ticker']} is a strict subset of "
                          f"{con['series_ticker']} for the same player and tournament")}


def rule_screen_pairs(pairs: list[tuple[dict, dict]]) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """Split pairs into (rule_results, remaining_for_llm)."""
    results, remaining = [], []
    for a, b in pairs:
        r = rule_screen_pair(a, b)
        if r is not None:
            results.append(r)
        else:
            remaining.append((a, b))
    return results, remaining


# ── LLM screening ───────────────────────────────────────────────────────────

def pair_signature(a: dict, b: dict) -> tuple:
    """Structural identity of a pair: the (series, event) of both legs, sorted.

    Two pairs with the same signature ask the same logical question about
    different entities ("X wins USO -> X wins a Slam" for X = Sinner or
    Zverev), so a screening verdict transfers between them.
    """
    return tuple(sorted([(a["series_ticker"], a["event_ticker"]),
                         (b["series_ticker"], b["event_ticker"])]))


def reuse_screen_pairs(
    pairs: list[tuple[dict, dict]], sig_verdicts: dict,
) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """Split pairs into (reused_results, remaining) using stored verdicts
    for structurally identical pairs (see get_signature_verdicts).

    Reused results carry the source pair's confidence and direction (mapped
    onto this pair's tickers by series+event), plus provenance in reasoning,
    the source's llm_model, and whether the source was human-confirmed.
    """
    reused, remaining = [], []
    for a, b in pairs:
        v = sig_verdicts.get(pair_signature(a, b))
        if v is None:
            remaining.append((a, b))
            continue
        r = {
            "ticker_a": a["ticker"], "ticker_b": b["ticker"],
            "confidence": v["confidence"],
            "reasoning": f"[structural reuse of {v['src_ticker_a']}/{v['src_ticker_b']}] {v['reasoning']}",
            "llm_model": v["llm_model"], "confirmed": v["confirmed"],
        }
        if v["antecedent_se"] is not None:
            if (a["series_ticker"], a["event_ticker"]) == v["antecedent_se"]:
                r["antecedent_ticker"], r["consequent_ticker"] = a["ticker"], b["ticker"]
            else:
                r["antecedent_ticker"], r["consequent_ticker"] = b["ticker"], a["ticker"]
        reused.append(r)
    return reused, remaining


def store_reused_results(conn, reused: list[dict], cv: str | None) -> None:
    """Store reused results, preserving each source's llm_model and
    auto-confirming high verdicts whose source structure a human confirmed."""
    groups: dict[tuple, list[dict]] = {}
    for r in reused:
        groups.setdefault((r.pop("llm_model"), r.pop("confirmed")), []).append(r)
    for (model, confirmed), rs in groups.items():
        db_mod.bulk_upsert_pair_results(
            conn, rs, model, auto_confirm_high=confirmed, code_version=cv,
        )


SCREENING_PROMPT = """\
You will judge pairs of prediction-market events for LOGICAL implication.

"A implies B" means: in every possible scenario where A resolves YES, B must also resolve YES by the rules of the competition — not by probability or form. If any legal scenario makes A YES and B NO, there is no implication. Check both directions.

Calibration:
- "X wins the French Open" implies "X wins a Grand Slam this year" (the French Open is a Grand Slam). The reverse does NOT hold — X could have won a different Slam.
- "Team wins the title" implies "team wins its conference/semifinal" only when the competition format makes that stage unavoidable.
- "X leads after Round 3" does NOT imply "X finishes top 10": a final-round collapse, withdrawal, or disqualification is possible. Near-certainty is not implication.
- Events from different tournaments almost never imply each other.

Return JSON only (no markdown fencing), one result per pair, in input order:
{"results": [
  {"ticker_a": "<copied exactly from Event A>",
   "ticker_b": "<copied exactly from Event B>",
   "implication": "a_implies_b" | "b_implies_a" | "none" | "unclear",
   "confidence": "high" | "medium" | "low",
   "reasoning": "one short sentence"}
]}

Field meanings:
- "implication" is your verdict; expect "none" for most pairs. Use "unclear" only when the market rules are too ambiguous to decide.
- "confidence" grades the implication itself, never your certainty in a "none" verdict: "high" = guaranteed by the rules of the sport; "medium" = holds unless rules are interpreted unusually; "low" = plausible but shaky. For "none"/"unclear", confidence is ignored.

CANDIDATE PAIRS:
"""


def _normalize_result(r: dict) -> dict:
    """Map the prompt's verdict schema (implication + confidence) onto the
    stored schema (confidence + antecedent/consequent tickers).

    The old schema overloaded "confidence" as both verdict and certainty,
    which let the model answer "high" meaning "highly confident there is NO
    implication" — a false positive that flows straight into evaluation.
    Verdict and certainty are now separate fields.
    """
    imp = r.get("implication")
    if imp is None:
        return r  # already in stored schema
    if imp in ("a_implies_b", "b_implies_a"):
        ra, rb = r.get("ticker_a"), r.get("ticker_b")
        if imp == "a_implies_b":
            r["antecedent_ticker"], r["consequent_ticker"] = ra, rb
        else:
            r["antecedent_ticker"], r["consequent_ticker"] = rb, ra
        if r.get("confidence") not in ("high", "medium", "low"):
            r["confidence"] = "low"
    elif imp == "unclear":
        r["confidence"] = "need_more_info"
        r["antecedent_ticker"] = r["consequent_ticker"] = None
    else:  # "none" or anything unrecognized
        r["confidence"] = "none"
        r["antecedent_ticker"] = r["consequent_ticker"] = None
    return r


def format_pair_for_llm(idx: int, a: dict, b: dict) -> str:
    """Format a candidate pair for the LLM prompt."""
    return (
        f"\n--- Pair {idx} ---\n"
        f"Event A:\n"
        f"  ticker: {a['ticker']}\n"
        f"  title: {a['title']}\n"
        f"  rules: {a['rules_primary'][:500]}\n"
        f"\n"
        f"Event B:\n"
        f"  ticker: {b['ticker']}\n"
        f"  title: {b['title']}\n"
        f"  rules: {b['rules_primary'][:500]}\n"
    )


def _call_anthropic(prompt: str, model: str) -> str:
    """Call Anthropic Messages API."""
    client = anthropic.Anthropic()
    # Sonnet 5 runs adaptive thinking by default; max_tokens covers thinking
    # plus the JSON response, and the text block may not be content[0].
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
    )
    return next(b.text for b in response.content if b.type == "text")


def _call_openai_compat(prompt: str, model: str, base_url: str) -> str:
    """Call an OpenAI-compatible chat completions endpoint.

    Used for DigitalOcean dedicated inference (and anything else that speaks
    /v1/chat/completions). Auth via LLM_API_KEY as a Bearer token.
    """
    resp = requests.post(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            # Reasoning models spend completion tokens on thinking before the
            # final JSON, so leave generous headroom.
            "max_tokens": 8192,
        },
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"].get("content") or ""


def _call_llm(prompt: str, model: str) -> str:
    """Route to the configured LLM backend.

    If LLM_BASE_URL is set, use the OpenAI-compatible endpoint there;
    otherwise call the Anthropic API directly.
    """
    base_url = os.environ.get("LLM_BASE_URL")
    if base_url:
        return _call_openai_compat(prompt, model, base_url)
    return _call_anthropic(prompt, model)


def _extract_json(text: str) -> list[dict]:
    """Extract a JSON array from LLM output, stripping markdown fencing.

    Raises ValueError (which json.JSONDecodeError subclasses) if the output
    is not JSON or not a recognizable list-of-objects shape.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Some models wrap the JSON in prose; retry on the outermost {...} span.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])
    if isinstance(parsed, dict):
        for key in ("results", "pairs", "data"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            if "antecedent_ticker" in parsed or "implication" in parsed:
                parsed = [parsed]
    if not isinstance(parsed, list) or not all(isinstance(r, dict) for r in parsed):
        raise ValueError("unrecognized LLM response shape")
    return parsed


def _fanout_result(r: dict, rep: tuple[dict, dict], sib: tuple[dict, dict]) -> dict:
    """Copy a representative pair's verdict onto a structural sibling,
    mapping the antecedent side by (series, event) identity."""
    ra, rb = rep
    sa, sb = sib
    out = {
        "ticker_a": sa["ticker"], "ticker_b": sb["ticker"],
        "confidence": r.get("confidence"),
        "reasoning": f"[structural reuse of {ra['ticker']}/{rb['ticker']}] {r.get('reasoning', '')}",
    }
    ant = r.get("antecedent_ticker")
    if ant and r.get("confidence") not in ("none", "need_more_info"):
        ant_leg = ra if ra["ticker"] == ant else rb
        ant_se = (ant_leg["series_ticker"], ant_leg["event_ticker"])
        if (sa["series_ticker"], sa["event_ticker"]) == ant_se:
            out["antecedent_ticker"], out["consequent_ticker"] = sa["ticker"], sb["ticker"]
        else:
            out["antecedent_ticker"], out["consequent_ticker"] = sb["ticker"], sa["ticker"]
    return out


def screen_pairs_with_llm(
    pairs: list[tuple[dict, dict]],
    model: str,
    batch_size: int = 12,
    conn: "sqlite3.Connection | None" = None,
) -> list[dict]:
    """Screen candidate pairs using Claude for implication checking.

    Batches pairs and returns ALL results (including confidence="none") so
    they can be stored in the DB to avoid re-screening. Each result dict
    includes ticker_a/ticker_b from the input pair.

    Structurally identical pairs (same series+event on both legs, different
    entity) are deduplicated: one representative per signature goes to the
    LLM and its verdict fans out to the siblings.

    If conn is provided, writes results to the DB after each batch.
    """
    by_sig: dict[tuple, list[tuple[dict, dict]]] = {}
    for p in pairs:
        by_sig.setdefault(pair_signature(*p), []).append(p)
    reps = [group[0] for group in by_sig.values()]
    siblings = {(g[0][0]["ticker"], g[0][1]["ticker"]): g[1:] for g in by_sig.values()}
    if len(reps) < len(pairs):
        print(f"  Structural dedup: {len(pairs)} pairs -> {len(reps)} unique structures")

    results = []
    total_batches = (len(reps) + batch_size - 1) // batch_size

    for batch_idx in range(0, len(reps), batch_size):
        batch = reps[batch_idx : batch_idx + batch_size]
        batch_num = batch_idx // batch_size + 1
        print(f"  LLM batch {batch_num}/{total_batches} ({len(batch)} pairs)...", flush=True)

        prompt = SCREENING_PROMPT
        for i, (a, b) in enumerate(batch, 1):
            prompt += format_pair_for_llm(i, a, b)

        results_start = len(results)
        try:
            text = _call_llm(prompt, model)
            log.debug("Batch %d raw response:\n%s", batch_num, text)
            batch_results = _extract_json(text)

            if len(batch_results) != len(batch):
                log.warning("Batch %d: expected %d results, got %d",
                            batch_num, len(batch), len(batch_results))

            # Build lookup from input pairs for ticker-based matching
            batch_lookup = {(a["ticker"], b["ticker"]): (a, b) for a, b in batch}
            matched_keys: set[tuple[str, str]] = set()
            accepted, rejected, unmatched_count = 0, 0, 0

            for r in batch_results:
                _normalize_result(r)
                ra = r.get("ticker_a", "")
                rb = r.get("ticker_b", "")

                # Try direct match, then reversed order
                key = None
                if (ra, rb) in batch_lookup:
                    key = (ra, rb)
                elif (rb, ra) in batch_lookup:
                    key = (rb, ra)
                else:
                    # Fallback for non-"none" results: match via antecedent/consequent tickers
                    ant = r.get("antecedent_ticker")
                    con = r.get("consequent_ticker")
                    if ant and con:
                        result_tickers = {ant, con}
                        for bk in batch_lookup:
                            if bk not in matched_keys and {bk[0], bk[1]} == result_tickers:
                                key = bk
                                break

                if key is None:
                    log.warning("Batch %d: unmatched LLM result ticker_a=%s ticker_b=%s ant=%s con=%s",
                                batch_num, ra, rb,
                                r.get("antecedent_ticker"), r.get("consequent_ticker"))
                    unmatched_count += 1
                    continue

                matched_keys.add(key)
                # Set canonical input pair tickers
                r["ticker_a"] = key[0]
                r["ticker_b"] = key[1]

                conf = r.get("confidence")
                if conf not in ("none", "need_more_info") and r.get("antecedent_ticker") and r.get("consequent_ticker"):
                    log.info("ACCEPTED: %s -> %s [%s] %s",
                             r.get("antecedent_ticker"), r.get("consequent_ticker"),
                             conf, r.get("reasoning", ""))
                    accepted += 1
                elif conf == "need_more_info":
                    log.info("NEED_INFO: %s / %s [%s] %s",
                             r.get("ticker_a"), r.get("ticker_b"),
                             conf, r.get("reasoning", ""))
                    rejected += 1
                else:
                    log.info("REJECTED: %s -> %s [%s] %s",
                             r.get("antecedent_ticker"), r.get("consequent_ticker"),
                             conf, r.get("reasoning", ""))
                    rejected += 1
                results.append(r)

                # Fan the verdict out to structural siblings of this pair
                rep_pair = batch_lookup[key]
                for sib_pair in siblings.get(key, ()):
                    sib_r = _fanout_result(r, rep_pair, sib_pair)
                    log.info("REUSED (%s): %s / %s [%s]", key[0], sib_r["ticker_a"],
                             sib_r["ticker_b"], sib_r["confidence"])
                    results.append(sib_r)

            # Warn about input pairs with no LLM result
            unresulted = set(batch_lookup.keys()) - matched_keys
            for uk in unresulted:
                log.warning("Batch %d: no LLM result for input pair %s / %s",
                            batch_num, uk[0], uk[1])

            log.info("Batch %d summary: %d accepted, %d rejected, %d unmatched, %d missing",
                     batch_num, accepted, rejected, unmatched_count, len(unresulted))

            # Write this batch's results to DB immediately
            if conn is not None:
                batch_stored = results[results_start:]
                db_mod.bulk_upsert_pair_results(
                    conn, batch_stored, model, code_version=code_version(),
                )
        except (ValueError, KeyError, requests.RequestException, anthropic.AnthropicError) as e:
            log.warning("Batch %d failed: %s", batch_num, e)
            print(f"    Warning: batch {batch_num} failed: {e}")
            continue

        time.sleep(0.5)

    return results


# ── Output ───────────────────────────────────────────────────────────────────


def print_summary(results: list[dict]) -> None:
    """Print a terminal summary of scan results."""
    if not results:
        print("\nNo implication relationships found.")
        return

    confirmed = [r for r in results if r.get("confidence") in ("high", "medium")]
    uncertain = [r for r in results if r.get("confidence") == "low"]
    need_info = [r for r in results if r.get("confidence") == "need_more_info"]

    print(f"\n{'='*80}")
    print(f"SCAN RESULTS: {len(confirmed)} confirmed, {len(uncertain)} uncertain, {len(need_info)} need info")
    print(f"{'='*80}")

    for label, group in [("CONFIRMED", confirmed), ("UNCERTAIN", uncertain), ("NEED MORE INFO", need_info)]:
        if not group:
            continue
        print(f"\n  {label}:")
        print(f"  {'─'*76}")
        for r in sorted(group, key=lambda x: x.get("arb_cost") or 999):
            ant = r.get("antecedent_ticker", "?")
            con = r.get("consequent_ticker", "?")
            ant_title = r.get("antecedent_title", "")
            con_title = r.get("consequent_title", "")
            cost = r.get("arb_cost")
            conf = r.get("confidence", "?")
            date = r.get("payoff_date", "?")
            reasoning = r.get("reasoning", "")

            cost_str = f"${cost:.4f}" if cost is not None else "N/A"
            arb_flag = " << ARB" if cost is not None and cost < 1.0 else ""

            print(f"\n    Antecedent: {ant:<30} {ant_title}")
            print(f"    Consequent: {con:<30} {con_title}")
            print(f"    Buy: no antecedent + yes consequent")
            print(f"    Cost: {cost_str}  (need < $1.00){arb_flag}")
            print(f"    Payoff date: {date}  |  Confidence: {conf}")
            print(f"    Reasoning: {reasoning[:120]}")


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan Kalshi markets for arbitrage opportunities"
    )
    # Fetch options
    parser.add_argument(
        "--category", default="Sports",
        help="Kalshi category to scan (default: Sports)",
    )
    parser.add_argument(
        "--filter", "-f", default=None, metavar="TAG",
        help="filter series by Kalshi API tag (e.g. 'tennis' or 'Tennis,Soccer')",
    )
    parser.add_argument(
        "--min-volume", type=int, default=200,
        help="exclude markets below this volume (default: 200)",
    )
    # DB options
    parser.add_argument(
        "--db", default="slonk_arb.db",
        help="SQLite database path (default: slonk_arb.db)",
    )
    parser.add_argument(
        "--from-db", action="store_true",
        help="skip fetching, use tickers already in DB",
    )
    parser.add_argument(
        "--rescan", action="store_true",
        help="re-screen all pairs even if already in DB",
    )
    parser.add_argument(
        "--max-pairs", type=int, default=None,
        help="max number of new pairs to screen per run (caps LLM calls)",
    )
    # LLM options
    parser.add_argument(
        "--model", default="claude-sonnet-5",
        help="Anthropic model name (default: claude-sonnet-5)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=12,
        help="pairs per LLM batch (default: 12)",
    )
    # Output options
    parser.add_argument(
        "--log-file", default="scan.log",
        help="log file path (default: scan.log)",
    )
    args = parser.parse_args()

    # ── Logging setup ─────────────────────────────────────────────────────
    handler = logging.FileHandler(args.log_file, mode="a")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    logging.basicConfig(level=logging.DEBUG, handlers=[handler])
    log.info("=== scan.py started: %s ===", " ".join(sys.argv[1:]))

    # ── Database setup ────────────────────────────────────────────────────
    conn = db_mod.get_connection(args.db)

    # ── Fetch or use DB ──────────────────────────────────────────────────
    if not args.from_db:
        t0 = time.time()
        if args.filter:
            raw = [t.strip().lower() for t in args.filter.split(",")]
            filter_tags = list({_FILTER_TO_API_TAG.get(t, t).title() for t in raw})
        else:
            filter_tags = None
        active_tickers = fetch_and_store_markets(args.category, conn, filter_tags=filter_tags)
        if not active_tickers:
            print("No open markets found.")
            sys.exit(0)

        # Only deactivate when we fetched ALL tickers (no filter), otherwise
        # we'd wrongly deactivate tickers outside the filter.
        if not args.filter:
            deactivated = db_mod.deactivate_missing_tickers(conn, active_tickers)
            print(f"  {deactivated} tickers deactivated")
        print(f"  Fetch completed in {time.time() - t0:.0f}s", flush=True)

    # ── Generate candidates from DB ──────────────────────────────────────
    print("\nGrouping markets by entity (from DB)...")
    groups = db_mod.get_tickers_by_entity(conn, min_volume=args.min_volume)

    # Apply --filter to restrict which markets go to LLM screening
    if args.filter:
        filtered_groups = filter_groups_by_sport(groups, args.filter.split(","))
        skipped_entities = len(groups) - len(filtered_groups)
        groups = filtered_groups
        if skipped_entities:
            print(f"  Filter '{args.filter}': kept {len(groups)} entities, skipped {skipped_entities}")
    print(f"  Entities in 2+ series: {len(groups)}")

    if not groups:
        print("No cross-series entities found. Nothing to scan.")
        sys.exit(0)

    all_pairs = generate_candidate_pairs(groups)
    print(f"  Cross-series candidate pairs: {len(all_pairs)}")

    # ── Filter to unscreened pairs ───────────────────────────────────────
    if not args.rescan:
        screened = db_mod.get_screened_pair_keys(conn)
        pairs = [p for p in all_pairs if db_mod.sorted_key(p) not in screened]
        skipped = len(all_pairs) - len(pairs)
        if skipped:
            print(f"  Skipping {skipped} already-screened pairs")
    else:
        pairs = all_pairs

    if not pairs:
        print("No new pairs to screen.")
        sys.exit(0)

    # ── Rule-based screening (free) before the LLM ───────────────────────
    rule_results, pairs = rule_screen_pairs(pairs)
    if rule_results:
        db_mod.bulk_upsert_pair_results(
            conn, rule_results, RULE_SCREENER_MODEL, auto_confirm_high=True,
            code_version=code_version(),
        )
        n_high = sum(1 for r in rule_results if r["confidence"] == "high")
        print(f"  Rule-screened {len(rule_results)} pairs "
              f"({n_high} high auto-confirmed, {len(rule_results) - n_high} none) — no LLM tokens")
        log.info("Rule-screened %d pairs (%d high)", len(rule_results), n_high)

    # ── Structural reuse: inherit stored verdicts for known structures ───
    if pairs and not args.rescan:
        reused, pairs = reuse_screen_pairs(pairs, db_mod.get_signature_verdicts(conn))
        if reused:
            n_conf = sum(1 for r in reused
                         if r["confirmed"] and r["confidence"] == "high")
            store_reused_results(conn, reused, code_version())
            print(f"  Structural reuse: {len(reused)} pairs inherited stored verdicts "
                  f"({n_conf} auto-confirmed from human review) — no LLM tokens")
            log.info("Structural reuse: %d pairs, %d auto-confirmed", len(reused), n_conf)

    if not pairs:
        print("No pairs left for LLM screening.")
        print_summary([r for r in rule_results if r.get("confidence") == "high"])
        conn.close()
        return

    if args.max_pairs is not None:
        if args.max_pairs == 0:
            print("--max-pairs 0: skipping LLM screening.")
            print_summary([])
            conn.close()
            return
        if len(pairs) > args.max_pairs:
            print(f"  Capping to {args.max_pairs} pairs (--max-pairs)")
            pairs = pairs[:args.max_pairs]

    # ── LLM screening ────────────────────────────────────────────────────
    model = args.model
    batch_size = args.batch_size
    print(f"\nScreening {len(pairs)} pairs with {model} (batch_size={batch_size})...", flush=True)
    t0 = time.time()
    all_results = screen_pairs_with_llm(pairs, model, batch_size, conn=conn)
    print(f"  DB: {len(all_results)} pair results stored (incremental)")
    print(f"  LLM screening completed in {time.time() - t0:.0f}s", flush=True)

    # ── Filter to confirmed for output (LLM + rule results) ─────────────
    all_results = rule_results + all_results
    confirmed = [r for r in all_results if r.get("confidence") not in ("none", "need_more_info") and r.get("antecedent_ticker") and r.get("consequent_ticker")]
    print(f"  Pairs with implication: {len(confirmed)}")

    # ── Output ───────────────────────────────────────────────────────────
    print_summary(confirmed)

    conn.close()


if __name__ == "__main__":
    main()
