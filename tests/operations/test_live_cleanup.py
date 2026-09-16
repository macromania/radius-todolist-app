import base64
import copy
import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import NAMESPACE_DNS, uuid5

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.config import DemoConfig, initialize_config  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts/operations/local"))
spec = importlib.util.spec_from_file_location(
    "live_local_cleanup_tests", ROOT / "scripts/operations/local/cleanup.py"
)
local = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = local
spec.loader.exec_module(local)
shared = local.live_support()


def uid(name):
    return str(uuid5(NAMESPACE_DNS, name))


class Platform:
    """All Azure, Docker, Kubernetes and Radius reads/mutations are in-memory."""

    def __init__(self, config, slots, *, external=False):
        self.config, self.slots = config, list(slots)
        self.calls, self.mutations = [], []
        self.sticky_app = self.fail_radius = self.sticky_child = False
        self.leave_management_app_resources = False
        self.extra_state = False
        self.app_namespaces = True
        self.foreign = None
        self.active_fault = False
        self.active_rule = False
        self.bootstrap_lease = None
        self.clock = 0
        self.apps, self.resources, self.states, self.access = {}, {}, {}, {}
        self.payloads = {}
        self.groups, self.azure_resources, self.nodes, self.clusters = {}, {}, {}, {}
        self.roles, self.assignments, self.tombstones = [], [], []
        self.scope = f"/planes/radius/local/resourceGroups/{config.stem}"
        self.tags = {
            "project": config.project,
            "deployment": config.deployment,
            "environment": config.environment,
            "managedBy": "radius-todolist-app",
            "SecurityControl": "Ignore",
        }
        self.vault_group = "external-owner" if external else f"rg-{config.stem}-platform"
        self.vault_id = (
            self.gid(self.vault_group) + "/providers/Microsoft.KeyVault/vaults/" + config.vault_name
        )
        self.external_vault = {"name": config.vault_name, "id": self.vault_id}
        self.nodes["f" * 64] = {
            "Id": "f" * 64,
            "Name": "/unrelated",
            "Config": {"Labels": {"io.x-k8s.kind.cluster": "unrelated"}},
        }
        if config.environment == "azure":
            for name in [
                f"rg-{config.stem}-platform",
                *[
                    self.group(slot, kind)
                    for slot in shared.SLOTS
                    for kind in ("app", "cluster", "nodes")
                ],
            ]:
                self.groups[name] = {"id": self.gid(name), "name": name, "tags": dict(self.tags)}
                self.azure_resources[name] = []
        for slot in slots:
            context = config.slot_name(slot)
            role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
            index = shared.SLOTS.index(slot)
            self.nodes[str(index + 1) * 64] = {
                "Id": str(index + 1) * 64,
                "Name": "/" + context + "-control-plane",
                "Config": {"Labels": {"io.x-k8s.kind.cluster": context}, "Image": local.NODE_IMAGE},
                "State": {"Running": True},
                "NetworkSettings": {"Networks": {"kind": {"IPAddress": f"172.18.0.{index + 2}"}}},
                "HostConfig": {
                    "PortBindings": {
                        "6443/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(35495 + index)}],
                        "31480/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(35490 + index)}],
                    }
                },
            }
            self.apps[slot], self.resources[slot], self.states[slot] = [], [], {}
            self.add_app(slot, role, slot)
            kind = (
                "Applications.Datastores/redisCaches"
                if role == "data"
                else "Demo.Platform/postgreSqlDatabases"
            )
            self.add_resource(slot, role, slot, kind, "store")
            if config.environment == "azure":
                cluster = {
                    "id": self.cluster_id(slot),
                    "name": "aks-" + context,
                    "nodeResourceGroup": self.group(slot, "nodes"),
                    "provisioningState": "Succeeded",
                    "fqdn": context + ".azmk8s.io",
                    "tags": dict(self.tags),
                }
                self.clusters[slot] = cluster
                self.azure_resources[self.group(slot, "cluster")] = [
                    {
                        "id": cluster["id"],
                        "type": "Microsoft.ContainerService/managedClusters",
                        "tags": dict(self.tags),
                    }
                ]
                self.azure_resources[self.group(slot, "app")] = [
                    {
                        "id": self.gid(self.group(slot, "app"))
                        + "/providers/Microsoft.Network/applicationGateways/gateway",
                        "type": "Microsoft.Network/applicationGateways",
                        "tags": dict(self.tags),
                    }
                ]
        for slot in slots:
            if slot == "management":
                continue
            self.add_app("management", "cluster-" + slot, "provision-" + slot)
            resource = self.add_resource(
                "management", "cluster-" + slot, "provision-" + slot, "Demo.Platform/clusters", slot
            )
            resource["properties"].update(
                slot=slot,
                clusterId=self.cluster_id(slot)
                if config.environment == "azure"
                else "kind://" + config.slot_name(slot),
                clusterName="aks-" + config.slot_name(slot)
                if config.environment == "azure"
                else config.slot_name(slot),
                bootstrapAccessRef=(
                    self.cluster_id(slot)
                    if config.environment == "azure"
                    else f"kubernetes://{config.stem}-access/{config.slot_name(slot)}-access#kubeconfig"
                ),
            )
            self.access[slot] = uid(slot + "access")
        self.role_ids = {
            key: (
                f"/subscriptions/{config.subscription}/providers/"
                "Microsoft.Authorization/roleDefinitions/"
            )
            + str(
                uuid5(
                    shared.GUID_NAMESPACE,
                    f"/subscriptions/{config.subscription}-{config.stem}-{value[0]}",
                )
            )
            for key, value in shared.ROLE_NAMES.items()
        }
        if config.environment == "azure":
            platform = f"rg-{config.stem}-platform"
            for key, identifier in self.role_ids.items():
                scopes = (
                    [self.gid(platform), self.vault_id]
                    if key in {"certificateImporter", "acmeStateWriter"}
                    else [self.gid(self.group(slot, "cluster")) for slot in shared.CHILDREN]
                )
                self.roles.append(
                    {
                        "id": identifier,
                        "roleType": "CustomRole",
                        "roleName": config.stem + shared.ROLE_NAMES[key][1][len(shared.PROJECT) :],
                        "assignableScopes": scopes,
                    }
                )
            self.azure_resources[platform] = [
                {
                    "id": self.gid(platform)
                    + "/providers/Microsoft.ContainerRegistry/registries/"
                    + config.registry_name,
                    "type": "Microsoft.ContainerRegistry/registries",
                    "tags": dict(self.tags),
                },
            ]
            if not external:
                self.azure_resources[platform].append(
                    {
                        "id": self.vault_id,
                        "type": "Microsoft.KeyVault/vaults",
                        "tags": dict(self.tags),
                    }
                )
            else:
                self.assignments.append(
                    {
                        "scope": self.vault_id + "/secrets/retained",
                        "id": self.vault_id
                        + "/secrets/retained/providers/Microsoft.Authorization/roleAssignments/"
                        + uid("external"),
                        "roleDefinitionId": self.role_ids["acmeStateWriter"],
                    }
                )
        for slot, resources in self.resources.items():
            self.payloads[slot] = {
                local.backend_secret_name(resource): self.state_payload(slot, resource)
                for resource in resources
            }

    def gid(self, name):
        return f"/subscriptions/{self.config.subscription}/resourceGroups/{name}"

    def group(self, slot, kind):
        return f"rg-{self.config.slot_name(slot)}-{kind}"

    def cluster_id(self, slot):
        return (
            self.gid(self.group(slot, "cluster"))
            + "/providers/Microsoft.ContainerService/managedClusters/aks-"
            + self.config.slot_name(slot)
        )

    def rid(self, kind, name):
        return self.scope + "/providers/" + kind + "/" + name

    def profile(self, slot, *, internal=False):
        name = self.config.slot_name(slot)
        index = shared.SLOTS.index(slot)
        value = {
            "current-context": name,
            "contexts": [{"name": name, "context": {"cluster": name, "user": name}}],
            "clusters": [
                {
                    "name": name,
                    "cluster": {
                        "server": f"https://172.18.0.{index + 2}:6443"
                        if internal
                        else f"https://127.0.0.1:{35495 + index}",
                        "certificate-authority-data": base64.b64encode(
                            ("ca-" + slot).encode()
                        ).decode(),
                    },
                }
            ],
            "users": [
                {
                    "name": name,
                    "user": {
                        "client-key-data": base64.b64encode(("key-" + slot).encode()).decode(),
                        "client-certificate-data": base64.b64encode(
                            ("cert-" + slot).encode()
                        ).decode(),
                    },
                }
            ],
        }
        if internal:
            value["clusters"][0]["cluster"]["tls-server-name"] = name
        return value

    def image_parameters(self):
        return {
            "resource_prefix": self.config.stem,
            "radius_group": self.config.stem,
            "access_namespace": self.config.stem + "-access",
            "runtime_images": {
                role: {
                    "reference": f"localhost/{self.config.stem}-{role}:" + "a" * 40,
                    "image_id": "sha256:" + "b" * 64,
                }
                for role in ("api", "provisioner", "operator")
            },
            "dependency_images": [
                {"reference": local.NODE_IMAGE, "image_id": "sha256:" + "c" * 64}
            ],
        }

    def state_payload(self, slot, resource):
        def managed(kind, name, attributes):
            provider = (
                "terraform.io/builtin/terraform"
                if kind == "terraform_data"
                else "registry.terraform.io/tehcyx/kind"
                if kind == "kind_cluster"
                else "registry.terraform.io/hashicorp/random"
                if kind == "random_password"
                else "registry.terraform.io/hashicorp/kubernetes"
            )
            return {
                "module": "module.default",
                "mode": "managed",
                "type": kind,
                "name": name,
                "provider": f'provider["{provider}"]',
                "instances": [{"schema_version": 0, "attributes": attributes}],
            }

        if resource["type"] == "Demo.Platform/clusters":
            child = resource["name"]
            name = self.config.slot_name(child)
            parameters = self.image_parameters()
            images = [
                parameters["runtime_images"][role] for role in ("api", "provisioner", "operator")
            ]
            images += parameters["dependency_images"]
            resources = [
                managed(
                    "kind_cluster",
                    "child",
                    {
                        "name": name,
                        "id": name + "-" + local.NODE_IMAGE,
                        "node_image": local.NODE_IMAGE,
                        "completed": True,
                        "kubeconfig": json.dumps(self.profile(child)),
                        "client_key": "key-" + child,
                    },
                ),
                managed(
                    "kubernetes_secret_v1",
                    "access",
                    {
                        "id": self.config.stem + "-access/" + name + "-access",
                        "metadata": [
                            {"name": name + "-access", "namespace": self.config.stem + "-access"}
                        ],
                    },
                ),
                managed(
                    "terraform_data",
                    "images",
                    {
                        "id": uid("image-import"),
                        "input": None,
                        "output": None,
                        "triggers_replace": {
                            "type": [
                                "object",
                                {
                                    "cluster_id": "string",
                                    "images": [
                                        "list",
                                        ["object", {"reference": "string", "image_id": "string"}],
                                    ],
                                },
                            ],
                            "value": {
                                "cluster_id": name + "-" + local.NODE_IMAGE,
                                "images": images,
                            },
                        },
                    },
                ),
            ]
        else:
            resources = [
                managed(
                    kind,
                    name,
                    {
                        "id": self.config.namespace(slot) + "/" + physical,
                        "metadata": [{"name": physical, "namespace": self.config.namespace(slot)}],
                    }
                    if physical is not None
                    else {"id": "synthetic-password"},
                )
                for (kind, name), physical in local.APPLICATION_STATE_OWNERS[
                    resource["type"]
                ].items()
            ]
        return {
            "lineage": uid(slot + resource["id"]),
            "serial": 1,
            "terraform_version": "1.15.8",
            "resources": resources,
        }

    def add_app(self, slot, name, environment):
        self.apps[slot].append(
            {
                "id": self.rid("Applications.Core/applications", name),
                "name": name,
                "properties": {
                    "environment": self.rid("Applications.Core/environments", environment)
                },
            }
        )

    def add_resource(self, slot, app, environment, kind, name):
        value = {
            "id": self.rid(kind, name),
            "type": kind,
            "name": name,
            "properties": {
                "application": self.rid("Applications.Core/applications", app),
                "environment": self.rid("Applications.Core/environments", environment),
                "provisioningState": "Succeeded",
            },
        }
        self.resources[slot].append(value)
        self.states[slot][local.backend_secret_name(value)] = uid(value["id"] + slot)
        return value

    def slot(self, context):
        return next(slot for slot in shared.SLOTS if self.config.slot_name(slot) == context)

    def fault_document(self, slot):
        helpers = shared.fault_helpers()
        namespace = self.config.namespace(slot)
        identifier = uid(slot + "fault")
        component = slot.rsplit("-", 1)[1] + "-reconciler"
        record = {
            "version": 1,
            "project": self.config.project,
            "environment": self.config.environment,
            "slot": slot,
            "component": component,
            "run_id": "a" * 12,
            "cluster_uid": uid(self.config.slot_name(slot)),
            "namespace_uid": uid(namespace),
            "creation_attempted": True,
            "restored": False,
            "physical_restored": False,
        }
        raw = helpers.canonical_json(record)
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "plane-demo-fault-" + component,
                "namespace": namespace,
                "uid": identifier,
                "resourceVersion": "1",
                "labels": {
                    "plane-demo/project": self.config.project,
                    "plane-demo/deployment": self.config.deployment,
                    "plane-demo/environment": self.config.environment,
                    "plane-demo/journal-kind": "fault",
                },
                "ownerReferences": [
                    {
                        "apiVersion": "v1",
                        "kind": "Namespace",
                        "name": namespace,
                        "uid": uid(namespace),
                        "controller": False,
                        "blockOwnerDeletion": False,
                    }
                ],
                "annotations": {
                    "plane-demo/cluster-uid": record["cluster_uid"],
                    "plane-demo/namespace-uid": record["namespace_uid"],
                    "plane-demo/journal-uid": identifier,
                    "plane-demo/record-sha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "plane-demo/intent-sha256": helpers.intent_fingerprint(record, "fault"),
                    "plane-demo/updated-at": "2026-09-14T00:00:00+00:00",
                },
            },
            "data": {"record.json": raw},
        }

    def fault_pod(self, slot):
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        component = role + "-reconciler"
        return {
            "metadata": {
                "name": component + "-pod",
                "namespace": self.config.namespace(slot),
                "uid": uid(slot + "pod"),
                "ownerReferences": [
                    {"kind": "ReplicaSet", "name": component + "-rs", "uid": uid(slot + "rs")}
                ],
            },
            "spec": {"nodeName": self.config.slot_name(slot) + "-control-plane"},
        }

    def __call__(self, argv, **options):
        self.calls.append((argv, options))
        if argv[0] == "env":
            value = "unix:///synthetic/docker.sock"
        elif argv[:2] == ["bash", "-c"]:
            assert argv[2] == shared.OPEN_CLUSTER
            assert "demo_open_cluster" in argv[2] and "demo_open_slot" not in argv[2]
            work, slot = Path(argv[-2]), argv[-1]
            context = self.config.slot_name(slot)
            path = work / slot / "kubeconfig"
            path.parent.mkdir(mode=0o700)
            path.write_text(json.dumps(self.profile(slot)))
            path.chmod(0o600)
            value = {
                "context": context,
                "kubeconfig": str(path),
                "cluster": {"metadata": {"name": "kube-system", "uid": uid(context)}},
            }
        elif argv[0] == "az":
            assert argv[argv.index("--subscription") + 1] == self.config.subscription
            args = argv[1:]
            if args[:2] == ["group", "exists"]:
                value = args[args.index("--name") + 1] in self.groups
            elif args[:2] == ["group", "show"]:
                value = copy.deepcopy(self.groups[args[args.index("--name") + 1]])
                if self.foreign == "group":
                    value["tags"]["deployment"] = "foreign"
            elif args[:2] == ["group", "list"]:
                value = list(self.groups.values())
            elif args[:2] == ["resource", "list"]:
                value = (
                    self.azure_resources[args[args.index("--resource-group") + 1]]
                    if "--resource-group" in args
                    else [value for values in self.azure_resources.values() for value in values]
                )
            elif args[:2] == ["aks", "list"]:
                group = args[args.index("--resource-group") + 1]
                value = [
                    entry
                    for slot, entry in self.clusters.items()
                    if self.group(slot, "cluster") == group
                ]
            elif args[:2] == ["aks", "delete"]:
                assert args[args.index("--name") + 1] == "aks-" + self.config.slot_name(
                    "management"
                )
                self.mutations.append(("management-aks", "management"))
                del self.clusters["management"]
                self.azure_resources[self.group("management", "cluster")] = []
                value = None
            elif args[:2] == ["group", "delete"]:
                name = args[args.index("--name") + 1]
                assert name != "external-owner" and not name.startswith("rg-todolist-")
                self.mutations.append(("group", name))
                for item in self.azure_resources.pop(name):
                    if item["type"] == "Microsoft.KeyVault/vaults":
                        self.tombstones.append(
                            {
                                "name": self.config.vault_name,
                                "properties": {
                                    "vaultId": item["id"],
                                    "scheduledPurgeDate": "2026-12-14T00:00:00Z",
                                },
                            }
                        )
                del self.groups[name]
                value = None
            elif args[:3] == ["deployment", "sub", "show"]:
                assert args[args.index("--name") + 1] == self.config.stem + "-bootstrap"
                foundation = {
                    "projectName": self.config.project,
                    "deploymentName": self.config.deployment,
                    "environment": "azure",
                    "resourcePrefix": self.config.stem,
                    "radiusResourceGroup": self.config.stem,
                    "subscriptionId": self.config.subscription,
                    "vaultOwned": self.config.key_vault is None,
                    "vaultId": self.vault_id,
                    "vaultResourceGroup": self.vault_group,
                    "vaultPrivateEndpointId": self.gid(f"rg-{self.config.stem}-platform")
                    + f"/providers/Microsoft.Network/privateEndpoints/pe-{self.config.stem}-vault",
                    "roleDefinitionIds": self.role_ids,
                }
                outputs = {
                    "foundation": foundation,
                    "allocations": [
                        {
                            "slot": slot,
                            "clusterName": "aks-" + self.config.slot_name(slot),
                            "appResourceGroup": self.group(slot, "app"),
                            "clusterResourceGroup": self.group(slot, "cluster"),
                            "nodeResourceGroup": self.group(slot, "nodes"),
                        }
                        for slot in shared.SLOTS
                    ],
                }
                value = {
                    "properties": {
                        "provisioningState": "Succeeded",
                        "outputs": {key: {"value": val} for key, val in outputs.items()},
                    }
                }
                if hasattr(self, "bootstrap_response"):
                    value = copy.deepcopy(self.bootstrap_response)
            elif args[:3] == ["role", "definition", "list"]:
                value = (
                    self.roles
                    if "--name" not in args
                    else [
                        item
                        for item in self.roles
                        if item["id"].endswith("/" + args[args.index("--name") + 1])
                    ]
                )
            elif args[:3] == ["role", "assignment", "list"]:
                value = self.assignments
            elif args[:3] == ["role", "definition", "delete"]:
                identifier = args[args.index("--name") + 1]
                self.mutations.append(("role", identifier))
                self.roles = [
                    item for item in self.roles if not item["id"].endswith("/" + identifier)
                ]
                value = None
            elif args[:2] == ["keyvault", "list"]:
                value = [self.external_vault] if self.config.key_vault else []
            elif args[:2] == ["keyvault", "list-deleted"]:
                value = self.tombstones
            else:
                raise AssertionError(f"Unexpected Azure call: {args}")
        elif argv[0] == "rad":
            slot = self.slot(argv[argv.index("--workspace") + 1])
            assert argv[argv.index("--group") + 1] == self.config.stem
            args = argv[3:]
            if args[:2] == ["app", "list"]:
                value = self.apps[slot]
            elif args[:2] == ["env", "show"]:
                environment = args[2]
                child = (
                    environment.removeprefix("provision-")
                    if environment.startswith("provision-")
                    else None
                )
                value = {
                    "id": self.rid("Applications.Core/environments", environment),
                    "properties": {
                        "compute": {
                            "kind": "kubernetes",
                            "namespace": f"{self.config.stem}-p-{child}"
                            if child
                            else self.config.namespace(slot),
                        },
                    },
                }
                if self.config.environment == "azure":
                    value["properties"]["providers"] = {
                        "azure": {
                            "scope": self.gid(
                                self.group(child or slot, "cluster" if child else "app")
                            )
                        }
                    }
                elif child:
                    value["properties"]["recipes"] = {
                        "Demo.Platform/clusters": {
                            "default": {
                                "templateKind": "terraform",
                                "parameters": self.image_parameters(),
                            }
                        }
                    }
            elif args[:2] == ["app", "delete"]:
                if self.fail_radius:
                    return subprocess.CompletedProcess(argv, 1, "", "Radius failed")
                name = args[2]
                self.mutations.append(("app", f"{slot}/{name}"))
                if not self.sticky_app:
                    self.apps[slot] = [item for item in self.apps[slot] if item["name"] != name]
                    removed = [
                        item
                        for item in self.resources[slot]
                        if item["properties"]["application"]
                        == self.rid("Applications.Core/applications", name)
                    ]
                    self.resources[slot] = [
                        item for item in self.resources[slot] if item not in removed
                    ]
                    for item in removed:
                        self.states[slot].pop(local.backend_secret_name(item), None)
                    if (
                        self.config.environment == "azure"
                        and not name.startswith("cluster-")
                        and not (name == "management" and self.leave_management_app_resources)
                    ):
                        self.azure_resources[self.group(slot, "app")] = []
                value = None
            else:
                raise AssertionError(args)
        elif argv[0] == "kubectl":
            slot = self.slot(argv[argv.index("--context") + 1])
            action = next(word for word in argv if word in {"get", "patch"})
            args = argv[argv.index(action) + 1 :]
            if action == "patch":
                patches = json.loads(args[-1])
                assert patches[0]["path"] == "/metadata/uid"
                self.mutations.append(("quiesce", args[1]))
                value = {}
            elif args[0] == "leases.coordination.k8s.io":
                assert slot == "management" and args[1] == "management-bootstrap"
                assert argv[argv.index("-n") + 1] == self.config.namespace("management")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(self.bootstrap_lease) if self.bootstrap_lease is not None else "",
                    "",
                )
            elif args[0] == "--raw":
                value = {"value": self.resources[slot]}
            elif args[:2] == ["namespace", "kube-system"]:
                value = {
                    "metadata": {"name": "kube-system", "uid": uid(self.config.slot_name(slot))}
                }
            elif args[0] == "namespace":
                if not self.app_namespaces:
                    return subprocess.CompletedProcess(argv, 0, "", "")
                value = {
                    "metadata": {
                        "name": args[1],
                        "uid": uid(args[1]),
                        "labels": {
                            "plane-demo/project": self.config.project,
                            "plane-demo/deployment": self.config.deployment,
                            "plane-demo/environment": self.config.environment,
                        },
                    }
                }
            elif args[0] == "secrets":
                if argv[argv.index("-n") + 1] == self.config.stem + "-access":
                    output = "".join(
                        f"{self.config.slot_name(child)}-access\t{identifier}\t{child}\t"
                        f"{self.rid('Demo.Platform/clusters', child)}\n"
                        for child, identifier in self.access.items()
                    )
                    return subprocess.CompletedProcess(argv, 0, output, "")
                values = dict(self.states[slot])
                if self.extra_state:
                    values["tfstate-default-" + "f" * 40] = uid("foreign-state")
                output = "".join(
                    f"{name}\t{identifier}\ttrue\tterraform\n"
                    for name, identifier in values.items()
                )
                return subprocess.CompletedProcess(argv, 0, output, "")
            elif args[0] == "secret":
                if args[1].startswith("tfstate-default-"):
                    name = args[1]
                    encoded = base64.b64encode(
                        gzip.compress(json.dumps(self.payloads[slot][name]).encode())
                    ).decode()
                    output = "\n".join((name, "radius-system", self.states[slot][name], encoded))
                    return subprocess.CompletedProcess(argv, 0, output, "")
                child = next(
                    child
                    for child in shared.CHILDREN
                    if args[1] == self.config.slot_name(child) + "-access"
                )
                if ".data.kubeconfig" in args[-1]:
                    output = (
                        self.access[child]
                        + "\n"
                        + base64.b64encode(
                            json.dumps(self.profile(child, internal=True)).encode()
                        ).decode()
                    )
                    return subprocess.CompletedProcess(argv, 0, output, "")
                output = (
                    "\n".join(
                        (
                            args[1],
                            self.config.stem + "-access",
                            self.access[child],
                            child,
                            self.rid("Demo.Platform/clusters", child),
                        )
                    )
                    if child in self.access
                    else ""
                )
                return subprocess.CompletedProcess(argv, 0, output, "")
            elif args[0] == "pods" and self.active_rule and slot == "shared-data":
                value = {"items": [self.fault_pod(slot)]}
            elif args[0] == "deployment":
                component = args[1]
                value = {
                    "metadata": {
                        "name": component,
                        "namespace": self.config.namespace(slot),
                        "uid": uid(slot + "deployment"),
                    },
                    "spec": {
                        "template": {
                            "metadata": {
                                "labels": {
                                    "plane-demo/project": self.config.project,
                                    "plane-demo/deployment": self.config.deployment,
                                    "plane-demo/environment": self.config.environment,
                                    "plane-demo/component": component,
                                }
                            }
                        }
                    },
                }
            elif args[0] == "replicaset":
                value = {
                    "metadata": {
                        "uid": uid(slot + "rs"),
                        "ownerReferences": [
                            {"kind": "Deployment", "uid": uid(slot + "deployment")}
                        ],
                    }
                }
            elif args[0] == "configmaps" and self.active_fault and slot == "shared-data":
                value = {"items": [self.fault_document(slot)]}
            elif args[0] == "configmap":
                value = self.fault_document(slot)
            elif args[0] == "deployments":
                value = {
                    "items": [
                        {
                            "metadata": {
                                "name": component,
                                "namespace": self.config.namespace("management"),
                                "uid": uid(component),
                                "labels": {
                                    "plane-demo/project": self.config.project,
                                    "plane-demo/component": component,
                                },
                            },
                            "spec": {"replicas": 1},
                        }
                        for component in ("management-api", "provisioner")
                    ]
                }
            else:
                assert args[0] in {
                    "configmaps",
                    "jobs",
                    "pods",
                    "networkpolicies.networking.k8s.io",
                    "ciliumnetworkpolicies.cilium.io",
                }
                value = {"items": []}
        elif argv[0] == "docker":
            assert argv[1:3] == ["--host", "unix:///synthetic/docker.sock"]
            if argv[3] == "ps":
                return subprocess.CompletedProcess(argv, 0, "\n".join(self.nodes), "")
            if argv[3] == "inspect":
                value = [self.nodes[identifier] for identifier in argv[6:]]
            else:
                assert argv[3] == "exec"
                slot = next(
                    slot
                    for slot in shared.SLOTS
                    if self.nodes[argv[4]]["Name"]
                    == "/" + self.config.slot_name(slot) + "-control-plane"
                )
                command = argv[5:]
                if command[:2] == ["crictl", "pods"]:
                    metadata = {
                        key: self.fault_pod(slot)["metadata"][key]
                        for key in ("name", "namespace", "uid")
                    }
                    value = {
                        "items": [{"id": "a" * 64, "metadata": metadata}]
                        if self.active_rule and slot == "shared-data"
                        else []
                    }
                elif command[:2] == ["crictl", "inspectp"]:
                    metadata = {
                        key: self.fault_pod(slot)["metadata"][key]
                        for key in ("name", "namespace", "uid")
                    }
                    value = {
                        "status": {
                            "id": "a" * 64,
                            "metadata": metadata,
                            "state": "SANDBOX_READY",
                            "labels": {"io.kubernetes.pod.uid": metadata["uid"]},
                        },
                        "info": {"pid": 123},
                    }
                elif command[0] == "stat":
                    return subprocess.CompletedProcess(argv, 0, "4026532000\n", "")
                else:
                    assert command[:2] == ["bash", "-ceu"]
                    assert 'exec 3<"/proc/$pid/ns/net"' in command[2]
                    assert command[4:6] == ["123", "4026532000"]
                    assert command[6:] == ["iptables", "-w", "2", "-S", "OUTPUT"]
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        "-P OUTPUT ACCEPT\n"
                        "-A OUTPUT -m comment --comment plane-demo-fault-foreign -j DROP\n",
                        "",
                    )
        elif argv[0] == "kind":
            assert argv[1:3] == ["delete", "cluster"]
            assert argv[argv.index("--name") + 1] == self.config.slot_name("management")
            self.mutations.append(("management-kind", "management"))
            self.nodes = {
                key: value
                for key, value in self.nodes.items()
                if value["Config"]["Labels"].get("io.x-k8s.kind.cluster")
                != self.config.slot_name("management")
            }
            value = None
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(value) if value is not None else "", ""
        )

    @contextmanager
    def sdk(self, *, config_file, context):
        assert context == self.config.slot_name("management")
        assert Path(config_file).is_file()

        def call_api(path, method, **kwargs):
            assert method == "DELETE"
            assert kwargs["query_params"] == [("api-version", "2025-08-01-preview")]
            child = path.rsplit("/", 1)[1]
            assert child in shared.CHILDREN
            self.mutations.append(("radius-child", child))
            if not self.sticky_child:
                resource = next(
                    item
                    for item in self.resources["management"]
                    if item["type"] == "Demo.Platform/clusters" and item["name"] == child
                )
                self.resources["management"].remove(resource)
                self.states["management"].pop(local.backend_secret_name(resource))
                self.access.pop(child, None)
                self.nodes = {
                    key: value
                    for key, value in self.nodes.items()
                    if value["Config"]["Labels"].get("io.x-k8s.kind.cluster")
                    != self.config.slot_name(child)
                }
                self.clusters.pop(child, None)
                if self.config.environment == "azure":
                    self.azure_resources[self.group(child, "cluster")] = []
            return {}, 202, {}

        from types import SimpleNamespace

        yield SimpleNamespace(
            configuration=SimpleNamespace(verify_ssl=True, proxy=None), call_api=call_api
        )


@pytest.fixture(params=["azure", "local"])
def world(tmp_path, monkeypatch, request):
    from kubernetes import config as kube

    monkeypatch.setattr(shared, "ROOT", tmp_path)
    environment = request.param.removesuffix("-external")
    selected = DemoConfig(
        environment,
        "demo",
        "team",
        "11111111-1111-1111-1111-111111111111" if environment == "azure" else None,
        "centralus" if environment == "azure" else None,
        key_vault="external-shared-vault" if request.param.endswith("-external") else None,
    )
    initialize_config(selected, tmp_path / ".env")
    platform = Platform(selected, shared.SLOTS, external=request.param.endswith("-external"))
    monkeypatch.setattr(kube, "new_client_from_config", platform.sdk)
    monkeypatch.setenv("CONFIRM_AZURE" if environment == "azure" else "CONFIRM_LOCAL", "yes")
    engine = (shared.LiveAzureCleanup if environment == "azure" else local.LiveLocalCleanup)(
        execute=True,
        runner=platform,
        clock=lambda: platform.clock,
        sleep=lambda seconds: setattr(platform, "clock", platform.clock + seconds),
    )
    yield engine, platform, tmp_path
    engine.close()


def test_normal_cleanup_obeys_radius_owners_without_saved_files(world):
    engine, platform, root = world
    platform.app_namespaces = False
    result = engine.clean()
    assert result["status"] == "clean"
    apps = [name for kind, name in platform.mutations if kind == "app"]
    assert apps[:4] == [
        "shared-data/data",
        "isolated-1-data/data",
        "shared-control/control",
        "isolated-1-control/control",
    ]
    children = [name for kind, name in platform.mutations if kind == "radius-child"]
    assert children == list(shared.CHILDREN)
    assert apps[-1] == "management/management"
    assert not (root / ".state").exists()
    assert not any(
        "MANAGEMENT_DSN" in str(argv) or "endpoints.json" in str(argv) for argv, _ in platform.calls
    )
    if engine.environment == "local":
        assert "f" * 64 in platform.nodes
        assert result["unrelatedPreservationVerified"] is True
    else:
        assert result["softDeletedVaults"] and result["purged"] is False


def failed_bootstrap(platform, state="Failed", outputs=None):
    config = platform.config
    return {
        "id": (
            f"/subscriptions/{config.subscription}/providers/Microsoft.Resources/"
            f"deployments/{config.stem}-bootstrap"
        ),
        "properties": {
            "provisioningState": state,
            "parameters": {
                key: {"value": value}
                for key, value in {
                    "projectName": config.project,
                    "deploymentName": config.deployment,
                    "environment": "azure",
                    "location": config.location,
                    "registryName": config.registry_name,
                    "vaultName": config.vault_name,
                    "deploymentHash": config.identity_hash,
                    "externalVaultResourceGroup": platform.vault_group if config.key_vault else "",
                }.items()
            },
            "outputs": outputs,
        },
    }


def remove_clusters_and_apps(platform):
    platform.clusters.clear()
    for slot in shared.SLOTS:
        for kind in ("cluster", "app", "nodes"):
            platform.azure_resources[platform.group(slot, kind)] = []


@pytest.mark.parametrize("world", ["azure"], indirect=True)
@pytest.mark.parametrize("state", ["Failed", "Canceled"])
@pytest.mark.parametrize("outputs", [None, {}, {"foundation": {"value": None}}])
def test_partial_bootstrap_cleans_owned_foundation_without_radius(world, state, outputs):
    engine, platform, _ = world
    remove_clusters_and_apps(platform)
    platform.bootstrap_response = failed_bootstrap(platform, state, outputs)
    engine.execute = False
    preview = engine.clean()
    assert preview["status"] == "planned" and platform.mutations == []
    engine.execute = True
    assert engine.clean()["status"] == "clean"
    assert not any(argv[0] in {"rad", "kubectl", "bash"} for argv, _ in platform.calls)
    assert engine.clean()["status"] == "clean"


@pytest.mark.parametrize("world", ["azure"], indirect=True)
def test_failed_bootstrap_rerun_still_deletes_existing_apps_through_radius(world):
    engine, platform, _ = world
    platform.bootstrap_response = failed_bootstrap(platform)
    assert engine.clean()["status"] == "clean"
    kinds = [kind for kind, _ in platform.mutations]
    assert kinds.index("app") < kinds.index("radius-child") < kinds.index("management-aks")


@pytest.mark.parametrize("world", ["azure"], indirect=True)
@pytest.mark.parametrize("failure", ["Succeeded", "Running", "foreign", "malformed", "missing"])
def test_invalid_bootstrap_metadata_blocks_cleanup_without_traceback(world, failure, capsys):
    engine, platform, _ = world
    remove_clusters_and_apps(platform)
    response = failed_bootstrap(platform)
    if failure in {"Succeeded", "Running"}:
        response["properties"]["provisioningState"] = failure
    elif failure == "foreign":
        response["properties"]["parameters"]["deploymentName"]["value"] = "foreign"
    elif failure == "malformed":
        response["properties"]["outputs"] = []
    else:
        response["properties"] = None
    platform.bootstrap_response = response
    assert shared.main(["--execute"], engine_factory=lambda **kwargs: engine) == 1
    output = capsys.readouterr()
    assert "Cleanup incomplete" in output.err and "Traceback" not in output.err
    assert platform.mutations == []


@pytest.mark.parametrize("world", ["azure"], indirect=True)
def test_partial_bootstrap_refuses_orphaned_nodes(world):
    engine, platform, _ = world
    remove_clusters_and_apps(platform)
    platform.bootstrap_response = failed_bootstrap(platform)
    group = platform.group("management", "nodes")
    platform.azure_resources[group] = [
        {
            "id": platform.gid(group) + "/providers/Microsoft.Compute/virtualMachines/orphan",
            "type": "Microsoft.Compute/virtualMachines",
            "tags": platform.tags,
        }
    ]
    with pytest.raises(shared.CleanupError, match="without their application/AKS owner"):
        engine.clean()
    assert platform.mutations == []


@pytest.mark.parametrize("world", ["azure"], indirect=True)
def test_bootstrap_restarting_between_inventory_and_delete_blocks_mutation(world):
    engine, platform, _ = world
    remove_clusters_and_apps(platform)
    platform.bootstrap_response = failed_bootstrap(platform)
    reads = 0

    def runner(argv, **kwargs):
        nonlocal reads
        if argv[:4] == ["az", "deployment", "sub", "show"]:
            reads += 1
            if reads > 1:
                platform.bootstrap_response["properties"]["provisioningState"] = "Running"
        return platform(argv, **kwargs)

    engine.runner = runner
    with pytest.raises(shared.CleanupError, match="nonterminal"):
        engine.clean()
    assert platform.mutations == []


@pytest.mark.parametrize(
    "target,confirmed,expected",
    [
        ("clean-plan", False, "planned"),
        ("clean-azure", True, "clean"),
        ("clean-azure", False, None),
    ],
)
def test_real_make_cleanup_path_handles_null_outputs_offline(tmp_path, target, confirmed, expected):
    config = DemoConfig(
        "azure", "demo", "team", "11111111-1111-1111-1111-111111111111", "centralus"
    )
    initialize_config(config, tmp_path / ".env")
    for relative in (
        "Makefile",
        "scripts/lib/env.sh",
        "scripts/lib/output.sh",
        "scripts/lib/progress.sh",
        "scripts/operations/stage.sh",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    binary = tmp_path / "bin"
    binary.mkdir()
    uv = binary / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, runpy, sys\nfrom pathlib import Path\n"
        "assert sys.argv[1:5] == ['run','--no-sync','python','scripts/operations/clean-azure.py']\n"
        f"test = runpy.run_path({str(Path(__file__).resolve())!r})\n"
        "shared = test['shared']; shared.ROOT = Path.cwd()\n"
        "config = shared.load_config(Path.cwd()/'.env')\n"
        "platform = test['Platform'](config, shared.SLOTS)\n"
        "test['remove_clusters_and_apps'](platform)\n"
        "platform.bootstrap_response = test['failed_bootstrap'](platform)\n"
        "def factory(**kwargs):\n"
        "    return shared.LiveAzureCleanup(runner=platform, **kwargs)\n"
        "raise SystemExit(shared.main(sys.argv[5:], engine_factory=factory))\n"
    )
    uv.chmod(0o700)
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            target,
            "COLOR=always",
            f"CONFIRM_AZURE={'yes' if confirmed else ''}",
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"], "NO_COLOR": ""},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode == 0) == (expected is not None), result.stderr
    if expected:
        assert json.loads(result.stdout)["status"] == expected
        assert "\033[" in result.stderr and "\033" not in result.stdout
    else:
        assert "CONFIRM_AZURE" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("failure", ["sticky_app", "fail_radius", "sticky_child"])
def test_radius_failure_never_falls_through_to_provider_deletion(world, failure):
    engine, platform, _ = world
    setattr(platform, failure, True)
    with pytest.raises((shared.CleanupError, local.LocalError)):
        engine.clean()
    assert not any(
        kind in {"management-kind", "management-aks", "group", "role"}
        for kind, _ in platform.mutations
    )


def test_preview_inventory_does_not_mutate(world):
    engine, platform, _ = world
    engine.execute = False
    assert engine.clean()["status"] == "planned"
    assert platform.mutations == []


def test_missing_management_owner_is_not_a_direct_child_delete_permission(world):
    engine, platform, _ = world
    platform.resources["management"] = [
        item
        for item in platform.resources["management"]
        if item["type"] != "Demo.Platform/clusters"
    ]
    with pytest.raises((shared.CleanupError, local.LocalError), match="Radius owner"):
        engine.clean()
    assert platform.mutations == []


def test_independent_verification_uses_only_platform_inventory(world):
    engine, platform, root = world
    engine.clean()
    platform.calls.clear()
    platform.mutations.clear()
    independent = type(engine)(runner=platform)
    try:
        assert independent.verify()["status"] == "clean"
    finally:
        independent.close()
    assert platform.mutations == []
    assert not any(argv[0] in {"kubectl", "rad", "bash"} for argv, _ in platform.calls)
    assert not (root / ".state").exists()


@pytest.mark.parametrize("world", ["local"], indirect=True)
def test_unexpected_terraform_state_blocks_local_cleanup(world):
    engine, platform, _ = world
    platform.extra_state = True
    with pytest.raises((shared.CleanupError, local.LocalError), match="backend"):
        engine.clean()
    assert platform.mutations == []


@pytest.mark.parametrize("world", ["azure"], indirect=True)
def test_foreign_azure_group_blocks_all_cleanup(world):
    engine, platform, _ = world
    platform.foreign = "group"
    with pytest.raises(shared.CleanupError, match="ownership"):
        engine.clean()
    assert platform.mutations == []


@pytest.mark.parametrize("world", ["azure-external"], indirect=True)
def test_external_vault_and_external_assignments_are_retained_without_reconfiguration(world):
    engine, platform, _ = world
    vault = copy.deepcopy(platform.external_vault)
    assignments = copy.deepcopy(platform.assignments)
    result = engine.clean()
    assert result["status"] == "clean"
    assert platform.external_vault == vault and platform.assignments == assignments
    retained = {value["id"] for value in result["retainedExternalObjects"]}
    assert vault["id"] in retained
    assert assignments[0]["id"] in retained
    assert platform.role_ids["acmeStateWriter"] in retained
    assert result["purged"] is False
    assert not any(
        argv[:2] == ["az", "keyvault"]
        and any(action in argv for action in ("delete", "purge", "update", "set-policy"))
        for argv, _ in platform.calls
    )


def test_active_fault_journal_blocks_normal_cleanup_before_any_mutation(world):
    engine, platform, _ = world
    platform.active_fault = True
    with pytest.raises(shared.CleanupError, match="fault journal"):
        engine.clean()
    assert platform.mutations == []


def test_normal_public_entrypoints_and_verification_need_no_record(world, capsys):
    engine, platform, _ = world
    entry = shared.main if engine.environment == "azure" else local.main

    def factory(execute=False):
        return type(engine)(
            execute=execute,
            runner=platform,
            clock=lambda: platform.clock,
            sleep=lambda seconds: setattr(platform, "clock", platform.clock + seconds),
        )

    assert entry([], engine_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert platform.mutations == []
    assert entry(["--execute"], engine_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clean"
    platform.calls.clear()
    platform.mutations.clear()
    result = (
        shared.main([], verify_only=True, engine_factory=factory)
        if engine.environment == "azure"
        else local.main(["--verify"], engine_factory=factory)
    )
    assert result == 0
    assert json.loads(capsys.readouterr().out)["scope"] == "owned-active-resources"
    assert not any(argv[0] in {"rad", "kubectl", "bash"} for argv, _ in platform.calls)
    assert platform.mutations == []


def test_environment_mismatch_or_missing_confirmation_stops_before_live_reads(world, monkeypatch):
    engine, platform, root = world
    confirmation = "CONFIRM_AZURE" if engine.environment == "azure" else "CONFIRM_LOCAL"
    monkeypatch.delenv(confirmation)
    with pytest.raises(shared.CleanupError, match="CONFIRM"):
        type(engine)(execute=True, runner=platform)
    other = "local" if engine.environment == "azure" else "azure"
    initialize_config(
        DemoConfig(
            other,
            "demo",
            "team",
            "11111111-1111-1111-1111-111111111111" if other == "azure" else None,
            "centralus" if other == "azure" else None,
        ),
        root / ".env",
    )
    with pytest.raises(shared.CleanupError, match="environment"):
        type(engine)(runner=platform)
    assert platform.calls == []


def test_verification_fails_when_owned_resources_still_exist(world):
    engine, _, _ = world
    with pytest.raises((shared.CleanupError, local.LocalError), match="remain"):
        engine.verify()


def test_management_only_cleanup_does_not_require_nonexistent_child_inventories(world):
    original, _, _ = world
    platform = Platform(original.config, ("management",))
    engine = type(original)(execute=True, runner=platform)
    try:
        assert engine.clean()["status"] == "clean"
    finally:
        engine.close()
    assert not any(kind == "radius-child" for kind, _ in platform.mutations)
    assert not any("acceptance.json" in str(argv) for argv, _ in platform.calls)


@pytest.mark.parametrize("world", ["azure"], indirect=True)
def test_app_resources_without_management_application_block_before_mutation(world):
    engine, platform, _ = world
    platform.apps["management"] = [
        item for item in platform.apps["management"] if item["name"] != "management"
    ]
    platform.resources["management"] = [
        item
        for item in platform.resources["management"]
        if not item["properties"]["application"].endswith("/management")
    ]
    with pytest.raises(shared.CleanupError, match="application owner"):
        engine.clean()
    assert platform.mutations == []


def test_environment_change_during_cleanup_revokes_next_mutation(world):
    engine, platform, root = world
    original = platform.__call__
    changed = False

    def execute(argv, **options):
        nonlocal changed
        result = original(argv, **options)
        if not changed and argv[0] == "rad" and "list" in argv:
            changed = True
            initialize_config(DemoConfig("local", "other", "deployment"), root / ".env")
        return result

    engine.runner = execute
    with pytest.raises(shared.CleanupError, match="configuration changed"):
        engine.clean()
    assert platform.mutations == []


@pytest.mark.parametrize("world", ["local"], indirect=True)
def test_local_cleanup_detects_live_fault_rules_without_a_host_or_cluster_journal(world):
    engine, platform, _ = world
    platform.active_rule = True
    with pytest.raises(local.LocalError, match="Active parent fault"):
        engine.clean()
    assert platform.mutations == []
    assert any("iptables" in argv for argv, _ in platform.calls)


def cluster_payload(platform, child="shared-data"):
    resource = next(
        item
        for item in platform.resources["management"]
        if item["type"] == "Demo.Platform/clusters" and item["name"] == child
    )
    return platform.payloads["management"][local.backend_secret_name(resource)]


@pytest.mark.parametrize("world", ["local"], indirect=True)
@pytest.mark.parametrize(
    "change",
    [
        "foreign-kind",
        "extra-managed",
        "foreign-access",
        "foreign-provider",
        "tainted-kind",
        "wrong-image-trigger",
        "wrong-credentials",
        "app-extra-kind",
        "app-foreign-namespace",
    ],
)
def test_normal_local_entrypoint_rejects_unowned_terraform_payloads(world, capsys, change):
    engine, platform, _ = world
    state = cluster_payload(platform)
    kind = next(item for item in state["resources"] if item["type"] == "kind_cluster")
    attrs = kind["instances"][0]["attributes"]
    if change == "foreign-kind":
        attrs.update(name="unrelated-cluster", id="unrelated-cluster-" + local.NODE_IMAGE)
    elif change == "extra-managed":
        state["resources"].append(copy.deepcopy(kind))
    elif change == "foreign-access":
        access = next(item for item in state["resources"] if item["type"] == "kubernetes_secret_v1")
        access["instances"][0]["attributes"]["metadata"][0]["namespace"] = "foreign"
    elif change == "foreign-provider":
        kind["provider"] = 'provider["registry.terraform.io/tehcyx/kind"].foreign'
    elif change == "tainted-kind":
        kind["instances"][0]["status"] = "tainted"
    elif change == "wrong-image-trigger":
        images = next(item for item in state["resources"] if item["type"] == "terraform_data")
        images["instances"][0]["attributes"]["triggers_replace"]["value"]["cluster_id"] = "foreign"
    elif change == "wrong-credentials":
        attrs["client_key"] = "must-not-appear-in-output"
    else:
        state = next(iter(platform.payloads["shared-data"].values()))
        if change == "app-extra-kind":
            state["resources"].append(copy.deepcopy(kind))
        else:
            item = next(
                item for item in state["resources"] if item["type"] == "kubernetes_service_v1"
            )
            item["instances"][0]["attributes"]["metadata"][0]["namespace"] = "foreign"
    assert local.main(["--execute"], engine_factory=lambda **_: engine) == 1
    output = capsys.readouterr()
    assert "Terraform" in output.err
    assert "must-not-appear-in-output" not in output.out + output.err
    assert platform.mutations == []


@pytest.mark.parametrize("world", ["local"], indirect=True)
@pytest.mark.parametrize("boundary", ["app", "child"])
def test_normal_path_revalidates_payload_with_unchanged_secret_uid_immediately_before_delete(
    world, monkeypatch, boundary, capsys
):
    engine, platform, _ = world
    original = engine.note
    changed = False

    def note(action, identity):
        nonlocal changed
        original(action, identity)
        trigger = action == ("radius-app" if boundary == "app" else "radius-child")
        if trigger and not changed:
            changed = True
            if boundary == "child":
                state = cluster_payload(platform)
                kind = next(item for item in state["resources"] if item["type"] == "kind_cluster")
                kind["instances"][0]["attributes"]["name"] = "foreign-cluster"
            else:
                state = next(iter(platform.payloads["shared-data"].values()))
                state["resources"].append(copy.deepcopy(cluster_payload(platform)["resources"][0]))

    monkeypatch.setattr(engine, "note", note)
    assert local.main(["--execute"], engine_factory=lambda **_: engine) == 1
    assert changed
    assert "Terraform" in capsys.readouterr().err
    assert not any(kind == "radius-child" for kind, _ in platform.mutations)
    if boundary == "app":
        assert not any(kind == "app" for kind, _ in platform.mutations)


@pytest.mark.parametrize("world", ["azure"], indirect=True)
@pytest.mark.parametrize(
    "remaining_type",
    [
        None,
        "Microsoft.DBforPostgreSQL/flexibleServers",
        "Microsoft.Network/applicationGateways",
    ],
)
def test_radius_only_checks_management_provider_resource_absence_before_success(
    world, capsys, remaining_type
):
    engine, platform, _ = world
    group = platform.group("management", "app")
    if remaining_type:
        platform.leave_management_app_resources = True
        identifier = platform.gid(group) + "/providers/" + remaining_type + "/leftover"
        platform.azure_resources[group] = [
            {"id": identifier, "type": remaining_type, "tags": dict(platform.tags)}
        ]
    result = shared.main(["--radius-only", "--execute"], engine_factory=lambda **_: engine)
    output = capsys.readouterr()
    if remaining_type:
        assert result == 1
        assert identifier in output.err
        assert "radius_resources_removed" not in output.out
    else:
        assert result == 0
        assert json.loads(output.out)["status"] == "radius_resources_removed"
    assert not any(kind in {"group", "role", "management-aks"} for kind, _ in platform.mutations)


def management_bootstrap_lease(platform):
    return {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "name": "management-bootstrap",
            "namespace": platform.config.namespace("management"),
            "uid": uid("management-bootstrap"),
            "resourceVersion": "1",
            "labels": {
                "plane-demo/project": platform.config.project,
                "plane-demo/deployment": platform.config.deployment,
                "plane-demo/environment": "local",
                "plane-demo/operator": "management-deploy",
            },
        },
        "spec": {"holderIdentity": uid("bootstrap-holder")},
    }


@pytest.mark.parametrize("world", ["local"], indirect=True)
@pytest.mark.parametrize("holder", ["active", "interrupted", "empty"])
def test_normal_cleanup_refuses_extant_bootstrap_lease_without_mutation(world, holder, capsys):
    engine, platform, _ = world
    lease = management_bootstrap_lease(platform)
    if holder == "interrupted":
        lease["spec"].update(renewTime="2000-01-01T00:00:00Z", leaseDurationSeconds=1)
    elif holder == "empty":
        lease["spec"]["holderIdentity"] = ""
    platform.bootstrap_lease = lease
    before = copy.deepcopy(lease)
    assert local.main(["--execute"], engine_factory=lambda **_: engine) == 1
    assert "Active or interrupted management bootstrap Lease" in capsys.readouterr().err
    assert platform.mutations == []
    assert platform.bootstrap_lease == before


@pytest.mark.parametrize("world", ["local"], indirect=True)
@pytest.mark.parametrize(
    "boundary", ["quiesce", "radius-app", "radius-child", "bootstrap-management-kind"]
)
def test_cleanup_rechecks_bootstrap_lease_before_destructive_boundaries(
    world, monkeypatch, capsys, boundary
):
    engine, platform, _ = world
    note = engine.note
    before = None

    def acquire(action, identity):
        nonlocal before
        note(action, identity)
        if action == boundary and before is None:
            before = list(platform.mutations)
            platform.bootstrap_lease = management_bootstrap_lease(platform)

    monkeypatch.setattr(engine, "note", acquire)
    assert local.main(["--execute"], engine_factory=lambda **_: engine) == 1
    assert "Active or interrupted management bootstrap Lease" in capsys.readouterr().err
    assert before is not None and platform.mutations == before
    assert platform.bootstrap_lease is not None
