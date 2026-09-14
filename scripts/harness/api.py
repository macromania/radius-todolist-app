#!/usr/bin/env python3
"""Call a plane API using protected project endpoint/key files."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.operations.project import ROOT  # noqa: E402


def endpoint(environment: str, target: str) -> tuple[str, str]:
    state = (ROOT / ".state" / environment).resolve()
    inventory = json.loads((state / "endpoints.json").read_text())
    if target == "management":
        selected = inventory["management"]
    else:
        plane, pair = target.split(":", 1)
        if plane not in {"control", "data"}:
            raise ValueError("Target must be management, control:<pair>, or data:<pair>")
        selected = inventory["pairs"][pair][plane]
    key_path = (state / selected["key_file"]).resolve()
    if not key_path.is_relative_to(state):
        raise ValueError("Demo key must come from this environment's state directory")
    key = key_path.read_text().strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("Invalid demo key file")
    url = selected["url"]
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("API base URL must not contain credentials, query, or fragment")
    if environment == "azure":
        if parts.scheme != "https" or not (parts.hostname or "").endswith(".cloudapp.azure.com"):
            raise ValueError("Azure API must use HTTPS on the provisioned Azure hostname")
    else:
        if parts.scheme != "http" or not ipaddress.ip_address(parts.hostname or "").is_loopback:
            raise ValueError("Local API must use loopback HTTP")
        if not parts.port or not 35490 <= parts.port <= 35494:
            raise ValueError("Local gateway port is outside the project reservation")
    return url.rstrip("/"), key


def request(url: str, key: str, method: str, path: str, body: str | None) -> tuple[int, str]:
    if not path.startswith("/") or path.startswith("//") or "#" in path:
        raise ValueError("API path must be an absolute path on the selected plane")
    if body is not None:
        json.loads(body)
    parts = urlsplit(url)
    connection_type = (
        http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(parts.hostname, parts.port, timeout=30)
    try:
        connection.request(
            method,
            f"{parts.path}{path}",
            body=body.encode() if body else None,
            headers={"X-Demo-Key": key, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", choices=["azure", "local"])
    parser.add_argument("target")
    parser.add_argument("method", choices=["GET", "POST", "PUT"])
    parser.add_argument("path")
    parser.add_argument("body", nargs="?")
    args = parser.parse_args()
    try:
        url, key = endpoint(args.environment, args.target)
        status, body = request(url, key, args.method, args.path, args.body)
        print(f"HTTP {status}", file=sys.stderr)
        print(body)
        return 0 if 200 <= status < 300 else 1
    except (ValueError, KeyError, OSError, http.client.HTTPException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
