"""Toggle the simulated provider outage on the llm-gateway (demo item 8).

    docker compose exec api python -m claimtriage.tools.fault on      # provider "down"
    docker compose exec api python -m claimtriage.tools.fault status
    docker compose exec api python -m claimtriage.tools.fault off
    docker compose exec api python -m claimtriage.tools.fault slow 25 # add 25 s latency (timeouts)
"""
import json
import sys

import httpx

from ..common.config import get_settings


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    base = get_settings().llm_gateway_url.rstrip("/")
    if cmd == "status":
        r = httpx.get(base + "/admin/breaker", timeout=5)
    else:
        body = {"provider_down": cmd == "on",
                "extra_latency_s": float(sys.argv[2]) if cmd == "slow" and len(sys.argv) > 2 else 0.0}
        r = httpx.post(base + "/admin/fault", json=body, timeout=5)
    print(json.dumps(r.json(), indent=2))


if __name__ == "__main__":
    main()
