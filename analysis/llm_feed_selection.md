# Which markets should we feed to the LLM?

*2026-07-10. Data: production DB pulled same day (702,432 trade evaluations across 3,383 pairs; 2.72M price snapshots Mar 16 – Jul 10; 8,758 screened pairs). Reproduce with `uv run analysis/llm_feed_selection.py`. Politics sizing via live Kalshi API sample.*

## TL;DR

**No arbitrage has ever been observed — not once, at any depth, in any market family.** Across ~4 months of twice-daily evaluations and price snapshots, the minimum pair cost ever recorded is exactly $1.00 (both walked-book-with-fees and top-of-book), and the breakeven after fees is ≤ $0.99. Expected reward measured on this data is $0 against a ~$60–90/mo GPU screening spend, so "feed nothing" is a defensible answer. The recommended compromise is a ~$10/mo configuration: screen weekly instead of daily, replace the PGA ladder screening with rules (no LLM), and keep the LLM only for cross-category pairs — preserving cheap optionality on regime change without paying daily for markets that are provably quoted tight.

## 1. Has arbitrage ever existed here?

| Measure | Result |
|---|---|
| Min walked-book cost incl. fees (702K evaluations) | **$1.00** — never below |
| Min top-of-book cost (ask+ask) | **$1.00** |
| Reconstructed price history, 3,351 pairs, same-timestamp joins | **0** observations below $1.00; 0 below $0.97 |
| Pairs whose lifetime minimum TOB sat in $1.00–1.02 | 1,744 (52%) |

Breakeven math: a same-day pair must cost ≲ $0.99 (two legs of taker fees ≈ 0.7–2¢ at typical prices); a 200-day pair must cost ≲ $0.966 (fees + ~5% hurdle discounted over the gap). The observed floor of $1.00 means the closest the market ever came was still ≥ 1¢ (same-day) to ≥ 3.4¢ (long-gap) away from profitability.

Caveats: snapshots are 1–3×/day (intraday dips invisible); pairs listed during the Apr–Jul screening outage were never screened, so their history isn't in the confirmed/high set — but their families (weekly golf ladders) are the same structures measured here.

## 2. The user hypotheses, tested

**"Within-tournament pairs are more efficient" — confirmed.** Per-pair lifetime minimum cost by settlement gap (antecedent → consequent expiration):

| Gap | n pairs | min | median of minima |
|---|---|---|---|
| Same settlement (≤1d) — e.g. Top5⊂Top10 | 2,313 | 1.000 | **1.010** |
| Short gap (2–30d) | 758 | 1.010 | 1.020 |
| Long gap (>30d) — e.g. French Open → Grand Slam | 71 | 1.000 | 1.056 |

Within-tournament ladders are quoted pinned at parity+1–2¢ — market makers clearly price the lattice coherently, and it never crosses.

**"Time-difference pairs are juiciest" — true but already priced.** Long-gap pairs trade *further above* parity, and the premium is almost exactly the time value of money: FO→GS pairs averaged $1.04–1.14 with a 203-day gap; $1.03 over 203 days ≈ 5% annualized ≈ the hurdle rate. The market charges rationally for the capital lock-up. These pairs have the most *room* to dislocate (and the only sub-$0.97 profitability threshold within reach if they ever do), but no dislocation has occurred yet.

## 3. Where the LLM spend goes vs. what it yields

Weekly new-pair volume: **Golf ≈ 1,400–2,600/wk (~85%)**, NHL 75–360, Tennis 38–125. Screening yield by family:

- **PGA finish-position lattice** (winner⊂Top5⊂Top10⊂Top20⊂MakeCut): ~2,700 pairs screened, 70–97% confirmed high — but the relationship is *deterministic from the series tickers*. An LLM is not needed to know Top-5 implies Top-10.
- **KXPGAR1LEAD × everything** (~1,400 pairs) and **KXNHLTOTAL × itself** (446): **0% high, 100% none** — pure waste, blocklistable by rule.
- Genuinely LLM-worthy judgment calls (KXFOMEN→KXATPGRANDSLAM, KXNHLPRES→KXNHLPLAYOFF, KXPGAMAJORTOP10 relations, KXMOWOMEN→KXWTAMATCH): a few dozen new pairs per month.

Gap distribution of all screened pairs: ≤1d: 3,265 · 2–7d: 925 · >7d: 4,568. The settlement gap is computable *before* the LLM from `expected_expiration_time`, so it can be a free pre-filter.

Inference cost (ephemeral MI300X, $1.99/hr): fixed ~1–1.5 GPU-h/day dominates; marginal ≈ $0.002–0.005/pair. Daily ≈ **$60–90/mo**; weekly ≈ **$8–12/mo**; monthly ≈ $2–3.

## 4. Politics

Sampled 150 election-shaped series (of 2,082 Politics series) live: **85 open markets, 65 candidate pairs, essentially zero volume.** Real implication ladders exist (date-threshold "Before X" ladders, per-senator resignation markets) but with no liquidity there is nothing to fill even if mispriced. **Skip politics now; revisit around late 2027** when 2028 primary → nomination → presidency series list — those are structurally ideal (long gaps, person entities, real volume) and cost pennies to screen.

## 5. Recommendation

Expected reward minus inference cost is negative for every configuration on observed data; what remains is buying cheap optionality on rare dislocations and new market types:

1. **Rule-based screener for lattice families** (PGA/LIV Top-N chains → auto-high; R1LEAD/TOTAL families → auto-none). Removes ~85% of LLM volume with zero quality loss.
2. **Screen weekly, not daily.** Keep the daily 07:30 ticker/price fetch (free, and it is the dataset that made this analysis possible).
3. If cutting further, **LLM-screen only pairs with settlement gap > 7 days** — the only family with theoretical room to dislocate. ≤1d ladders never came within a cent of breakeven in 4 months.
4. **Don't add politics yet**; calendar a look at the 2028 cycle.
5. Net config: ~$10/mo of credits. Accept that the honest empirical answer to "what should we feed the LLM to maximize E[reward] − cost" is *approximately nothing* — the value of continuing at all is the option on market-structure change, not observed edge.
