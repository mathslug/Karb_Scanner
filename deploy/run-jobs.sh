#!/usr/bin/env bash
#
# run-jobs.sh — run one scheduled karb job as short-lived containers.
#
#     deploy/run-jobs.sh daily
#
# This replaces /etc/cron.d/slonk-arb. Each job is a sequence of steps run from
# the same image the web UI runs, so the scanner and the review UI can never
# disagree about which code version they are running.
#
# Two deliberate departures from the cron file it replaces:
#
#   * Every step runs even if an earlier one fails — that was cron's `;`
#     semantics, and losing a whole evaluation sweep because a Treasury yield
#     fetch 500'd would be a regression. Unlike cron, a failure is remembered
#     and this script exits non-zero, so systemd marks the unit failed and the
#     failure is visible rather than buried in a log nobody opens.
#
#   * Logs go to stderr, which under systemd means the journal, rather than to
#     the four unrotated files that reached 162MB on the droplet. See the
#     logging comment in scan.py for why that also means INFO rather than
#     DEBUG.

set -uo pipefail   # deliberately not -e: see above

JOB="${1:-}"
IMAGE="localhost/karb:latest"
DATA="${HOME}/data/karb"
ENV_FILE="${HOME}/.config/karb.env"
DB="/data/slonk_arb.db"

failed=0

step() {
  local name="$1"; shift
  printf '== %s\n' "$name"
  # --rm so exited containers do not accumulate. --memory is sized to hold the
  # database rather than to constrain the process — these jobs sweep the whole
  # 702MB of it, and a cap below that makes the kernel reclaim page cache from
  # this cgroup and re-read from the SD card. See deploy/karb.container.
  if podman run --rm \
      --memory=1536m \
      --env-file "$ENV_FILE" \
      --env "SLONK_DB=${DB}" \
      --volume "${DATA}:/data" \
      "$IMAGE" python "$@"
  then
    printf '== %s: ok\n' "$name"
  else
    printf '== %s: FAILED (exit %d)\n' "$name" "$?" >&2
    failed=1
  fi
}

case "$JOB" in
  # 07:30 UTC — fetch every sports ticker into the DB. No LLM calls.
  sports)
    step "scan sports" \
      scan.py --category Sports --max-pairs 0 --db "$DB" --log-file -
    ;;

  # 08:00 UTC — the main pass: yields, then an LLM screen, then both
  # evaluation modes over what it found.
  daily)
    step "fetch yields" \
      fetch_yields.py --db "$DB"
    step "scan" \
      scan.py --from-db --filter "tennis,hockey,golf" --min-volume 200 \
              --db "$DB" --log-file -
    step "evaluate" \
      evaluate.py --db "$DB" --log-file -
    step "evaluate high" \
      evaluate.py --mode high --db "$DB" --log-file -
    ;;

  # 15:00 and 20:00 UTC — full re-evaluation of confirmed and
  # high-confidence pairs.
  sweep)
    step "evaluate" \
      evaluate.py --db "$DB" --log-file -
    step "evaluate high" \
      evaluate.py --mode high --db "$DB" --log-file -
    ;;

  # Every hour at :30 — re-check only pairs whose latest top-of-book cost is
  # near parity. Small set, ~2 min; catches intraday dislocations between
  # sweeps.
  hot)
    step "evaluate hot" \
      evaluate.py --hot --db "$DB" --log-file -
    step "evaluate hot high" \
      evaluate.py --hot --mode high --db "$DB" --log-file -
    ;;

  *)
    printf 'usage: %s {sports|daily|sweep|hot}\n' "$0" >&2
    exit 2
    ;;
esac

exit "$failed"
