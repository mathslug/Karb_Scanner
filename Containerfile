# karb — container image
#
# Build this ON the target host:
#
#   podman build -t karb:latest .
#
# One image serves two roles. `CMD` runs the web app; the scheduled jobs run
# the same image with a different command (see rpi/systemd/karb-*.service), so
# the scanner and the review UI can never disagree about which code version
# they are running.
#
# The process runs as container-root on purpose. Under rootless podman the
# container's uid 0 maps to the unprivileged host user that owns the container
# (see /etc/subuid), so this is not privileged on the host — and it keeps the
# database under /data owned by that host user, which is what lets the off-box
# backup read it.

FROM docker.io/library/python:3.13-slim-trixie

# Pinned rather than :latest so a rebuild six months from now resolves the same
# dependencies the lockfile was written against.
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /usr/local/bin/uv

ENV SLONK_DB=/data/slonk_arb.db \
    PYTHONUNBUFFERED=1 \
    UV_NO_SYNC=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# `git describe` supplies the code version shown in the UI. Without git the app
# still runs, it just reports "unknown".
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Dependencies in their own layer so app-code edits don't force a reinstall.
# --frozen fails loudly if uv.lock has drifted from pyproject.toml, rather than
# quietly resolving something the lockfile never described.
# --no-install-project because there is no [build-system]: this is an
# application laid out as loose modules, not a package to install.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

# The test suite runs at build time and a failure fails the build, so a bad
# commit never reaches a running container. This is the gate the old GitHub
# Actions deploy provided (`uv run pytest -x -q` before restarting the
# service); the move to pull-based deploys would otherwise have dropped it
# silently. 166 tests, ~1s, no network.
#
# One RUN, ending with the dev dependencies pruned back out, so pytest does not
# survive into the image layer.
RUN uv sync --frozen --no-install-project --extra dev \
 && .venv/bin/pytest -x -q \
 && uv sync --frozen --no-dev --no-install-project

# Bind-mounted at runtime; create it so the image also runs bare.
RUN mkdir -p /data

EXPOSE 8000

# Two workers as on the droplet. Safe here in a way it is not for AvaLong:
# karb keeps all its state in SQLite, not in process memory.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", \
     "--access-logfile", "-", "app:create_app()"]
