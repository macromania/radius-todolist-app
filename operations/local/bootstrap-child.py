#!/usr/bin/env python3
"""Run inside the management gate Pod: verified child access and child Radius bootstrap."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

CHILD = "radplanes-local-shared-control"
CONTEXT = CHILD
WORK = Path("/work")
KUBECONFIG = WORK / "home/.kube/config"
ACCESS = Path("/access/kubeconfig")
PASSWORD = Path("/password/password")
CHILD_NAMESPACE = "radplanes-local-child-gate-harmless"
PG_PROBE = """
if failure=$(PGPASSWORD=deliberately-wrong psql -Atc "SELECT 1" 2>&1); then
    echo "Password authentication was bypassed" >&2
    exit 1
fi
case "$failure" in
    *"password authentication failed"*) ;;
    *) echo "Unexpected authentication failure" >&2; exit 1 ;;
esac
psql -v ON_ERROR_STOP=1 -Atc "SELECT 'child-to-parent-password-authenticated';"
"""


class ChildError(RuntimeError):
    pass


def run(args: list[str], *, data: dict | None = None, timeout: int = 300) -> str:
    result = subprocess.run(
        args,
        input=json.dumps(data) if data is not None else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        (WORK / "diagnostic.txt").write_text(result.stderr + result.stdout)
        raise ChildError(
            f"{args[0]} failed (exit {result.returncode}); private /work/diagnostic.txt"
        )
    return result.stdout


def kube(*args: str) -> list[str]:
    return [
        "kubectl",
        "--kubeconfig",
        str(KUBECONFIG),
        "--context",
        CONTEXT,
        "--request-timeout=30s",
        *args,
    ]


def rad(*args: str, workspace: bool = True) -> list[str]:
    return [
        "rad",
        "--config",
        str(WORK / "radius.yaml"),
        *args,
        *(["--workspace", CHILD] if workspace else []),
    ]


def create_resource(kind: str, name: str, properties: dict) -> None:
    path = WORK / f"{name}.json"
    path.write_text(json.dumps({"location": "global", "properties": properties}))
    run(rad("resource", "create", kind, name, "--from-file", str(path)))


def fixtures(inputs: dict, password: str) -> list[dict]:
    metadata = {"namespace": CHILD_NAMESPACE}
    manager_type = (
        "type.googleapis.com/envoy.extensions.filters.network."
        "http_connection_manager.v3.HttpConnectionManager"
    )
    router_type = "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"
    response_body = f"local-cluster-gate:{inputs['runId']}"
    envoy_config = {
        "static_resources": {
            "listeners": [
                {
                    "name": "gate",
                    "address": {"socket_address": {"address": "0.0.0.0", "port_value": 10080}},
                    "filter_chains": [
                        {
                            "filters": [
                                {
                                    "name": "envoy.filters.network.http_connection_manager",
                                    "typed_config": {
                                        "@type": manager_type,
                                        "stat_prefix": "gate",
                                        "route_config": {
                                            "name": "gate",
                                            "virtual_hosts": [
                                                {
                                                    "name": "gate",
                                                    "domains": ["*"],
                                                    "routes": [
                                                        {
                                                            "match": {"prefix": "/"},
                                                            "direct_response": {
                                                                "status": 200,
                                                                "body": {
                                                                    "inline_string": response_body
                                                                },
                                                            },
                                                        }
                                                    ],
                                                }
                                            ],
                                        },
                                        "http_filters": [
                                            {
                                                "name": "envoy.filters.http.router",
                                                "typed_config": {
                                                    "@type": router_type,
                                                },
                                            }
                                        ],
                                    },
                                }
                            ]
                        }
                    ],
                }
            ]
        },
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {**metadata, "name": "parent-pg"},
            "type": "Opaque",
            "data": {"password": base64.b64encode(password.encode()).decode()},
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {**metadata, "name": "parent-pg-probe"},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 90,
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "probe",
                                "image": inputs["postgresImage"],
                                "command": ["sh", "-ec"],
                                "args": [PG_PROBE],
                                "env": [
                                    {"name": "PGHOST", "value": inputs["parentAddress"]},
                                    {"name": "PGPORT", "value": "31543"},
                                    {"name": "PGUSER", "value": "gate"},
                                    {"name": "PGDATABASE", "value": "gate"},
                                    {"name": "PGSSLMODE", "value": "disable"},
                                    {"name": "PGCONNECT_TIMEOUT", "value": "10"},
                                    {
                                        "name": "PGPASSWORD",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": "parent-pg",
                                                "key": "password",
                                            }
                                        },
                                    },
                                ],
                                "securityContext": {
                                    "runAsUser": 65532,
                                    "runAsNonRoot": True,
                                    "allowPrivilegeEscalation": False,
                                },
                            }
                        ],
                    }
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {**metadata, "name": "gate-envoy"},
            "data": {"envoy.json": json.dumps(envoy_config)},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {**metadata, "name": "gate-envoy"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "gate-envoy"}},
                "template": {
                    "metadata": {"labels": {"app": "gate-envoy"}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "envoy",
                                "image": inputs["envoyImage"],
                                "args": [
                                    "-c",
                                    "/config/envoy.json",
                                    "--log-level",
                                    "warning",
                                    "--concurrency",
                                    "1",
                                ],
                                "readinessProbe": {"tcpSocket": {"port": 10080}},
                                "securityContext": {
                                    "runAsUser": 65532,
                                    "runAsNonRoot": True,
                                    "allowPrivilegeEscalation": False,
                                },
                                "resources": {
                                    "requests": {"cpu": "50m", "memory": "64Mi"},
                                    "limits": {"cpu": "500m", "memory": "128Mi"},
                                },
                                "volumeMounts": [
                                    {
                                        "name": "config",
                                        "mountPath": "/config",
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                        "volumes": [{"name": "config", "configMap": {"name": "gate-envoy"}}],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {**metadata, "name": "gate-envoy"},
            "spec": {
                "type": "NodePort",
                "selector": {"app": "gate-envoy"},
                "ports": [{"port": 10080, "targetPort": 10080, "nodePort": 31480}],
            },
        },
    ]


def bootstrap(inputs: dict) -> dict:
    KUBECONFIG.parent.mkdir(parents=True, mode=0o700)
    access = json.loads(ACCESS.read_text())
    cluster = access["clusters"][0]["cluster"]
    if (
        cluster.get("insecure-skip-tls-verify")
        or not cluster.get("certificate-authority-data")
        or cluster.get("tls-server-name") != CHILD
        or cluster["server"] != f"https://{inputs['childAddress']}:6443"
        or access["current-context"] != CONTEXT
    ):
        raise ChildError("Recipe access does not contain the expected verified child endpoint")
    KUBECONFIG.write_text(json.dumps(access))
    os.environ["HOME"] = str(WORK / "home")
    os.environ["KUBECONFIG"] = str(KUBECONFIG)
    if run(kube("get", "--raw=/readyz")).strip() != "ok":
        raise ChildError("Child API is not ready")
    negative = subprocess.run(
        kube("--tls-server-name=not-the-child.invalid", "get", "--raw=/readyz"),
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    if negative.returncode == 0 or "x509" not in negative.stderr.lower():
        raise ChildError("Wrong certificate name did not produce the required TLS rejection")
    run(
        rad(
            "install",
            "kubernetes",
            "--kubecontext",
            CONTEXT,
            "--skip-contour-install",
            "--set",
            "dashboard.enabled=false",
            "--set",
            "global.terraform.enabled=false",
            workspace=False,
        ),
        timeout=600,
    )
    run(
        rad(
            "workspace",
            "create",
            "kubernetes",
            CHILD,
            "--context",
            CONTEXT,
            workspace=False,
        )
    )
    run(rad("group", "create", "radplanes-local"))
    run(
        rad(
            "environment",
            "create",
            "child-gate",
            "--group",
            "radplanes-local",
            "--kubernetes-namespace",
            "radplanes-local-child-gate",
        )
    )
    run(
        rad(
            "workspace",
            "create",
            "kubernetes",
            CHILD,
            "--context",
            CONTEXT,
            "--group",
            "radplanes-local",
            "--environment",
            "child-gate",
            "--force",
            workspace=False,
        )
    )
    scope = "/planes/radius/local/resourceGroups/radplanes-local/providers/Applications.Core"
    create_resource(
        "Applications.Core/applications",
        "harmless",
        {
            "environment": f"{scope}/environments/child-gate",
        },
    )
    create_resource(
        "Applications.Core/containers",
        "harmless",
        {
            "environment": f"{scope}/environments/child-gate",
            "application": f"{scope}/applications/harmless",
            "container": {"image": inputs["postgresImage"], "command": ["sleep", "3600"]},
        },
    )
    run(
        kube(
            "-n",
            CHILD_NAMESPACE,
            "rollout",
            "status",
            "deployment/harmless",
            "--timeout=180s",
        )
    )
    for deployment in ("ucp", "applications-rp", "dynamic-rp"):
        run(
            kube(
                "-n",
                "radius-system",
                "rollout",
                "status",
                f"deployment/{deployment}",
                "--timeout=180s",
            )
        )
    objects = fixtures(inputs, PASSWORD.read_text())
    run(kube("create", "-f", "-"), data={"apiVersion": "v1", "kind": "List", "items": objects})
    run(
        kube(
            "-n",
            CHILD_NAMESPACE,
            "wait",
            "--for=condition=complete",
            "job/parent-pg-probe",
            "--timeout=100s",
        ),
        timeout=120,
    )
    pg_result = run(kube("-n", CHILD_NAMESPACE, "logs", "job/parent-pg-probe")).strip()
    if pg_result != "child-to-parent-password-authenticated":
        raise ChildError("Child-to-parent SQL query returned an unexpected result")
    run(
        kube(
            "-n",
            CHILD_NAMESPACE,
            "rollout",
            "status",
            "deployment/gate-envoy",
            "--timeout=180s",
        )
    )
    return {
        "runId": inputs["runId"],
        "childTLS": True,
        "wrongServerNameRejected": True,
        "childRadius": True,
        "radiusWorkload": True,
        "parentPostgreSQL": {"authenticated": True, "tlsRequired": False, "nodePort": 31543},
        "envoyReady": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        print("Preview only: bootstrap one child from inside the management gate Pod.")
        raise SystemExit(0)
    os.umask(0o077)
    try:
        print(json.dumps(bootstrap(json.loads(Path("/scripts/inputs.json").read_text()))))
    except (ChildError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"Child bootstrap failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
