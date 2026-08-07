#!/usr/bin/env python3
"""Manage the ephemeral GPU droplet running Ollama for LLM screening.

Alternative to dedicated.py (DigitalOcean dedicated inference): runs the same
gpt-oss-120b on a plain MI300X GPU droplet ($1.99/hr vs $2.59/hr) with Ollama
serving an OpenAI-compatible endpoint. Same contract as dedicated.py:

    uv run gpu_droplet.py create    # create + wait ready, print shell exports
    uv run gpu_droplet.py destroy   # tear down by tag (idempotent)
    uv run gpu_droplet.py status    # list droplets + snapshot
    uv run gpu_droplet.py snapshot  # power off + snapshot for fast future boots

`create` prefers the snapshot (Ollama + model weights baked in, ~5 min boot);
without one it falls back to the ROCm base image and a cloud-init script that
installs Ollama and pulls the model (~20-40 min first boot).

Ollama has no auth, so access control is a tag-scoped cloud firewall that only
admits port 11434 from the machine running `create` — which is the scanner.
Requires DIGITALOCEAN_TOKEN (droplet, firewall, tag, ssh_key read scopes; write
for droplet/firewall).
"""

import argparse
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API = "https://api.digitalocean.com/v2"
DROPLET_NAME = "slonk-scan-llm-gpu"
TAG = "slonk-llm"
REGION = "atl1"
SIZE = "gpu-mi300x1-192gb"
BASE_IMAGE = "digitaloceanai-rocmsoftware"
SNAPSHOT_NAME = "slonk-llm-snapshot"
FIREWALL_NAME = "slonk-llm-fw"
OLLAMA_PORT = 11434
MODEL = "gpt-oss:120b"
POLL_INTERVAL_S = 20
READY_TIMEOUT_S = 45 * 60

# First-boot provisioning when starting from the bare ROCm image. The
# sentinel file lets `snapshot` verify provisioning finished.
USER_DATA = f"""#!/bin/bash
set -e
curl -fsSL https://ollama.com/install.sh | sh
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf <<EOF
[Service]
Environment="OLLAMA_HOST=0.0.0.0:{OLLAMA_PORT}"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_CONTEXT_LENGTH=32768"
EOF
systemctl daemon-reload
systemctl enable --now ollama
systemctl restart ollama
ollama pull {MODEL}
touch /var/lib/slonk-llm-provisioned
"""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _headers() -> dict:
    token = os.environ.get("DIGITALOCEAN_TOKEN")
    if not token:
        log("ERROR: DIGITALOCEAN_TOKEN not set")
        sys.exit(2)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def list_droplets() -> list[dict]:
    resp = requests.get(
        f"{API}/droplets", headers=_headers(),
        params={"tag_name": TAG, "per_page": 50}, timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("droplets") or []


def public_ip(droplet: dict) -> str | None:
    for net in droplet.get("networks", {}).get("v4", []):
        if net.get("type") == "public":
            return net["ip_address"]
    return None


def find_snapshot() -> str | None:
    resp = requests.get(
        f"{API}/snapshots", headers=_headers(),
        params={"resource_type": "droplet", "per_page": 200}, timeout=30,
    )
    resp.raise_for_status()
    for s in resp.json().get("snapshots", []):
        if s["name"] == SNAPSHOT_NAME:
            return s["id"]
    return None


def _caller_ip() -> str | None:
    try:
        return requests.get("https://api.ipify.org", timeout=10).text.strip()
    except requests.RequestException:
        return None


def ensure_firewall() -> None:
    """Create or update the tag-scoped firewall admitting Ollama traffic
    only from the machine running create.

    That machine IS the scanner, so a separate hostname lookup was always
    redundant — and once karb moved behind a Cloudflare tunnel it became
    actively dangerous: karb.mathslug.com resolves to a shared anycast address,
    which this would have whitelisted on port 11434 in front of an
    unauthenticated Ollama.
    """
    caller = _caller_ip()
    sources = [caller] if caller else []
    if not sources:
        log("ERROR: no source IPs for firewall; refusing to expose Ollama")
        sys.exit(2)

    spec = {
        "name": FIREWALL_NAME,
        "inbound_rules": [
            {"protocol": "tcp", "ports": "22",
             "sources": {"addresses": ["0.0.0.0/0", "::/0"]}},
            {"protocol": "tcp", "ports": str(OLLAMA_PORT),
             "sources": {"addresses": sources}},
        ],
        "outbound_rules": [
            {"protocol": "tcp", "ports": "0",
             "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
            {"protocol": "udp", "ports": "0",
             "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
            {"protocol": "icmp",
             "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
        ],
        "tags": [TAG],
    }
    requests.post(f"{API}/tags", headers=_headers(), json={"name": TAG}, timeout=30)
    resp = requests.get(f"{API}/firewalls", headers=_headers(), params={"per_page": 200}, timeout=30)
    resp.raise_for_status()
    existing = next((f for f in resp.json().get("firewalls", []) if f["name"] == FIREWALL_NAME), None)
    if existing:
        resp = requests.put(f"{API}/firewalls/{existing['id']}", headers=_headers(), json=spec, timeout=30)
    else:
        resp = requests.post(f"{API}/firewalls", headers=_headers(), json=spec, timeout=30)
    resp.raise_for_status()
    log(f"Firewall {FIREWALL_NAME}: port {OLLAMA_PORT} open to {', '.join(sources)}")


def _ssh_key_ids() -> list[int]:
    resp = requests.get(f"{API}/account/keys", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return [k["id"] for k in resp.json().get("ssh_keys", [])]


def wait_ready(ip: str, deadline: float) -> bool:
    """Wait until Ollama serves MODEL, then trigger a warm-up load."""
    base = f"http://{ip}:{OLLAMA_PORT}"
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base}/api/tags", timeout=10)
            models = [m.get("name", "") for m in resp.json().get("models", [])]
            if any(m.startswith(MODEL) for m in models):
                break
            log(f"  ollama up, model not ready yet (have: {models or 'none'})...")
        except (requests.RequestException, ValueError):
            log("  waiting for ollama...")
        time.sleep(POLL_INTERVAL_S)
    else:
        return False

    log("Model present; warm-up inference (loads weights into VRAM)...")
    try:
        resp = requests.post(
            f"{base}/v1/chat/completions",
            json={"model": MODEL, "max_tokens": 10,
                  "messages": [{"role": "user", "content": "Say ok."}]},
            timeout=600,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log(f"ERROR: warm-up failed: {e}")
        return False


def create() -> int:
    if list_droplets():
        log("Stray GPU droplet found; destroying first...")
        destroy()
        time.sleep(20)

    ensure_firewall()
    snapshot_id = find_snapshot()
    image: str | int
    if snapshot_id:
        image = snapshot_id
        log(f"Creating {DROPLET_NAME} from snapshot {SNAPSHOT_NAME} ({SIZE}, {REGION})...")
    else:
        image = BASE_IMAGE
        log(f"No snapshot; creating {DROPLET_NAME} from {BASE_IMAGE} — first boot "
            f"installs Ollama and pulls {MODEL} (~20-40 min)...")

    body = {
        "name": DROPLET_NAME,
        "region": REGION,
        "size": SIZE,
        "image": image,
        "ssh_keys": _ssh_key_ids(),
        "tags": [TAG],
    }
    if not snapshot_id:
        body["user_data"] = USER_DATA
    resp = requests.post(f"{API}/droplets", headers=_headers(), json=body, timeout=60)
    if resp.status_code != 202:
        log(f"ERROR: droplet create returned {resp.status_code}: {resp.text[:300]}")
        return 2
    droplet_id = resp.json()["droplet"]["id"]

    deadline = time.time() + READY_TIMEOUT_S
    ip = None
    while time.time() < deadline:
        resp = requests.get(f"{API}/droplets/{droplet_id}", headers=_headers(), timeout=30)
        resp.raise_for_status()
        d = resp.json()["droplet"]
        ip = public_ip(d)
        if d.get("status") == "active" and ip:
            break
        log(f"  droplet status={d.get('status')}, waiting...")
        time.sleep(POLL_INTERVAL_S)
    if not ip:
        log("ERROR: droplet never became active; destroying.")
        destroy()
        return 3

    log(f"Droplet active at {ip}; waiting for Ollama + {MODEL}...")
    if not wait_ready(ip, deadline):
        log("ERROR: Ollama/model not ready in time; destroying.")
        destroy()
        return 3

    log(f"Ready: http://{ip}:{OLLAMA_PORT}")
    print(f"export LLM_BASE_URL='http://{ip}:{OLLAMA_PORT}'")
    print("export LLM_API_KEY='ollama'")
    print(f"export LLM_MODEL='{MODEL}'")
    return 0


def destroy() -> int:
    """Delete all droplets carrying TAG. Idempotent."""
    if not list_droplets():
        log(f"No droplets tagged {TAG}; nothing to destroy.")
        return 0
    resp = requests.delete(
        f"{API}/droplets", headers=_headers(), params={"tag_name": TAG}, timeout=30,
    )
    if resp.status_code not in (200, 202, 204):
        log(f"WARNING: delete returned {resp.status_code}: {resp.text[:200]}")
        return 1
    log(f"Destroyed droplets tagged {TAG}.")
    return 0


def snapshot() -> int:
    """Power off the droplet and snapshot it as SNAPSHOT_NAME for fast boots."""
    droplets = list_droplets()
    if not droplets:
        log("No droplet to snapshot.")
        return 1
    d = droplets[0]
    log(f"Powering off {d['name']} ({d['id']})...")
    requests.post(f"{API}/droplets/{d['id']}/actions", headers=_headers(),
                  json={"type": "power_off"}, timeout=30)
    time.sleep(30)
    log(f"Snapshotting as {SNAPSHOT_NAME} (takes several minutes)...")
    resp = requests.post(f"{API}/droplets/{d['id']}/actions", headers=_headers(),
                         json={"type": "snapshot", "name": SNAPSHOT_NAME}, timeout=30)
    resp.raise_for_status()
    action = resp.json()["action"]
    while True:
        resp = requests.get(f"{API}/actions/{action['id']}", headers=_headers(), timeout=30)
        status = resp.json()["action"]["status"]
        if status == "completed":
            log("Snapshot complete.")
            return 0
        if status == "errored":
            log("ERROR: snapshot failed.")
            return 1
        log(f"  snapshot {status}...")
        time.sleep(30)


def status() -> int:
    droplets = list_droplets()
    if not droplets:
        print("No GPU droplets.")
    for d in droplets:
        print(f"{d['id']}  {d['name']:22} {d['status']:10} {public_ip(d) or '-'}")
    snap = find_snapshot()
    print(f"snapshot {SNAPSHOT_NAME}: {'present (' + str(snap) + ')' if snap else 'absent'}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the ephemeral Ollama GPU droplet")
    parser.add_argument("command", choices=["create", "destroy", "status", "snapshot"])
    args = parser.parse_args()
    sys.exit({"create": create, "destroy": destroy,
              "status": status, "snapshot": snapshot}[args.command]())


if __name__ == "__main__":
    main()
