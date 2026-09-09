#!/usr/bin/env python3
"""Certbot HTTP-01 hook confined to one pre-created challenge ConfigMap."""

from __future__ import annotations

import argparse
import http.client
import os
import re
import sys
import time

from kubernetes import client, config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["present", "cleanup"])
    args = parser.parse_args()
    domain = os.environ["CERTBOT_DOMAIN"]
    token = os.environ["CERTBOT_TOKEN"]
    validation = os.environ["CERTBOT_VALIDATION"]
    namespace = os.environ["ACME_NAMESPACE"]
    name = os.environ["ACME_CONFIGMAP"]
    if domain != os.environ["ACME_DOMAIN"] or not domain.endswith(".cloudapp.azure.com"):
        raise ValueError("Certbot domain does not match this gateway")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", token):
        raise ValueError("Invalid ACME token")
    config.load_incluster_config()
    api = client.CoreV1Api()
    api.patch_namespaced_config_map(
        name,
        namespace,
        {"data": {token: validation if args.action == "present" else None}},
        _request_timeout=20,
    )
    if args.action == "cleanup":
        return 0
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection(domain, 80, timeout=10)
        try:
            connection.request("GET", f"/.well-known/acme-challenge/{token}")
            response = connection.getresponse()
            if response.status == 200 and response.read(2048).decode() == validation:
                print("Public ACME challenge is reachable.")
                return 0
        except (OSError, http.client.HTTPException) as exc:
            print(f"Waiting for ACME route: {type(exc).__name__}", file=sys.stderr)
        finally:
            connection.close()
        time.sleep(5)
    raise RuntimeError("ACME route did not serve the expected token within 180 seconds")


if __name__ == "__main__":
    sys.exit(main())
