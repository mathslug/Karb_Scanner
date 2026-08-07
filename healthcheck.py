#!/usr/bin/env python3
"""Container health check — exits 0 if the web app answers, 1 otherwise.

A script rather than an inline `python -c "…"` in the Quadlet unit: Quadlet's
parser does not survive nested quotes and silently truncates the command at the
first inner quote, leaving the container permanently "unhealthy" while the app
itself is fine.
"""

import sys
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=5) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
