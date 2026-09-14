#!/usr/bin/env python3
"""Prepare a static Recipe archive and manifests; no cluster or Docker operations."""

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import sys
import tarfile
from pathlib import Path

from common import (
    ENVIRONMENT_ID,
    PYTHON_IMAGE,
    ROOT,
    STATE,
    LocalError,
    digest,
    image_names,
    write_private,
)

MODULE_FILES = (
    ".terraform.lock.hcl",
    "terraform.tf",
    "providers.tf",
    "variables.tf",
    "locals.tf",
    "main.tf",
    "outputs.tf",
    "node-address.sh",
    "load-images.sh",
)
RECIPE_FILES = {
    "cluster": MODULE_FILES,
    **{
        name: tuple(path for path in MODULE_FILES if not path.endswith(".sh"))
        for name in ("postgresql", "redis", "gateway")
    },
}
RECIPE_TYPES = {
    "cluster": "Demo.Platform/clusters",
    "postgresql": "Demo.Platform/postgreSqlDatabases",
    "redis": "Applications.Datastores/redisCaches",
    "gateway": "Demo.Platform/gateways",
}


def source_bytes(path: Path) -> bytes:
    if not path.is_relative_to(ROOT):
        raise LocalError("Recipe sources must stay inside the repository")
    for entry in (path, *path.parents):
        if not entry.is_relative_to(ROOT):
            break
        if entry.is_symlink():
            raise LocalError(f"Missing or symlinked Recipe source: {path.relative_to(ROOT)}")
    if not path.is_file():
        raise LocalError(f"Missing or symlinked Recipe source: {path.relative_to(ROOT)}")
    return path.read_bytes()


def module_archive(recipe: str = "cluster") -> bytes:
    if recipe not in RECIPE_FILES:
        raise LocalError(f"Unknown local Recipe: {recipe}")
    root = ROOT / "infra/radius/recipes/local" / recipe
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name in RECIPE_FILES[recipe]:
                path = (
                    ROOT / "scripts/recipes/local" / recipe / name
                    if name.endswith(".sh")
                    else root / name
                )
                data = source_bytes(path)
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o644
                archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def manifests(archive: bytes) -> tuple[list[dict], dict, str]:
    sha = digest(archive)
    server_path = ROOT / "scripts/operations/local/module-server.py"
    server_code = source_bytes(server_path).decode()
    name = f"local-module-{digest(archive + server_code.encode())[:20]}"
    labels = {"app": name}
    url = f"http://{name}.radius-system.svc.cluster.local:18080/{sha}.tar.gz"
    objects = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": "radius-system"},
            "immutable": True,
            "binaryData": {"archive.tar.gz": base64.b64encode(archive).decode()},
            "data": {"server.py": server_code},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": "radius-system"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 65532},
                        "containers": [
                            {
                                "name": "module",
                                "image": PYTHON_IMAGE,
                                "command": ["python3", "/module/server.py"],
                                "env": [{"name": "MODULE_SHA256", "value": sha}],
                                "ports": [{"containerPort": 18080}],
                                "readinessProbe": {
                                    "httpGet": {"path": f"/{sha}.tar.gz", "port": 18080},
                                },
                                "securityContext": {
                                    "readOnlyRootFilesystem": True,
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": {"cpu": "25m", "memory": "32Mi"},
                                    "limits": {"cpu": "200m", "memory": "64Mi"},
                                },
                                "volumeMounts": [
                                    {
                                        "name": "module",
                                        "mountPath": "/module",
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                        "volumes": [{"name": "module", "configMap": {"name": name}}],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": "radius-system"},
            "spec": {"selector": labels, "ports": [{"port": 18080, "targetPort": 18080}]},
        },
    ]
    environment = {
        "location": "global",
        "properties": {
            "compute": {
                "kind": "kubernetes",
                "resourceId": "self",
                "namespace": "radplanes-local-gate",
            },
            "recipes": {
                "Demo.Platform/clusters": {
                    "default": {"templateKind": "terraform", "templatePath": url},
                },
            },
            "recipeConfig": {
                "env": {
                    "DOCKER_HOST": "unix:///run/radplanes/docker.sock",
                    "KIND_EXPERIMENTAL_PROVIDER": "docker",
                    "KIND_EXPERIMENTAL_DOCKER_NETWORK": "kind",
                },
            },
        },
    }
    return objects, environment, name


def recipe_manifests() -> tuple[list[dict], dict, dict]:
    """Return server objects, Radius Recipe mappings, and public per-module identities."""
    objects, recipes, modules = [], {}, {}
    for recipe, resource_type in RECIPE_TYPES.items():
        archive = module_archive(recipe)
        server_objects, environment, name = manifests(archive)
        template = environment["properties"]["recipes"]["Demo.Platform/clusters"]["default"]
        objects.extend(server_objects)
        recipes[resource_type] = {"default": template}
        modules[recipe] = {
            "url": template["templatePath"],
            "sha256": digest(archive),
            "moduleServer": name,
            "resourceType": resource_type,
        }
    return objects, recipes, modules


def recipe_bundle() -> dict:
    """All content is source-only; no CLI, live state, or credential reads."""
    objects, recipes, modules = recipe_manifests()
    source_paths = [
        *sorted((ROOT / "infra/radius/apps").glob("*.bicep")),
        *sorted((ROOT / "infra/radius/modules").glob("*.bicep")),
        *sorted((ROOT / "infra/radius/types").glob("*.yaml")),
        *sorted((ROOT / "infra/radius/recipes/azure").glob("*.bicep")),
    ]
    return {
        "objects": objects,
        "recipes": recipes,
        "modules": modules,
        "sharedSourceHashes": {
            str(path.relative_to(ROOT)): digest(source_bytes(path)) for path in source_paths
        },
        "liveStatus": "not-run",
    }


def prepare() -> dict:
    archive = module_archive()
    objects, environment, name = manifests(archive)
    write_private(STATE / "prepared/module.tar.gz", archive)
    write_private(
        STATE / "prepared/module-server.json",
        {
            "apiVersion": "v1",
            "kind": "List",
            "items": objects,
        },
    )
    write_private(STATE / "prepared/environment.json", environment)
    write_private(
        STATE / "prepared/child.json",
        {
            "location": "global",
            "properties": {"environment": ENVIRONMENT_ID, "slot": "shared-control"},
        },
    )
    summary = {
        "moduleSHA256": digest(archive),
        "moduleServer": name,
        "images": image_names(),
        "liveStatus": "not-run",
    }
    write_private(STATE / "prepared/inputs.json", summary)
    return summary


if __name__ == "__main__":
    os.umask(0o077)
    try:
        print(json.dumps(prepare(), indent=2))
    except (LocalError, OSError, ValueError) as error:
        print(f"Preparation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
