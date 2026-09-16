#!/usr/bin/env python3
"""Subscription registrations for the foundation and all Azure Recipes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.operations.output import status  # noqa: E402

# Compute and Storage support AKS nodes; the other providers appear in the templates.
PROVIDERS = (
    "Microsoft.Network",
    "Microsoft.Compute",
    "Microsoft.Storage",
    "Microsoft.ContainerService",
    "Microsoft.ManagedIdentity",
    "Microsoft.ContainerRegistry",
    "Microsoft.KeyVault",
    "Microsoft.DBforPostgreSQL",
    "Microsoft.Cache",
)
# Inherited policies can require this feature by appending FirstPartyUsage IP tags.
FEATURES: tuple[tuple[str, str], ...] = (("Microsoft.Network", "AllowBringYourOwnPublicIpAddress"),)


class PrerequisiteError(RuntimeError):
    pass


class Registrations:
    def __init__(
        self, subscription, *, runner=subprocess.run, clock=time.monotonic, sleep=time.sleep
    ):
        self.subscription = str(UUID(subscription))
        self.runner, self.clock, self.sleep = runner, clock, sleep

    def _run_az(self, *args, output):
        if args[1] == "register" and os.environ.get("CONFIRM_AZURE") != "yes":
            raise PrerequisiteError("Azure registration requires CONFIRM_AZURE=yes")
        try:
            result = self.runner(
                ["az", *args, "--subscription", self.subscription, "--output", output],
                stdout=subprocess.PIPE,
                text=True,
                check=False,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PrerequisiteError(
                f"Azure registration command unavailable or timed out: {args[0]}"
            ) from error
        if result.returncode:
            raise PrerequisiteError(
                f"{' '.join(args[:2])} failed (exit {result.returncode}); "
                "check subscription registration permissions and the Azure diagnostic above"
            )
        return result.stdout

    def az(self, *args):
        raw = self._run_az(*args, output="json")
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise PrerequisiteError("Azure registration returned invalid JSON") from error
        if not isinstance(value, dict):
            raise PrerequisiteError("Azure registration returned an invalid object")
        return value

    def provider(self, namespace, *, refresh=False):
        value = self.az("provider", "show", "--namespace", namespace)
        state = value.get("registrationState")
        if state not in {"Registered", "Registering", "NotRegistered", "Unregistered"}:
            raise PrerequisiteError(
                f"{namespace}: unexpected provider state {state!r}; resolve before bootstrap"
            )
        if refresh or state in {"NotRegistered", "Unregistered"}:
            status("progress", f"Register provider {namespace}")
            self._run_az("provider", "register", "--namespace", namespace, output="none")
            deadline = self.clock() + 300
            while True:
                state = self.az("provider", "show", "--namespace", namespace).get(
                    "registrationState"
                )
                if state in {"Registered", "Registering"}:
                    break
                if state not in {"NotRegistered", "Unregistered"} or self.clock() >= deadline:
                    raise PrerequisiteError(
                        f"{namespace}: registration incomplete ({state!r}); rerun after resolving"
                    )
                status("progress", f"{namespace}: waiting for provider registration")
                self.sleep(15)
        # Registration proceeds per region. The caller validates ARM in the selected region.
        status("success" if state == "Registered" else "progress", f"{namespace}: {state}")
        return state

    def feature(self, namespace, name):
        label = f"{namespace}/{name}"
        deadline = self.clock() + 900
        submitted = False
        while True:
            value = self.az("feature", "show", "--namespace", namespace, "--name", name)
            properties = value.get("properties")
            state = properties.get("state") if isinstance(properties, dict) else None
            if state == "Registered":
                status("success", f"{label}: Registered")
                # Refresh even on a resumed run whose feature finished after an earlier timeout.
                return self.provider(namespace, refresh=True)
            if state == "Pending":
                raise PrerequisiteError(
                    f"{label}: Pending service approval; request Azure support approval, then rerun"
                )
            if state not in {"NotRegistered", "Unregistered", "Registering"}:
                raise PrerequisiteError(
                    f"{label}: unavailable or unexpected feature state {state!r}"
                )
            if state in {"NotRegistered", "Unregistered"} and not submitted:
                status("progress", f"Register feature {label}")
                self._run_az(
                    "feature", "register", "--namespace", namespace, "--name", name, output="none"
                )
                submitted = True
            if self.clock() >= deadline:
                raise PrerequisiteError(
                    f"{label}: registration timed out; inspect feature state before retrying"
                )
            status("progress", f"{label}: {state}; waiting for Registered")
            self.sleep(15)

    def prepare(self):
        if os.environ.get("CONFIRM_AZURE") != "yes":
            raise PrerequisiteError("Azure registration requires CONFIRM_AZURE=yes")
        providers = {name: self.provider(name) for name in PROVIDERS}
        for namespace, name in FEATURES:
            providers[namespace] = self.feature(namespace, name)
        return {
            "subscriptionId": self.subscription,
            "providers": providers,
            "features": [f"{namespace}/{name}" for namespace, name in FEATURES],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(Registrations(args.subscription).prepare()))
        return 0
    except (PrerequisiteError, ValueError) as error:
        status("error", str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
