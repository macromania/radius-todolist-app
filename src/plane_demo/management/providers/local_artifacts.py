"""Shared prepared local Recipe contracts and manifests. No platform command execution."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

TYPES = {
    "cluster": "Demo.Platform/clusters",
    "postgresql": "Demo.Platform/postgreSqlDatabases",
    "redis": "Applications.Datastores/redisCaches",
    "gateway": "Demo.Platform/gateways",
}
SLOTS = ("management", "shared-control", "shared-data", "isolated-1-control", "isolated-1-data")
TF_INIT = "/opt/radplanes/bootstrap/terraform-init.py"


def require(condition: object, code: str) -> None:
    if not condition:
        raise ValueError(code)


def source_archives(root: Path) -> tuple[dict[str, bytes], bytes]:
    sys.path.insert(0, str(root / "scripts/operations/local"))
    spec = importlib.util.spec_from_file_location(
        "source_recipe_prepare", root / "scripts/operations/local/prepare.py"
    )
    require(spec is not None and spec.loader is not None, "recipe_packager_missing")
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    prepare.ROOT = root
    server = prepare.source_bytes(root / "scripts/operations/local/module-server.py")
    return {kind: prepare.module_archive(kind) for kind in TYPES}, server


def package(root: Path, destination: Path, revision: str) -> dict:
    require(re.fullmatch(r"[a-f0-9]{40}", revision), "invalid_source_revision")
    archives, server = source_archives(root)
    modules = {}
    for kind in TYPES:
        archive = archives[kind]
        directory = destination / "modules" / kind
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "archive.tar.gz").write_bytes(archive)
        sha = hashlib.sha256(archive).hexdigest()
        name = "local-module-" + hashlib.sha256(archive + server).hexdigest()[:20]
        modules[kind] = {
            "resourceType": TYPES[kind],
            "moduleServer": name,
            "sha256": sha,
            "reference": f"http://{name}.radius-system.svc.cluster.local:18080/{sha}.tar.gz",
        }
    (destination / "module-server.py").write_bytes(server)
    (destination / "terraform-init.py").write_bytes(
        (root / "scripts/operations/local/terraform-init.py").read_bytes()
    )
    value = {"version": 1, "revision": revision, "modules": modules}
    (destination / "modules.json").write_text(json.dumps(value))
    return value


def setup_plan(
    directory: Path, inputs: dict, prefix: str, group: str, access_namespace: str, node_address: str
) -> dict:
    bundle = prepared(directory)
    require(
        inputs["revision"] == bundle["revision"] and inputs["stem"] == prefix,
        "prepared_source_mismatch",
    )
    objects, recipes = [], {}
    for kind in TYPES:
        value = descriptor(bundle, kind, prefix, group, inputs)
        objects += module_objects(value, inputs)
        module = value["module"]
        recipes[kind] = {
            "reference": module["reference"],
            "digest": "sha256:" + module["sha256"],
            "moduleServer": module["moduleServer"],
        }
    return {
        "objects": objects,
        "recipes": recipes,
        "environment": environment(
            prefix,
            group,
            access_namespace,
            "management",
            recipes,
            inputs,
            node_address,
            all_recipes=True,
        ),
        "terraform": terraform_patch(inputs["images"]["operator"]["reference"], "applications-rp"),
    }


def prepared(directory: Path) -> dict:
    value = json.loads((directory / "modules.json").read_text())
    require(
        isinstance(value, dict)
        and value.get("version") == 1
        and isinstance(value.get("modules"), dict)
        and set(value["modules"]) == set(TYPES),
        "prepared_recipes_missing",
    )
    require(re.fullmatch(r"[a-f0-9]{40}", value.get("revision", "")), "invalid_source_revision")
    server = (directory / "module-server.py").read_bytes()
    for kind, module in value["modules"].items():
        archive = (directory / "modules" / kind / "archive.tar.gz").read_bytes()
        sha = hashlib.sha256(archive).hexdigest()
        name = "local-module-" + hashlib.sha256(archive + server).hexdigest()[:20]
        require(
            module
            == {
                "resourceType": TYPES[kind],
                "moduleServer": name,
                "sha256": sha,
                "reference": f"http://{name}.radius-system.svc.cluster.local:18080/{sha}.tar.gz",
            },
            "prepared_recipe_digest_mismatch",
        )
    value["dependencies"] = json.loads((directory / "images.json").read_text())
    require(
        isinstance(value["dependencies"], list) and value["dependencies"],
        "prepared_dependencies_missing",
    )
    return value


def labels(prefix: str, group: str, kind: str) -> dict:
    require(
        re.fullmatch(r"[a-z][a-z0-9-]{0,23}[a-z0-9]", prefix) and group == prefix,
        "recipe_identity_mismatch",
    )
    return {
        "plane-demo/resource-prefix": prefix,
        "plane-demo/radius-group": group,
        "plane-demo/recipe": kind,
    }


def descriptor(bundle: dict, kind: str, prefix: str, group: str, inputs: dict) -> dict:
    require(kind in TYPES, "unknown_recipe")
    operator = inputs["images"]["operator"]
    require(
        operator["reference"] == f"localhost/{prefix}-operator:{bundle['revision']}"
        and re.fullmatch(r"sha256:[a-f0-9]{64}", operator["id"]),
        "operator_image_mismatch",
    )
    for role in ("api", "provisioner"):
        image = inputs["images"][role]
        require(
            image["reference"] == f"localhost/{prefix}-{role}:{bundle['revision']}"
            and re.fullmatch(r"sha256:[a-f0-9]{64}", image["id"]),
            "application_image_mismatch",
        )
    dependencies = inputs["dependencies"]
    require(dependencies == bundle["dependencies"], "prepared_dependencies_mismatch")
    return {
        "version": 1,
        "kind": kind,
        "revision": bundle["revision"],
        "resourcePrefix": prefix,
        "radiusGroup": group,
        "module": bundle["modules"][kind],
    }


def module_objects(value: dict, inputs: dict) -> list[dict]:
    kind, module = value["kind"], value["module"]
    name = module["moduleServer"]
    owner = labels(value["resourcePrefix"], value["radiusGroup"], kind)
    selector = {"app": name}
    metadata = {"name": name, "namespace": "radius-system", "labels": owner}
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata,
            "immutable": True,
            "data": {"module.json": json.dumps(value, sort_keys=True)},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": metadata,
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": selector},
                "template": {
                    "metadata": {"labels": {**selector, **owner}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 65532},
                        "containers": [
                            {
                                "name": "module",
                                "image": inputs["images"]["operator"]["reference"],
                                "imagePullPolicy": "Never",
                                "command": ["python3", "/opt/radplanes/bootstrap/module-server.py"],
                                "env": [
                                    {"name": "MODULE_SHA256", "value": module["sha256"]},
                                    {
                                        "name": "MODULE_ROOT",
                                        "value": f"/opt/radplanes/bootstrap/modules/{kind}",
                                    },
                                ],
                                "ports": [{"containerPort": 18080}],
                                "readinessProbe": {
                                    "httpGet": {
                                        "path": f"/{module['sha256']}.tar.gz",
                                        "port": 18080,
                                    }
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
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": metadata,
            "spec": {
                "selector": selector,
                "ports": [{"port": 18080, "targetPort": 18080}],
            },
        },
    ]


def module_descriptor(
    module: dict, recipe: dict, kind: str, prefix: str, group: str, bundle: dict
) -> dict:
    value = json.loads(module["data"]["module.json"])
    require(
        value["version"] == 1
        and value["kind"] == kind
        and value["resourcePrefix"] == prefix
        and value["radiusGroup"] == group
        and value["revision"] == bundle["revision"]
        and value["module"] == bundle["modules"][kind]
        and module.get("immutable") is True
        and module["metadata"]["name"] == recipe["moduleServer"]
        and module["metadata"]["namespace"] == "radius-system"
        and recipe["reference"] == value["module"]["reference"]
        and recipe["digest"] == "sha256:" + value["module"]["sha256"],
        "local_recipe_owner_mismatch",
    )
    require(
        all(
            module["metadata"].get("labels", {}).get(key) == expected
            for key, expected in labels(prefix, group, kind).items()
        ),
        "local_recipe_owner_mismatch",
    )
    require("images" not in value and "dependencies" not in value, "module_inventory_refused")
    return value


def matches(expected, actual, *, patch: bool = False) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            (patch and value is None and key not in actual)
            or (key in actual and matches(value, actual[key], patch=patch))
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if (
            patch
            and isinstance(actual, list)
            and all(isinstance(item, dict) and "name" in item for item in expected)
        ):
            return all(
                any(
                    isinstance(candidate, dict)
                    and candidate.get("name") == item["name"]
                    and matches(item, candidate, patch=True)
                    for candidate in actual
                )
                for item in expected
            )
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(
                matches(left, right, patch=patch)
                for left, right in zip(expected, actual, strict=True)
            )
        )
    return expected == actual


def child_images(value: dict) -> list[dict]:
    images = [
        *(value["images"][role] for role in ("api", "provisioner", "operator")),
        *value["dependencies"],
    ]
    references = [image["reference"] for image in images]
    require(len(references) == len(set(references)), "duplicate_prepared_image")
    return [{"reference": image["reference"], "image_id": image["id"]} for image in images]


def environment_namespace(prefix: str, slot: str, *, cluster: bool = False) -> str:
    require(slot in SLOTS, "unknown_slot")
    labels(prefix, prefix, "cluster" if cluster else "gateway")
    namespace = f"{prefix}-p-{SLOTS.index(slot)}" if cluster else f"{prefix}-{slot}"
    application = (
        f"cluster-{slot}"
        if cluster
        else "management"
        if slot == "management"
        else slot.rsplit("-", 1)[1]
    )
    # Radius 0.60.2 appends the application name to the environment namespace.
    require(len(f"{namespace}-{application}") <= 63, "application_namespace_too_long")
    return namespace


def binding_inputs(value: dict, prefix: str, group: str) -> dict:
    """Read deployment inputs from the management environment, without Docker or local inventory."""
    scope = f"/planes/radius/local/resourceGroups/{group}/providers/Applications.Core"
    require(
        isinstance(value, dict) and isinstance(value.get("id"), str),
        "management_environment_mismatch",
    )
    require(
        prefix == group
        and value.get("id", "").lower() == (f"{scope}/environments/management".lower()),
        "management_environment_mismatch",
    )
    properties = value["properties"]
    compute = properties["compute"]
    require(
        compute["kind"] == "kubernetes"
        and compute["resourceId"] == "self"
        and compute["namespace"] == environment_namespace(prefix, "management"),
        "management_environment_mismatch",
    )
    recipes = properties["recipes"]
    for resource_type in TYPES.values():
        binding = recipes[resource_type]["default"]
        require(
            binding["templateKind"] == "terraform"
            and re.fullmatch(
                r"http://local-module-[a-f0-9]{20}\.radius-system\.svc\.cluster\.local:"
                r"18080/[a-f0-9]{64}\.tar\.gz",
                binding["templatePath"],
            )
            and binding["parameters"]["resource_prefix"] == prefix
            and binding["parameters"]["radius_group"] == group,
            "management_recipe_binding_mismatch",
        )
    parameters = recipes[TYPES["cluster"]]["default"]["parameters"]
    require(parameters["access_namespace"] == prefix + "-access", "access_namespace_mismatch")
    runtime = parameters["runtime_images"]
    require(
        isinstance(runtime, dict) and set(runtime) == {"api", "provisioner", "operator"},
        "runtime_images_missing",
    )
    images, revisions = {}, set()
    for role, image in runtime.items():
        match = re.fullmatch(
            rf"localhost/{re.escape(prefix)}-{role}:([a-f0-9]{{40}})", image["reference"]
        )
        require(
            match and re.fullmatch(r"sha256:[a-f0-9]{64}", image["image_id"]),
            "runtime_image_mismatch",
        )
        revisions.add(match[1])
        images[role] = {"reference": image["reference"], "id": image["image_id"]}
    require(len(revisions) == 1, "runtime_image_revision_mismatch")
    dependencies = parameters["dependency_images"]
    require(isinstance(dependencies, list) and dependencies, "dependency_images_missing")
    for image in dependencies:
        require(
            re.fullmatch(
                r"(?:ghcr\.io/radius-project|docker\.io/(?:library|envoyproxy)|kindest)"
                r"/[a-zA-Z0-9._/-]+(?::[a-zA-Z0-9._-]+)?(?:@sha256:[a-f0-9]{64})?",
                image["reference"],
            )
            and re.fullmatch(r"sha256:[a-f0-9]{64}", image["image_id"]),
            "dependency_image_mismatch",
        )
    inputs = {
        "revision": revisions.pop(),
        "stem": prefix,
        "images": images,
        "dependencies": [
            {"reference": image["reference"], "id": image["image_id"]} for image in dependencies
        ],
    }
    child_images(inputs)
    return inputs


def environment(
    prefix: str,
    group: str,
    access_namespace: str,
    slot: str,
    recipes: dict,
    inputs: dict,
    node_address: str | None,
    *,
    cluster: bool = False,
    all_recipes: bool = False,
) -> dict:
    require(slot in SLOTS and access_namespace == prefix + "-access", "recipe_identity_mismatch")
    common = {"resource_prefix": prefix, "radius_group": group}
    parameters = {
        "cluster": {
            **common,
            "access_namespace": access_namespace,
            "runtime_images": {
                role: {
                    "reference": inputs["images"][role]["reference"],
                    "image_id": inputs["images"][role]["id"],
                }
                for role in ("api", "provisioner", "operator")
            },
            "dependency_images": [
                {"reference": image["reference"], "image_id": image["id"]}
                for image in inputs["dependencies"]
            ],
        },
        "postgresql": {**common, "node_address": node_address},
        "redis": common,
        "gateway": {**common, "gateway_host_port": 35490 + SLOTS.index(slot)},
    }
    kinds = (
        list(TYPES)
        if all_recipes
        else (
            ["cluster"]
            if cluster
            else ["gateway", "redis" if slot.endswith("-data") else "postgresql"]
        )
    )
    return {
        "location": "global",
        "properties": {
            "compute": {
                "kind": "kubernetes",
                "resourceId": "self",
                "namespace": environment_namespace(prefix, slot, cluster=cluster),
            },
            "recipes": {
                TYPES[kind]: {
                    "default": {
                        "templateKind": "terraform",
                        "templatePath": recipes[kind]["reference"],
                        "parameters": parameters[kind],
                    }
                }
                for kind in kinds
            },
            "recipeConfig": {
                "env": {
                    "DOCKER_HOST": "unix:///run/radplanes/docker.sock",
                    "KIND_EXPERIMENTAL_PROVIDER": "docker",
                    "KIND_EXPERIMENTAL_DOCKER_NETWORK": "kind",
                }
                if cluster
                else {}
            },
        },
    }


def terraform_patch(operator: str, name: str) -> dict:
    return {
        "spec": {
            "strategy": {"type": "Recreate", "rollingUpdate": None},
            "template": {
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {"fsGroup": 65532, "fsGroupChangePolicy": "OnRootMismatch"},
                    "initContainers": [
                        {
                            "name": "local-terraform-layout",
                            "image": operator,
                            "imagePullPolicy": "Never",
                            "command": ["python3", TF_INIT],
                            "securityContext": {
                                "runAsUser": 0,
                                "runAsGroup": 0,
                                "runAsNonRoot": False,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"], "add": ["CHOWN"]},
                            },
                            "volumeMounts": [{"name": "terraform", "mountPath": "/terraform"}],
                        }
                    ],
                    "containers": [
                        {
                            "name": name,
                            "imagePullPolicy": "Never",
                            "env": [
                                {"name": "RADIUS_LOGGING_LEVEL", "value": "error"},
                                {
                                    "name": "TF_CLI_CONFIG_FILE",
                                    "value": "/terraform/terraform.tfrc",
                                },
                                {"name": "CHECKPOINT_DISABLE", "value": "1"},
                            ],
                            "volumeMounts": [
                                {
                                    "name": "local-radius-service-account",
                                    "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "local-radius-service-account",
                            "projected": {
                                "defaultMode": 0o440,
                                "sources": [
                                    {
                                        "serviceAccountToken": {
                                            "path": "token",
                                            "expirationSeconds": 3600,
                                        }
                                    },
                                    {
                                        "configMap": {
                                            "name": "kube-root-ca.crt",
                                            "items": [{"key": "ca.crt", "path": "ca.crt"}],
                                        }
                                    },
                                    {
                                        "downwardAPI": {
                                            "items": [
                                                {
                                                    "path": "namespace",
                                                    "fieldRef": {
                                                        "apiVersion": "v1",
                                                        "fieldPath": "metadata.namespace",
                                                    },
                                                }
                                            ]
                                        }
                                    },
                                ],
                            },
                        }
                    ],
                },
            },
        }
    }


def live_config(
    project: str,
    deployment: str,
    inputs: dict,
    plan: dict,
    node: dict,
    namespace: dict,
    service: dict,
    ca: str,
) -> dict:
    from uuid import UUID

    from plane_demo.management.providers.identity import DemoConfig
    from plane_demo.management.providers.local_config import private_ipv4

    identity = DemoConfig("local", project, deployment, revision=inputs["revision"])
    prefix = identity.stem
    require(
        inputs["stem"] == prefix
        and node["metadata"]["name"] == prefix + "-management-control-plane",
        "management_node_mismatch",
    )
    addresses = [
        item["address"] for item in node["status"]["addresses"] if item["type"] == "InternalIP"
    ]
    require(
        len(addresses) == 1
        and namespace["metadata"]["name"] == "kube-system"
        and service["metadata"]["name"] == "kubernetes"
        and service["metadata"]["namespace"] == "default",
        "management_identity_mismatch",
    )
    certificate = base64.b64decode(ca, validate=True)
    require(certificate, "management_ca_missing")
    return {
        "version": 1,
        "provider": "local",
        "projectName": project,
        "bootstrapIdentity": identity.public_values(),
        "allocations": {
            slot: {
                "slot": slot,
                "clusterName": identity.slot_name(slot),
                "context": identity.slot_name(slot),
                "gatewayPort": 35490 + index,
                "apiPort": 35495 + index,
            }
            for index, slot in enumerate(SLOTS)
        },
        "recipes": plan["recipes"],
        "images": {
            role: {
                "reference": inputs["images"][role]["reference"],
                "imageId": inputs["images"][role]["id"],
            }
            for role in ("api", "provisioner")
        },
        "managementCluster": {
            "clusterId": f"kind://{prefix}-management",
            "uid": str(UUID(namespace["metadata"]["uid"])),
            "nodeAddress": private_ipv4(addresses[0]),
            "serviceAddress": private_ipv4(service["spec"]["clusterIP"]),
            "caSHA256": hashlib.sha256(certificate).hexdigest(),
        },
        "interfaceSources": {
            "environment": (
                f"/planes/radius/local/resourceGroups/{prefix}"
                "/providers/Applications.Core/environments/management"
            ),
            "modules": {kind: item["moduleServer"] for kind, item in plan["recipes"].items()},
            "operator": inputs["images"]["operator"],
        },
    }
