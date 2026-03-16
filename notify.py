"""Send email notifications for BUY recommendations via Mailgun."""

import logging
import os

import requests

log = logging.getLogger("notify")


def send_buy_alert(results: list[dict]) -> bool:
    """Send email summarizing BUY recommendations. Returns True on success."""
    api_key = os.environ.get("MAILGUN_API_KEY")
    domain = os.environ.get("MAILGUN_DOMAIN")
    to_email = os.environ.get("NOTIFY_EMAIL")
    if not all([api_key, domain, to_email]):
        log.warning("Mailgun not configured, skipping notification")
        return False

    buys = [r for r in results if r.get("recommendation") == "buy"]
    if not buys:
        return False

    total_capital = sum(r.get("total_cost", 0) for r in buys)
    lines = [f"Found {len(buys)} BUY recommendation(s). Total capital: ${total_capital:.2f}\n"]
    for r in sorted(buys, key=lambda x: -(x.get("excess_yield") or 0)):
        ann_pct = r['annualized_yield'] * 100 if r.get('annualized_yield') is not None else 0
        exc_pct = r['excess_yield'] * 100 if r.get('excess_yield') is not None else 0
        lines.append(
            f"  Pair #{r['pair_id']:>3}  n={r['n_contracts']:>4}  "
            f"yield={ann_pct:>6.2f}%  "
            f"excess={exc_pct:>+6.2f}%  "
            f"cost=${r['total_cost']:>8.2f}"
        )

    body = "\n".join(lines)
    resp = requests.post(
        f"https://api.mailgun.net/v3/{domain}/messages",
        auth=("api", api_key),
        data={
            "from": f"Kalshi Arb <alerts@{domain}>",
            "to": [to_email],
            "subject": f"[Kalshi Arb] {len(buys)} BUY signal(s)",
            "text": body,
        },
        timeout=10,
    )
    resp.raise_for_status()
    log.info("Sent BUY alert email (%d recommendations)", len(buys))
    return True
