#!/usr/bin/env python3
"""Manage the ephemeral DigitalOcean dedicated-inference deployment for scan.py.

The scanner's LLM screening runs once daily, so instead of paying for a
24/7 GPU we create a dedicated-inference deployment just before the scan
and destroy it right after (~1-2 GPU-hours/day).

Usage:
    uv run dedicated.py create     # create + wait for active, print shell exports
    uv run dedicated.py destroy    # tear down (idempotent, safe to re-run)
    uv run dedicated.py status     # list deployments

`create` prints `export LLM_BASE_URL=...` / `export LLM_API_KEY=...` lines on
stdout for eval'ing in a shell wrapper (see deploy/run_scan_gpu.sh); progress
goes to stderr. Requires DIGITALOCEAN_TOKEN in the environment or .env with
dedicated-inference read/create/delete scopes (plus vpc read/create the first
time, to ensure the atl1 VPC exists).
"""

import argparse
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API = "https://api.digitalocean.com/v2"
DEPLOYMENT_NAME = "slonk-scan-llm"
REGION = "atl1"
GPU_SLUG = "gpu-mi300x1-192gb"
MODEL_SLUG = "openai/gpt-oss-120b"
VPC_NAME = "slonk-atl1"
POLL_INTERVAL_S = 20
CREATE_TIMEOUT_S = 40 * 60


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _headers() -> dict:
    token = os.environ.get("DIGITALOCEAN_TOKEN")
    if not token:
        log("ERROR: DIGITALOCEAN_TOKEN not set")
        sys.exit(2)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def list_deployments() -> list[dict]:
    resp = requests.get(f"{API}/dedicated-inferences", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("dedicated_inferences") or []


def find_deployment() -> dict | None:
    for d in list_deployments():
        if d.get("name") == DEPLOYMENT_NAME or d.get("spec", {}).get("name") == DEPLOYMENT_NAME:
            return d
    return None


def get_deployment(dep_id: str) -> dict:
    resp = requests.get(f"{API}/dedicated-inferences/{dep_id}", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()["dedicated_inference"]


def ensure_vpc() -> str:
    """Return the UUID of a VPC in REGION, creating one if none exists."""
    resp = requests.get(f"{API}/vpcs", headers=_headers(), params={"per_page": 200}, timeout=30)
    resp.raise_for_status()
    for v in resp.json().get("vpcs", []):
        if v["region"] == REGION:
            return v["id"]
    log(f"No {REGION} VPC found; creating {VPC_NAME}...")
    resp = requests.post(
        f"{API}/vpcs", headers=_headers(),
        json={"name": VPC_NAME, "region": REGION}, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["vpc"]["id"]


def destroy(quiet: bool = False) -> int:
    """Delete the deployment if it exists. Returns 0 (idempotent)."""
    dep = find_deployment()
    if not dep:
        if not quiet:
            log(f"No deployment named {DEPLOYMENT_NAME}; nothing to destroy.")
        return 0
    dep_id = dep["id"]
    resp = requests.delete(f"{API}/dedicated-inferences/{dep_id}", headers=_headers(), timeout=30)
    if resp.status_code not in (200, 202, 204):
        log(f"WARNING: delete returned {resp.status_code}: {resp.text[:200]}")
        return 1
    log(f"Destroyed deployment {dep_id} ({DEPLOYMENT_NAME}).")
    return 0


def create() -> int:
    """Create the deployment, wait for active, mint a token, print exports."""
    # A stray deployment from a failed prior run blocks the unique name.
    if find_deployment():
        log("Stray deployment found; destroying it first...")
        destroy()
        deadline = time.time() + 10 * 60
        while find_deployment() and time.time() < deadline:
            time.sleep(POLL_INTERVAL_S)
        if find_deployment():
            log("ERROR: stray deployment did not go away; aborting.")
            return 2

    vpc_uuid = ensure_vpc()
    log(f"Creating {DEPLOYMENT_NAME}: {MODEL_SLUG} on {GPU_SLUG} in {REGION}...")
    body = {
        "spec": {
            "version": 1,
            "name": DEPLOYMENT_NAME,
            "region": REGION,
            "vpc": {"uuid": vpc_uuid},
            "enable_public_endpoint": True,
            "model_deployments": [{
                "model_slug": MODEL_SLUG,
                "model_provider": "hugging_face",
                "workload_config": {},
                "accelerators": [{
                    "scale": 1,
                    "type": "prefill_decode",
                    "accelerator_slug": GPU_SLUG,
                }],
            }],
        },
    }
    resp = requests.post(f"{API}/dedicated-inferences", headers=_headers(), json=body, timeout=60)
    if resp.status_code not in (200, 201, 202):
        log(f"ERROR: create returned {resp.status_code}: {resp.text[:300]}")
        return 2

    dep = resp.json().get("dedicated_inference") or find_deployment()
    if not dep:
        log("ERROR: created deployment not found in listing.")
        return 2
    dep_id = dep["id"]
    log(f"Deployment {dep_id} accepted; waiting for active (timeout {CREATE_TIMEOUT_S // 60} min)...")

    deadline = time.time() + CREATE_TIMEOUT_S
    status = dep.get("status", "new")
    while time.time() < deadline:
        dep = get_deployment(dep_id)
        status = dep.get("status")
        if status == "active":
            break
        if status == "error":
            log("ERROR: deployment entered error status (likely no GPU capacity); destroying.")
            destroy()
            return 3
        log(f"  status={status}, waiting...")
        time.sleep(POLL_INTERVAL_S)

    if status != "active":
        log("ERROR: timed out waiting for active; destroying.")
        destroy()
        return 3

    endpoint = dep.get("endpoints", {}).get("public_endpoint_fqdn")
    if not endpoint:
        log("ERROR: active deployment has no public endpoint; destroying.")
        destroy()
        return 3

    log("Deployment active; minting access token...")
    resp = requests.post(
        f"{API}/dedicated-inferences/{dep_id}/tokens",
        headers=_headers(), json={"name": f"scan-{int(time.time())}"}, timeout=30,
    )
    resp.raise_for_status()
    token_value = resp.json()["token"]["value"]

    log(f"Ready: {endpoint}")
    print(f"export LLM_BASE_URL='{endpoint}'")
    print(f"export LLM_API_KEY='{token_value}'")
    print(f"export LLM_MODEL='{MODEL_SLUG}'")
    return 0


def status() -> int:
    deps = list_deployments()
    if not deps:
        print("No dedicated-inference deployments.")
        return 0
    for d in deps:
        endpoint = d.get("endpoints", {}).get("public_endpoint_fqdn", "-")
        print(f"{d['id']}  {d.get('name', '?'):20} {d.get('status', '?'):12} {endpoint}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the ephemeral GPU LLM deployment")
    parser.add_argument("command", choices=["create", "destroy", "status"])
    args = parser.parse_args()
    if args.command == "create":
        sys.exit(create())
    elif args.command == "destroy":
        sys.exit(destroy())
    else:
        sys.exit(status())


if __name__ == "__main__":
    main()
