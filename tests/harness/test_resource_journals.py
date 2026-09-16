import copy
import ipaddress
import json
import shlex
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from test_acceptance import Clock, faults
from test_live_discovery import IMAGE_ID, REVISION, SOURCE, NativeCommands, runner, uid

from scripts.operations.config import DemoConfig, initialize_config

LocalFault = faults.fault_class(SimpleNamespace(environment="local", live=False))
network = sys.modules[LocalFault.__module__]


class FaultCommands(NativeCommands):
    """Native CLI/API boundary double. It never starts a platform process."""

    def __init__(self, root, config):
        super().__init__(root, config)
        self.clock = Clock()
        self.policies = {}
        self.rules = {}
        self.extra_rules = {}
        self.extra_policies = {}
        self.deleted = []
        self.after_create_error = False
        self.drift = None
        self.pod_generation = {}
        self.old_sandboxes = {}
        self.replicas = {}
        self.probes = []

    def slot(self, context):
        return next(slot for slot in faults.LOCAL_SLOTS if self.config.slot_name(slot) == context)

    def node(self, slot):
        index = faults.LOCAL_SLOTS.index(slot)
        return {
            "Id": str(index + 1) * 64,
            "Name": "/" + self.config.slot_name(slot) + "-control-plane",
            "Config": {
                "Image": network.NODE_IMAGE,
                "Labels": {
                    "io.x-k8s.kind.cluster": self.config.slot_name(slot),
                    "io.x-k8s.kind.role": "control-plane",
                },
            },
            "State": {"Running": True},
            "HostConfig": {
                "PortBindings": {
                    "6443/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(35495 + index)}],
                    "31480/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(35490 + index)}],
                }
            },
            "NetworkSettings": {"Networks": {"kind": {"IPAddress": f"172.18.0.{index + 2}"}}},
        }

    def pod(self, slot, component):
        role = "provisioner" if component == "provisioner" else "api"
        image = (
            f"localhost/{self.config.stem}-{role}:{REVISION}"
            if self.config.environment == "local"
            else self.config.registry_name + f".azurecr.io/plane-{role}@" + IMAGE_ID
        )
        generation = self.pod_generation.get((slot, component), 0)
        return {
            "metadata": {
                **self.metadata(component + "-pod"),
                "namespace": self.config.namespace(slot),
                "uid": uid(slot + component + str(generation)),
                "labels": {**self.metadata("")["labels"], "plane-demo/component": component},
                "ownerReferences": [
                    {
                        "kind": "ReplicaSet",
                        "name": component + "-rs",
                        "uid": uid(slot + component + "rs"),
                    }
                ],
            },
            "spec": {
                "nodeName": self.node(slot)["Name"][1:],
                "serviceAccountName": "data-api-runtime" if component == "data-api" else component,
                "containers": [{"name": component, "image": image}],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": component,
                        "ready": True,
                        "imageID": IMAGE_ID,
                        "containerID": "containerd://" + "c" * 64,
                    }
                ],
            },
        }

    def postgres(self, slot):
        role = "management" if slot == "management" else "control"
        prefix = f"/planes/radius/local/resourceGroups/{self.config.stem}/providers"
        return {
            "id": prefix + "/Demo.Platform/postgreSqlDatabases/postgres",
            "properties": {
                "application": prefix + "/Applications.Core/applications/" + role,
                "environment": prefix + "/Applications.Core/environments/" + slot,
                "provisioningState": "Succeeded",
                "database": role,
                "host": self.node(slot)["NetworkSettings"]["Networks"]["kind"]["IPAddress"]
                if self.config.environment == "local"
                else f"pg-{slot}.postgres.database.azure.com",
                "port": 31543 if self.config.environment == "local" else 5432,
                "tlsRequired": self.config.environment == "azure",
                "serverId": f"kubernetes://{self.config.namespace(slot)}/statefulsets/postgres"
                if self.config.environment == "local"
                else (
                    f"/subscriptions/{self.config.subscription}/resourceGroups/"
                    f"rg-{self.config.slot_name(slot)}/providers/"
                    f"Microsoft.DBforPostgreSQL/flexibleServers/pg-{slot}"
                ),
            },
        }

    def subnet(self, slot):
        return (
            f"/subscriptions/{self.config.subscription}/resourceGroups/"
            f"rg-{self.config.stem}-platform/providers/Microsoft.Network/"
            f"virtualNetworks/vnet-{self.config.stem}/subnets/snet-{slot}-postgresql"
        )

    def component(self, argv):
        selector = argv[argv.index("-l") + 1]
        return next(
            item.split("=", 1)[1]
            for item in selector.split(",")
            if item.startswith("plane-demo/component=")
        )

    def __call__(self, argv, **options):
        if argv[0] == "kubectl":
            slot = self.slot(argv[argv.index("--context") + 1])
            offset = next(
                (
                    i
                    for i, word in enumerate(argv)
                    if word in {"get", "exec", "create", "patch", "rollout", "logs"}
                ),
                None,
            )
            action = argv[offset] if offset is not None else None
            args = argv[offset + 1 :] if offset is not None else []
            if action in {"rollout", "logs"}:
                self.calls.append((argv, options))
                output = faults.utc_now() + " INFO control_poll examined=2 succeeded=2 failed=0\n"
                return subprocess.CompletedProcess(argv, 0, output, "")
            elif action == "patch":
                assert args[0] == "deployment"
                changes = json.loads(args[-1])
                assert changes[0]["value"] == uid(slot + args[1] + "deployment")
                assert changes[1]["value"] == self.replicas.get((slot, args[1]), 1)
                self.replicas[(slot, args[1])] = changes[2]["value"]
                value = {}
            elif action == "get" and args[0] == "--raw":
                value = self.postgres(slot)
                if self.drift == "radius":
                    value["properties"]["application"] += "-foreign"
            elif action == "get" and args[0] == "statefulset":
                value = {
                    "metadata": {
                        **self.metadata("postgres"),
                        "namespace": self.config.namespace(slot),
                    }
                }
            elif action == "get" and args[0] == "deployment":
                name = args[1]
                value = {
                    "metadata": {
                        "name": name,
                        "namespace": self.config.namespace(slot),
                        "uid": uid(slot + name + "deployment"),
                    },
                    "spec": {
                        "replicas": self.replicas.get((slot, name), 1),
                        "template": {
                            "spec": {"terminationGracePeriodSeconds": 0},
                            "metadata": {
                                "labels": {
                                    **self.metadata("")["labels"],
                                    "plane-demo/component": name,
                                }
                            },
                        },
                    },
                }
            elif action == "get" and args[0] == "pods":
                component = self.component(argv)
                value = {
                    "items": [self.pod(slot, component)]
                    if self.replicas.get((slot, component), 1)
                    else []
                }
            elif action == "get" and args[0] == "replicaset":
                component = args[1].removesuffix("-rs")
                value = {
                    "metadata": {
                        "uid": uid(slot + component + "rs"),
                        "ownerReferences": [
                            {"kind": "Deployment", "uid": uid(slot + component + "deployment")}
                        ],
                    }
                }
            elif action == "get" and args[0] == "customresourcedefinition":
                value = {
                    "spec": {"versions": [{"name": "v2", "served": True}]},
                    "status": {"conditions": [{"type": "Established", "status": "True"}]},
                }
            elif action == "get" and args[0] in (
                "networkpolicies.networking.k8s.io",
                faults.POLICY_RESOURCE,
            ):
                policies = list(self.policies.get(slot, {}).values())
                if args[0] == "networkpolicies.networking.k8s.io":
                    policies = self.extra_policies.get(slot, [])
                if len(args) > 1 and not args[1].startswith("-"):
                    value = next(
                        (item for item in policies if item["metadata"]["name"] == args[1]), None
                    )
                    self.calls.append((argv, options))
                    return subprocess.CompletedProcess(
                        argv, 0, json.dumps(value) if value else "", ""
                    )
                value = {"items": policies}
            elif (
                action == "create" and json.loads(options["input"])["kind"] == "CiliumNetworkPolicy"
            ):
                value = json.loads(options["input"])
                value["metadata"]["uid"] = uid(value["metadata"]["name"])
                self.policies.setdefault(slot, {})[value["metadata"]["name"]] = value
                if self.after_create_error:
                    self.calls.append((argv, options))
                    return subprocess.CompletedProcess(argv, 1, "", "ambiguous response")
            elif action == "exec":
                component = args[args.index("-c") + 1]
                code = args[args.index("--") + 3]
                if code == faults.PARENT_BINDING_PROBE:
                    parent = (
                        "management"
                        if component == "control-reconciler"
                        else (slot.removesuffix("-data") + "-control")
                    )
                    properties = self.postgres(parent)["properties"]
                    value = {
                        "host": properties["host"],
                        "port": properties["port"],
                        "sslmode": "disable"
                        if self.config.environment == "local"
                        else "verify-full",
                    }
                    if self.drift == "binding":
                        value["host"] = "foreign"
                elif code == runner.SOURCE_PROBE:
                    value = {"files": {"source.py": "c" * 64}, "parent_dsn_present": False}
                elif code == runner.DATA_API_PERMISSIONS_PROBE:
                    value = {
                        "permissions": json.loads(args[-1]),
                        "parent_secret_get_status": 403,
                        "secret_list_status": 403,
                    }
                elif code == runner.IDENTITY_PROBE:
                    if component == "data-api":
                        value = {
                            "redis": {
                                "host": f"redis.{self.config.namespace(slot)}.svc.cluster.local",
                                "port": 6379,
                                "tls": self.config.environment == "azure",
                                "authenticated": True,
                                "wrong_password_rejected": True,
                            }
                        }
                    else:
                        pg = self.postgres(slot)["properties"]
                        value = {
                            "postgresql": {
                                "host": pg["host"],
                                "port": pg["port"],
                                "tls": self.config.environment == "azure",
                            }
                        }
                else:
                    raise AssertionError("Unexpected privileged pod command")
            else:
                return super().__call__(argv, **options)
        elif argv[0] == "az":
            assert argv[argv.index("--subscription") + 1] == self.config.subscription
            identity = argv[argv.index("--ids") + 1]
            if argv[1:3] == ["postgres", "flexible-server"]:
                slot = identity.rsplit("/pg-", 1)[1]
                pg = self.postgres(slot)
                value = {
                    "id": identity,
                    "host": pg["properties"]["host"],
                    "state": "Ready",
                    "network": {
                        "publicNetworkAccess": "Disabled",
                        "delegatedSubnetResourceId": self.subnet(slot),
                    },
                    "tags": {
                        "radapp.io-resource": pg["id"],
                        "radapp.io-environment": pg["properties"]["environment"],
                        "radapp.io-application": pg["properties"]["application"],
                    },
                }
                if self.drift == "public":
                    value["network"]["publicNetworkAccess"] = "Enabled"
            elif argv[1:4] == ["network", "vnet", "subnet"]:
                slot = identity.rsplit("/snet-", 1)[1].removesuffix("-postgresql")
                value = {
                    "id": identity,
                    "addressPrefix": f"10.64.{32 + faults.LOCAL_SLOTS.index(slot)}.0/27",
                    "delegations": [{"serviceName": "Microsoft.DBforPostgreSQL/flexibleServers"}],
                }
                if self.drift == "subnet":
                    value["id"] += "-foreign"
            else:
                assert argv[1:3] == ["network", "vnet"]
                value = {
                    "id": identity,
                    "tags": {
                        "project": self.config.project,
                        "deployment": self.config.deployment,
                        "environment": "azure",
                    },
                }
                if self.drift == "vnet":
                    value["tags"]["deployment"] = "foreign"
        elif argv[0] == "docker" and argv[3:5] != ["image", "inspect"]:
            assert argv[1:3] == ["--host", "unix:///synthetic/docker.sock"]
            args = argv[3:]
            if args[0] == "ps":
                context = args[-1].rsplit("=", 1)[1]
                self.calls.append((argv, options))
                return subprocess.CompletedProcess(argv, 0, self.node(self.slot(context))["Id"], "")
            if args[0] == "inspect":
                value = [
                    next(
                        self.node(slot)
                        for slot in faults.LOCAL_SLOTS
                        if self.node(slot)["Id"] == args[-1]
                    )
                ]
            elif args[:2] == ["network", "inspect"]:
                value = [
                    {
                        "Containers": {
                            self.node(slot)["Id"]: {
                                "IPv4Address": self.node(slot)["NetworkSettings"]["Networks"][
                                    "kind"
                                ]["IPAddress"]
                                + "/16"
                            }
                            for slot in faults.LOCAL_SLOTS
                        }
                    }
                ]
            else:
                assert args[0] == "exec"
                slot = next(slot for slot in faults.LOCAL_SLOTS if self.node(slot)["Id"] == args[1])
                command = args[2:]
                component = "control-reconciler" if slot.endswith("-control") else "data-reconciler"
                pod = self.pod(slot, component)
                generation = self.pod_generation.get((slot, component), 0)
                sandbox_id = ("d" if generation == 0 else "e") * 64
                sandbox_pid = 123 + generation
                sandbox_inode = str(4026532000 + generation)
                if command[:2] == ["crictl", "pods"]:
                    value = {
                        "items": [
                            {
                                "id": sandbox_id,
                                "metadata": {
                                    key: pod["metadata"][key]
                                    for key in ("uid", "name", "namespace")
                                },
                            },
                            *self.old_sandboxes.get(slot, []),
                        ]
                    }
                elif command[:2] == ["crictl", "inspectp"]:
                    value = {
                        "status": {
                            "id": sandbox_id,
                            "state": "SANDBOX_READY",
                            "metadata": {
                                key: pod["metadata"][key] for key in ("name", "namespace", "uid")
                            },
                            "labels": {"io.kubernetes.pod.uid": pod["metadata"]["uid"]},
                        },
                        "info": {"pid": sandbox_pid},
                    }
                elif command[:2] == ["crictl", "inspect"]:
                    value = {
                        "info": {"sandboxID": sandbox_id},
                        "status": {
                            "id": "c" * 64,
                            "labels": {"io.kubernetes.pod.uid": pod["metadata"]["uid"]},
                            "metadata": {"name": component},
                        },
                    }
                elif command[0] == "stat":
                    self.calls.append((argv, options))
                    return subprocess.CompletedProcess(argv, 0, sandbox_inode + "\n", "")
                elif command[:2] == ["crictl", "inspecti"]:
                    value = {"status": {"id": IMAGE_ID}}
                else:
                    assert command[:3] == ["bash", "-ceu", network.NETWORK_COMMAND]
                    assert command[4:6] == [str(sandbox_pid), sandbox_inode]
                    native = command[6:]
                    if native == ["iptables", "-w", "2", "-S", "OUTPUT"]:
                        output = "-P OUTPUT ACCEPT\n"
                        if slot in self.rules:
                            output += shlex.join(["-A", "OUTPUT", *self.rules[slot]]) + "\n"
                        output += self.extra_rules.get(slot, "")
                    else:
                        assert native[:3] == ["bash", "-ceu", network.RULE_COMMAND]
                        action, rule = native[4], native[5:]
                        if action == "add":
                            assert slot not in self.rules
                            self.rules[slot] = list(rule)
                        elif action == "delete":
                            assert self.rules.get(slot) == list(rule)
                            del self.rules[slot]
                            self.deleted.append((slot, "rule"))
                        else:
                            assert action == "check" and self.rules.get(slot) == list(rule)
                        if action == "add" and self.after_create_error:
                            self.calls.append((argv, options))
                            return subprocess.CompletedProcess(argv, 1, "", "ambiguous response")
                        output = ""
                    self.calls.append((argv, options))
                    return subprocess.CompletedProcess(argv, 0, output, "")
        else:
            return super().__call__(argv, **options)
        self.calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")

    def active(self, slot):
        return bool(self.rules.get(slot) or self.policies.get(slot))

    def probe(self, kube, component, pod):
        operator = self

        class Probe:
            def request(self, action):
                operator.probes.append((kube.target.slot, action))
                network_range = ipaddress.ip_network(kube.target.parent["allowed_cidrs"][0])
                address = str(
                    network_range.network_address + (0 if network_range.prefixlen == 32 else 4)
                )
                blocked = operator.active(kube.target.slot)
                return {
                    "ok": action == "local" or not blocked,
                    "network_failure": action != "local" and blocked,
                    "ips": [address],
                }

            def close(self):
                pass

        return Probe()

    @contextmanager
    def sdk(self, *, config_file, context):
        slot = self.slot(context)
        assert config_file and self.config.slot_name(slot) == context

        def call_api(path, method, *, body, **kwargs):
            assert method == "DELETE"
            assert body["preconditions"] and body["gracePeriodSeconds"] == 5
            assert kwargs["_request_timeout"] == (5, 15)
            name = path.rsplit("/", 1)[1]
            if "/pods/" in path:
                component = name.removesuffix("-pod")
                assert body["preconditions"]["uid"] == self.pod(slot, component)["metadata"]["uid"]
                self.pod_generation[(slot, component)] = (
                    self.pod_generation.get((slot, component), 0) + 1
                )
                self.deleted.append((slot, name))
                return
            current = self.policies[slot][name]
            assert body["preconditions"]["uid"] == current["metadata"]["uid"]
            assert f"/namespaces/{self.config.namespace(slot)}/" in path
            del self.policies[slot][name]
            self.deleted.append((slot, name))

        yield SimpleNamespace(configuration=SimpleNamespace(verify_ssl=True), call_api=call_api)


@pytest.fixture(params=["azure", "local"])
def world(tmp_path, monkeypatch, request):
    from kubernetes import config as kube_config

    monkeypatch.setattr(faults, "ROOT", tmp_path)
    selected = DemoConfig(
        request.param,
        "demo",
        "team",
        "11111111-1111-1111-1111-111111111111" if request.param == "azure" else None,
        "centralus" if request.param == "azure" else None,
    )
    initialize_config(selected, tmp_path / ".env")
    operator = FaultCommands(tmp_path, selected)
    configuration = faults.LiveConfiguration(tmp_path / ".env", execute=operator)
    monkeypatch.setattr(kube_config, "new_client_from_config", operator.sdk)
    selected_fault = LocalFault if request.param == "local" else faults.ParentFault

    def fault(
        config=configuration, slot="shared-control", component="control-reconciler", **kwargs
    ):
        return selected_fault(
            config,
            slot,
            component,
            clock=operator.clock,
            sleep=operator.clock.sleep,
            probe_factory=operator.probe,
            **kwargs,
        )

    yield SimpleNamespace(
        config=configuration, operator=operator, selected=selected_fault, fault=fault, root=tmp_path
    )
    configuration.close()


def test_parent_endpoint_is_live_radius_owned_and_matches_narrow_runtime_binding(world):
    target = world.config.fault_target("shared-data", "data-reconciler")
    assert target.parent["owner"]["slot"] == "shared-control"
    assert target.parent["owner"]["resource_id"].endswith("/postgreSqlDatabases/postgres")
    assert target.parent["port"] == (31543 if world.config.environment == "local" else 5432)
    assert any(faults.PARENT_BINDING_PROBE in argv for argv, _ in world.operator.calls)
    assert not any("secret" in argv for argv, _ in world.operator.calls)
    assert not (world.root / ".state").exists()


@pytest.mark.parametrize("drift", ["radius", "binding"])
def test_parent_owner_or_binding_drift_fails_before_any_fault_mutation(world, drift):
    world.operator.drift = drift
    with pytest.raises(faults.AcceptanceError):
        world.fault()
    assert not world.operator.journals
    assert not world.operator.rules and not world.operator.policies


def test_fault_journal_survives_fresh_configuration_and_restores_only_owned_rule(world):
    fault = world.fault()
    fault.activate()
    reference = fault.journal.reference
    assert world.operator.active("shared-control")
    fault.probe.close()
    fault.probe = None
    world.config.close()
    fresh = faults.LiveConfiguration(world.root / ".env", execute=world.operator)
    try:
        restored = world.selected.from_journal(
            fresh,
            "shared-control",
            "control-reconciler",
            reference,
            clock=world.operator.clock,
            sleep=world.operator.clock.sleep,
            probe_factory=world.operator.probe,
        )
        restored.restore()
        assert not world.operator.active("shared-control")
        assert restored.record["restored"] and restored.record["physical_restored"]
        saved = json.loads(next(iter(world.operator.journals.values()))["data"]["record.json"])
        assert saved["restored"] is True
        assert len(world.operator.deleted) == 1
        assert not (world.root / ".state").exists()
    finally:
        fresh.close()


@pytest.mark.parametrize("tamper", ["uid", "owner", "record", "version"])
def test_foreign_replaced_or_changed_journal_cannot_authorize_restore(world, tamper):
    fault = world.fault()
    fault.activate()
    document = next(iter(world.operator.journals.values()))
    if tamper == "uid":
        document["metadata"]["uid"] = uid("replacement")
    elif tamper == "owner":
        document["metadata"]["labels"]["plane-demo/deployment"] = "foreign"
    elif tamper == "record":
        document["data"]["record.json"] = "{}"
    else:
        document["metadata"]["resourceVersion"] = "99999"
    with pytest.raises(faults.AcceptanceError):
        fault.restore()
    assert world.operator.active("shared-control")
    assert world.operator.deleted == []


def test_changed_live_rules_are_detected_before_deleting_anything(world):
    fault = world.fault()
    fault.activate()
    if world.config.environment == "local":
        world.operator.extra_rules["shared-control"] = "-A OUTPUT -d 172.18.0.19/32 -j ACCEPT\n"
    else:
        world.operator.extra_policies["shared-control"] = [
            {
                "kind": "NetworkPolicy",
                "metadata": {"name": "foreign", "uid": uid("foreign")},
                "spec": {"podSelector": {}},
            }
        ]
    with pytest.raises(faults.AcceptanceError):
        fault.restore()
    assert world.operator.active("shared-control")
    assert world.operator.deleted == []


def test_ambiguous_rule_creation_uses_committed_journal_for_safe_restore(world):
    world.operator.after_create_error = True
    fault = world.fault()
    with pytest.raises(faults.AcceptanceError):
        with fault:
            pytest.fail("ambiguous create must not report activation success")
    assert not world.operator.active("shared-control")
    record = json.loads(next(iter(world.operator.journals.values()))["data"]["record.json"])
    assert record["creation_attempted"] is True and record["restored"] is True


def test_another_active_fault_is_not_adopted_or_stacked(world):
    original = world.fault()
    original.activate()
    before = copy.deepcopy(world.operator.journals)
    with pytest.raises(faults.AcceptanceError):
        with world.fault():
            pytest.fail("active fault must be restored explicitly")
    assert world.operator.journals == before
    assert world.operator.active("shared-control")


@pytest.mark.parametrize("world", ["azure"], indirect=True)
@pytest.mark.parametrize("drift", ["public", "vnet", "subnet"])
def test_azure_backend_and_network_owner_guards_are_not_bypassed(world, drift):
    world.operator.drift = drift
    with pytest.raises(faults.AcceptanceError):
        world.config.fault_target("shared-control", "control-reconciler")
    assert world.operator.journals == {}


def test_stale_reference_cannot_restore_a_new_fault_generation(world):
    first = world.fault()
    with first:
        reference = first.journal.reference
    second = world.fault()
    second.activate()
    deleted = list(world.operator.deleted)
    with pytest.raises(faults.AcceptanceError, match="reference_changed"):
        world.selected.from_journal(
            world.config,
            "shared-control",
            "control-reconciler",
            reference,
            probe_factory=world.operator.probe,
        )
    assert world.operator.deleted == deleted
    second.restore()


def test_journal_intent_hash_is_checked_independently_of_record_hash(world):
    import hashlib

    fault = world.fault()
    fault.activate()
    document = next(iter(world.operator.journals.values()))
    record = json.loads(document["data"]["record.json"])
    record["parent"]["port"] = 1234
    raw = faults.canonical_json(record)
    document["data"]["record.json"] = raw
    document["metadata"]["annotations"]["plane-demo/record-sha256"] = hashlib.sha256(
        raw.encode()
    ).hexdigest()
    with pytest.raises(faults.AcceptanceError, match="fingerprint"):
        world.selected.from_journal(
            world.config,
            "shared-control",
            "control-reconciler",
            probe_factory=world.operator.probe,
        )
    assert world.operator.active("shared-control") and world.operator.deleted == []


@pytest.mark.parametrize("selector", ["reference", "active"])
def test_restore_cli_recovers_orphan_from_only_dotenv_and_live_journal(
    world, monkeypatch, capsys, selector
):
    fault = world.fault()
    fault.activate()
    reference = fault.journal.reference
    fault.probe.close()
    world.config.close()
    factory = Mock()
    factory.from_journal.side_effect = lambda *args: world.selected.from_journal(
        *args,
        clock=world.operator.clock,
        sleep=world.operator.clock.sleep,
        probe_factory=world.operator.probe,
    )
    monkeypatch.setattr(faults, "fault_class", lambda _: factory)
    monkeypatch.setattr(faults, "read_json", Mock(side_effect=AssertionError("host journal")))
    assert (
        faults.main(
            [
                "--execute",
                "--slot",
                "shared-control",
                "--component",
                "control-reconciler",
                "--restore",
                *([reference] if selector == "reference" else []),
            ],
            configuration_factory=lambda path: faults.LiveConfiguration(
                path, execute=world.operator
            ),
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report == {"outcome": "restored_only_not_acceptance", "journal": reference}
    assert not world.operator.active("shared-control")
    assert not (world.root / ".state").exists()


class APIs:
    """Synthetic runtime API boundary for exercising the unchanged harness algorithms."""

    def __init__(self, operator):
        self.operator = operator
        self.tenants = {}
        self.urls = {}
        for index, slot in enumerate(faults.LOCAL_SLOTS):
            url = (
                f"http://127.0.0.1:{35490 + index}"
                if operator.config.environment == "local"
                else f"https://{operator.config.slot_name(slot)}.centralus.cloudapp.azure.com"
            )
            self.urls[url] = slot

    def create(self, name, message):
        self.tenants[name] = {
            "pair_id": "isolated-1" if name == "isolated-c" else "shared",
            "operation_id": uid(name + "operation"),
            "onboarding_id": uid(name + "onboarding"),
            "running": True,
            "version": 1,
            "message": message,
            "counter": 0,
            "applied_version": 0,
            "applied_message": None,
            "applied": [],
        }

    def refresh(self, record):
        slot = record["pair_id"] + "-data"
        if (
            not self.operator.active(slot)
            and self.operator.replicas.get((slot, "data-reconciler"), 1)
            and record["applied_version"] != record["version"]
        ):
            record["applied_version"] = record["version"]
            record["applied_message"] = record["message"]
            record["applied"].append(record["version"])

    def handle(self, request):
        slot = self.urls[
            str(request.url)
            .split("/tenants", 1)[0]
            .split("/operations", 1)[0]
            .split("/healthz", 1)[0]
            .split("/api/", 1)[0]
        ]
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        path = request.url.path
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        expected_key = (
            role + "-api-runtime" + self.operator.config.namespace(slot) + "-synthetic-key"
        )
        if request.headers.get("X-Demo-Key") != expected_key:
            return httpx.Response(401, json={"detail": "unauthorized"})
        if path == "/api/container-info":
            return httpx.Response(404, json={})
        if path == "/tenants" and request.method == "POST":
            body = json.loads(request.content)
            name = body["tenant_id"]
            if name.startswith("busy-"):
                return httpx.Response(
                    503, json={"detail": "provisioner_busy"}, headers={"Retry-After": "5"}
                )
            if name in self.tenants:
                return httpx.Response(
                    409,
                    json={"status_url": "/tenants/" + name},
                    headers={"Location": "/tenants/" + name},
                )
            self.create(name, body["initial_message"])
            return httpx.Response(
                202,
                json={
                    "operation_id": self.tenants[name]["operation_id"],
                    "status_url": "/tenants/" + name,
                },
            )
        if path.startswith("/operations/"):
            record = next(
                value
                for value in self.tenants.values()
                if value["operation_id"] == path.rsplit("/", 1)[1]
            )
            return httpx.Response(
                200, json={"status": "running" if record["running"] else "succeeded"}
            )
        name = path.split("/")[2]
        record = self.tenants.get(name)
        if record is None or (
            slot != "management" and not slot.startswith(record["pair_id"] + "-")
        ):
            return httpx.Response(404, json={"detail": "not_found"})
        if request.method == "PUT":
            record["version"] += 1
            record["message"] = json.loads(request.content)["message"]
        self.refresh(record)
        at = "2026-09-14T00:00:00+00:00"
        if role == "management":
            record["running"] = False
            value = {
                "tenant_id": name,
                "pair_id": record["pair_id"],
                "isolation": "isolated" if name == "isolated-c" else "shared",
                "operation_id": record["operation_id"],
                "onboarding_id": record["onboarding_id"],
                "provisioning_status": "succeeded",
                "onboarding_status": "ready",
                "control_record": {"status": "created", "observed_revision": 1, "reported_at": at},
            }
            events = [("tenant_requested", 1), ("control_record_created", 1)]
        elif role == "control":
            value = {
                "tenant_id": name,
                "onboarding_id": record["onboarding_id"],
                "desired": {"version": record["version"], "message": record["message"]},
                "data_config": {
                    "status": "applied"
                    if record["applied_version"] == record["version"]
                    else "pending",
                    "last_applied_version": record["applied_version"] or None,
                    "reported_at": at,
                },
            }
            events = [("configuration_created", 1)]
            events += [
                ("configuration_updated", version) for version in range(2, record["version"] + 1)
            ]
            events += [("config_applied", version) for version in record["applied"]]
        else:
            if not record["applied_version"]:
                return httpx.Response(404, json={"detail": "not_found"})
            if request.method == "POST":
                record["counter"] += 1
            value = {
                "tenant_id": name,
                "onboarding_id": record["onboarding_id"],
                "applied_version": record["applied_version"],
                "message": record["applied_message"],
                "counter": record["counter"],
            }
            events = []
        if "after_event_id" in request.url.params:
            after = int(request.url.params["after_event_id"])
            page = [
                {"event_id": index + 1, "type": kind, "version": version, "received_at": at}
                for index, (kind, version) in enumerate(events)
                if index + 1 > after
            ][:2]
            value["timeline"] = page
            value["next_after_event_id"] = (
                page[-1]["event_id"] if page and page[-1]["event_id"] < len(events) else None
            )
        return httpx.Response(200, json=value)

    def clients(self, configuration):
        return runner.APIs(
            configuration,
            factory=lambda url, key: runner.Client(
                url, key, transport=httpx.MockTransport(self.handle)
            ),
        )


@pytest.mark.parametrize("mode", ["all", "outages", "verify-existing"])
def test_live_modes_run_real_outage_algorithms_and_persist_restoration(world, monkeypatch, mode):
    api = APIs(world.operator)
    if mode != "all":
        for name in ("shared-a", "shared-b", "isolated-c"):
            api.create(name, "existing-" + name)
            api.tenants[name]["running"] = False
    subject = runner.Runner(
        world.config,
        mode,
        apis=api.clients(world.config),
        fault_factory=lambda *args: world.selected(
            *args,
            clock=world.operator.clock,
            sleep=world.operator.clock.sleep,
            probe_factory=world.operator.probe,
        ),
        clock=world.operator.clock,
        sleep=world.operator.clock.sleep,
    )
    monkeypatch.setattr(faults, "command", lambda _: "")
    monkeypatch.setattr(faults, "source_metadata", lambda: SOURCE)
    monkeypatch.setattr(runner, "source_hashes", lambda _: {"source.py": "c" * 64})
    monkeypatch.setattr(runner, "local_source_hashes", lambda _: {"source.py": "c" * 64})
    subject.run()
    assert subject.record["outcome"] == "passed"
    faults_saved = [
        json.loads(value["data"]["record.json"])
        for value in world.operator.journals.values()
        if value["metadata"]["labels"]["plane-demo/journal-kind"] == "fault"
    ]
    assert len(faults_saved) == 2
    assert all(record["restored"] and record["physical_restored"] for record in faults_saved)
    assert not world.operator.active("shared-control") and not world.operator.active("shared-data")
    assert any(
        event["type"] == "data_api_restarted_without_parent" for event in subject.record["events"]
    )
    continuity = [event for event in subject.record["events"] if event["type"] == "data_continuity"]
    assert len(continuity) == 2 and all(len(event["samples"]) == 10 for event in continuity)
    assert not (world.root / ".state").exists()


def prepare_run(world, api, mode, **kwargs):
    return runner.Runner(
        world.config,
        mode,
        apis=api.clients(world.config),
        fault_factory=lambda *args: world.selected(
            *args,
            clock=world.operator.clock,
            sleep=world.operator.clock.sleep,
            probe_factory=world.operator.probe,
        ),
        clock=world.operator.clock,
        sleep=world.operator.clock.sleep,
        **kwargs,
    )


def source_doubles(monkeypatch):
    monkeypatch.setattr(
        faults,
        "command",
        lambda args: SOURCE["committed_at"] if args[1:3] == ["show", "-s"] else "",
    )
    monkeypatch.setattr(faults, "source_metadata", lambda: SOURCE)
    monkeypatch.setattr(runner, "source_hashes", lambda _: {"source.py": "c" * 64})
    monkeypatch.setattr(runner, "local_source_hashes", lambda _: {"source.py": "c" * 64})


def test_continuation_cli_uses_owned_journal_and_finishes_real_outages(world, monkeypatch, capsys):
    source_doubles(monkeypatch)
    api = APIs(world.operator)
    first = prepare_run(world, api, "all")
    first.wait_ready = Mock(side_effect=faults.AcceptanceError("first_admission_interrupted"))
    with pytest.raises(faults.AcceptanceError, match="first_admission_interrupted"):
        first.run()
    reference = first.journal.reference
    first_uid = api.tenants["shared-a"]["onboarding_id"]
    world.config.close()

    original = runner.Runner

    def construct(configuration, mode, **kwargs):
        return original(
            configuration,
            mode,
            apis=api.clients(configuration),
            fault_factory=lambda *args: world.selected(
                *args,
                clock=world.operator.clock,
                sleep=world.operator.clock.sleep,
                probe_factory=world.operator.probe,
            ),
            clock=world.operator.clock,
            sleep=world.operator.clock.sleep,
            **kwargs,
        )

    monkeypatch.setattr(runner, "Runner", construct)
    monkeypatch.setattr(faults, "read_json", Mock(side_effect=AssertionError("host authority")))
    assert (
        runner.main(
            [
                "--config",
                str(world.root / ".env"),
                "--mode",
                "all",
                "--continue-first-from",
                reference,
                "--execute",
            ],
            configuration_factory=lambda path: faults.LiveConfiguration(
                path, execute=world.operator
            ),
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "passed"
    assert report["continued_first_from"]["run_id"] == first.run_id
    assert api.tenants["shared-a"]["onboarding_id"] == first_uid
    assert [
        event["tenant"] for event in report["events"] if event["type"] == "tenant_accepted"
    ] == ["shared-b", "isolated-c"]
    assert any(event["type"] == "management_link_recovered" for event in report["events"])
    assert any(event["type"] == "control_link_recovered_latest_only" for event in report["events"])
    assert len(world.operator.deleted) == 3
    predecessor = world.operator.journals[
        (world.operator.config.namespace("management"), first.journal.name)
    ]
    assert predecessor["metadata"]["annotations"]["plane-demo/continued-by"] == report["run_id"]


def acceptance_record(configuration, run_id="d" * 32):
    at = faults.utc_now()
    events = [
        {"type": "acceptance_started"},
        {"type": "workload_image", "slot": "management", "component": "management-api"},
        {"type": "workload_image", "slot": "management", "component": "provisioner"},
        {
            "type": "tenant_accepted",
            "tenant": "shared-a",
            "http_status": 202,
            "operation_id": uid("operation"),
            "busy_verified": True,
        },
    ]
    return {
        "version": 1,
        "project": configuration.project,
        "environment": configuration.environment,
        "mode": "all",
        "run_id": run_id,
        "source": SOURCE,
        "outcome": "failed",
        "started_at": at,
        "finished_at": at,
        "events": [{**event, "at": at} for event in events],
    }


@pytest.mark.parametrize("outcome", ["failed", "running"])
def test_continuation_is_claimed_once_and_old_writer_cannot_resume(world, monkeypatch, outcome):
    source_doubles(monkeypatch)
    record = acceptance_record(world.config)
    record["outcome"] = outcome
    if outcome == "running":
        del record["finished_at"]
    journal = faults.ConfigMapJournal(
        world.config.kube(world.config.target("management")),
        "plane-demo-acceptance-" + record["run_id"],
        "acceptance",
    )
    journal.start(record)
    first = runner.Runner(world.config, "all", continue_first_from=journal.reference)
    second = runner.Runner(world.config, "all", continue_first_from=journal.reference)
    try:
        first.load_first_admission()
        with pytest.raises(faults.AcceptanceError, match="already_continued"):
            second.load_first_admission()
        with pytest.raises(faults.AcceptanceError, match="journal_changed"):
            journal.save(record)
    finally:
        first.apis.close()
        second.apis.close()


def test_mutations_check_current_acceptance_journal_before_http(world):
    api = APIs(world.operator)
    subject = prepare_run(world, api, "scenario")
    subject.record["source"] = SOURCE
    subject.record["outcome"] = "running"
    subject.save("acceptance_started")
    client = subject.client("management")
    document = world.operator.journals[
        (world.config.config.namespace("management"), subject.journal.name)
    ]
    document["metadata"]["resourceVersion"] = "99999"
    try:
        with pytest.raises(faults.AcceptanceError, match="journal_changed"):
            client.request(
                "POST",
                "/tenants",
                body={
                    "tenant_id": "shared-a",
                    "isolation": "shared",
                    "initial_message": "test",
                },
                statuses=(202,),
            )
        assert api.tenants == {}
    finally:
        subject.apis.close()


def test_changed_owned_rule_fingerprint_is_not_deleted(world):
    fault = world.fault()
    fault.activate()
    if world.config.environment == "local":
        world.operator.rules["shared-control"][-1] = "ACCEPT"
    else:
        policy = next(iter(world.operator.policies["shared-control"].values()))
        policy["spec"]["egressDeny"][0]["toPorts"][0]["ports"][0]["port"] = "1234"
    with pytest.raises(faults.AcceptanceError):
        fault.restore()
    assert world.operator.deleted == []


@pytest.mark.parametrize("world", ["local"], indirect=True)
def test_live_cleanup_preflight_uses_journals_and_current_rules_without_files(world):
    with world.fault():
        pass
    before = copy.deepcopy(world.operator.journals)
    result = network.assert_restored_for_cleanup(world.config.command, world.config)
    assert len(result["targets"]) == 4
    assert len(result["journals"]) == 1
    assert world.operator.journals == before
    assert not (world.root / ".state").exists()
    world.operator.extra_rules["shared-control"] = "-A OUTPUT -j ACCEPT\n"
    with pytest.raises(faults.AcceptanceError, match="rules_changed"):
        network.assert_restored_for_cleanup(world.config.command, world.config)
    assert world.operator.journals == before


def test_recreated_journal_cannot_rebind_an_existing_fault_even_with_a_new_uid_seal(world):
    fault = world.fault()
    fault.activate()
    document = next(iter(world.operator.journals.values()))
    replacement = uid("recreated-and-resealed")
    document["metadata"]["uid"] = replacement
    document["metadata"]["annotations"]["plane-demo/journal-uid"] = replacement
    with pytest.raises(faults.AcceptanceError, match="restore_.*mismatch"):
        world.selected.from_journal(
            world.config,
            "shared-control",
            "control-reconciler",
            probe_factory=world.operator.probe,
        )
    assert world.operator.active("shared-control")
    assert world.operator.deleted == []


def test_prepared_intent_is_cancelled_safely_after_a_crash_before_mutation(world, monkeypatch):
    fault = world.fault()
    monkeypatch.setattr(
        fault.journal, "commit_fault", Mock(side_effect=RuntimeError("process disappeared"))
    )
    with pytest.raises(RuntimeError, match="process disappeared"):
        fault.activate()
    reference = fault.journal.reference
    assert not world.operator.active("shared-control")
    fault.probe.close()
    world.config.close()
    fresh = faults.LiveConfiguration(world.root / ".env", execute=world.operator)
    try:
        restored = world.selected.from_journal(
            fresh,
            "shared-control",
            "control-reconciler",
            reference,
            clock=world.operator.clock,
            sleep=world.operator.clock.sleep,
            probe_factory=world.operator.probe,
        )
        assert restored.creation_attempted is False
        restored.restore()
        assert restored.record["restored"] is True
        assert world.operator.deleted == []
    finally:
        fresh.close()


def test_resealed_and_rewritten_journal_cannot_relabel_the_live_fault(world):
    import hashlib

    fault = world.fault()
    fault.activate()
    document = next(iter(world.operator.journals.values()))
    replacement = uid("rewritten-journal")
    document["metadata"]["uid"] = replacement
    annotations = document["metadata"]["annotations"]
    annotations["plane-demo/journal-uid"] = replacement
    record = json.loads(document["data"]["record.json"])
    if world.config.environment == "local":
        index = record["rule"].index("--comment") + 1
        record["rule"][index] = f"plane-demo-fault-{record['run_id']}-{replacement}"
    else:
        record["policy"]["metadata"]["annotations"]["plane-demo/journal-uid"] = replacement
    raw = faults.canonical_json(record)
    document["data"]["record.json"] = raw
    annotations["plane-demo/record-sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    annotations["plane-demo/intent-sha256"] = faults.intent_fingerprint(record, "fault")
    restored = world.selected.from_journal(
        world.config,
        "shared-control",
        "control-reconciler",
        clock=world.operator.clock,
        sleep=world.operator.clock.sleep,
        probe_factory=world.operator.probe,
    )
    with pytest.raises(faults.AcceptanceError, match="fault_restoration_failed"):
        restored.restore()
    assert world.operator.deleted == []
    assert world.operator.active("shared-control")


def crash_between_create_and_seal(world, monkeypatch):
    fault = world.fault()
    submit = fault.journal._submit

    def interrupted(record, verb, **kwargs):
        submit(record, verb, **kwargs)
        if verb == "create":
            raise RuntimeError("crash_between_create_and_seal")

    monkeypatch.setattr(fault.journal, "_submit", interrupted)
    with pytest.raises(RuntimeError, match="crash_between_create_and_seal"):
        fault.activate()
    reference = fault.journal.reference
    document = next(iter(world.operator.journals.values()))
    assert "plane-demo/journal-uid" not in document["metadata"]["annotations"]
    assert json.loads(document["data"]["record.json"])["creation_attempted"] is False
    assert not world.operator.active("shared-control")
    fault.probe.close()
    world.config.close()
    return reference


def test_unsealed_creation_is_cancelled_from_a_fresh_command_and_component_is_unblocked(
    world, monkeypatch, capsys
):
    reference = crash_between_create_and_seal(world, monkeypatch)
    factory = Mock()
    factory.from_journal.side_effect = lambda *args: world.selected.from_journal(
        *args,
        clock=world.operator.clock,
        sleep=world.operator.clock.sleep,
        probe_factory=world.operator.probe,
    )
    monkeypatch.setattr(faults, "fault_class", lambda _: factory)
    monkeypatch.setattr(faults, "read_json", Mock(side_effect=AssertionError("local journal")))
    assert (
        faults.main(
            [
                "--execute",
                "--slot",
                "shared-control",
                "--component",
                "control-reconciler",
                "--restore",
            ],
            configuration_factory=lambda path: faults.LiveConfiguration(
                path, execute=world.operator
            ),
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report == {"outcome": "cancelled_before_mutation", "journal": reference}
    assert world.operator.deleted == []
    document = next(iter(world.operator.journals.values()))
    assert (
        document["metadata"]["annotations"]["plane-demo/journal-uid"] == document["metadata"]["uid"]
    )
    record = json.loads(document["data"]["record.json"])
    assert record["creation_attempted"] is False and record["restored"] is True
    fresh = faults.LiveConfiguration(world.root / ".env", execute=world.operator)
    try:
        subsequent = world.fault(config=fresh)
        with subsequent:
            pass
        assert subsequent.record["restored"] is True
        assert subsequent.journal.reference != reference
        assert not (world.root / ".state").exists()
    finally:
        fresh.close()


@pytest.mark.parametrize("tamper", ["foreign_owner", "replaced_uid", "mismatched_seal"])
def test_unsealed_recovery_rejects_foreign_replaced_and_badly_sealed_journals(
    world, monkeypatch, tamper
):
    reference = crash_between_create_and_seal(world, monkeypatch)
    document = next(iter(world.operator.journals.values()))
    if tamper == "foreign_owner":
        document["metadata"]["labels"]["plane-demo/project"] = "foreign"
    elif tamper == "replaced_uid":
        document["metadata"]["uid"] = uid("unsealed-replacement")
    else:
        document["metadata"]["annotations"]["plane-demo/journal-uid"] = uid("wrong-seal")
    before = copy.deepcopy(world.operator.journals)
    fresh = faults.LiveConfiguration(world.root / ".env", execute=world.operator)
    try:
        with pytest.raises(faults.AcceptanceError):
            world.selected.from_journal(
                fresh,
                "shared-control",
                "control-reconciler",
                reference,
                probe_factory=world.operator.probe,
            )
        assert world.operator.journals == before
        assert world.operator.deleted == []
    finally:
        fresh.close()


def test_active_journal_with_removed_seal_is_not_treated_as_unsealed_creation(world):
    fault = world.fault()
    fault.activate()
    document = next(iter(world.operator.journals.values()))
    del document["metadata"]["annotations"]["plane-demo/journal-uid"]
    with pytest.raises(faults.AcceptanceError, match="not_pre_mutation"):
        world.selected.from_journal(
            world.config,
            "shared-control",
            "control-reconciler",
            probe_factory=world.operator.probe,
        )
    assert world.operator.active("shared-control")
    assert world.operator.deleted == []


def test_unsealed_cancellation_rejects_replacement_between_check_and_update(world, monkeypatch):
    crash_between_create_and_seal(world, monkeypatch)
    replaced = False

    def execute(argv, **options):
        nonlocal replaced
        if "replace" in argv:
            submitted = json.loads(options["input"])
            record = json.loads(submitted["data"]["record.json"])
            if record.get("outcome") == "cancelled_before_mutation":
                document = next(iter(world.operator.journals.values()))
                document["metadata"]["uid"] = uid("replacement-at-cancellation")
                document["metadata"]["resourceVersion"] = "999999"
                replaced = True
        return world.operator(argv, **options)

    fresh = faults.LiveConfiguration(world.root / ".env", execute=execute)
    try:
        fault = world.selected.from_journal(
            fresh,
            "shared-control",
            "control-reconciler",
            probe_factory=world.operator.probe,
        )
        with pytest.raises(faults.AcceptanceError):
            fault.restore()
        assert replaced
        document = next(iter(world.operator.journals.values()))
        assert "plane-demo/journal-uid" not in document["metadata"]["annotations"]
        assert json.loads(document["data"]["record.json"])["outcome"] == "preparing"
        assert world.operator.deleted == []
    finally:
        fresh.close()


def test_unsealed_cancellation_requires_original_rules_to_be_unchanged(world, monkeypatch):
    crash_between_create_and_seal(world, monkeypatch)
    if world.config.environment == "local":
        world.operator.extra_rules["shared-control"] = "-A OUTPUT -j ACCEPT\n"
    else:
        world.operator.extra_policies["shared-control"] = [
            {
                "kind": "NetworkPolicy",
                "metadata": {"name": "other", "uid": uid("other")},
                "spec": {"podSelector": {}},
            }
        ]
    before = copy.deepcopy(world.operator.journals)
    fresh = faults.LiveConfiguration(world.root / ".env", execute=world.operator)
    try:
        fault = world.selected.from_journal(
            fresh,
            "shared-control",
            "control-reconciler",
            probe_factory=world.operator.probe,
        )
        with pytest.raises(faults.AcceptanceError, match="original_.*changed"):
            fault.restore()
        assert world.operator.journals == before
        assert world.operator.deleted == []
    finally:
        fresh.close()


def forge_unsealed_baseline_containing_applied_fault(world, applied):
    import hashlib

    document = next(iter(world.operator.journals.values()))
    record = json.loads(document["data"]["record.json"])
    document["metadata"]["uid"] = uid("forged-unsealed-baseline")
    document["metadata"]["resourceVersion"] = "99999"
    annotations = document["metadata"]["annotations"]
    annotations.pop("plane-demo/journal-uid")
    record.update(outcome="preparing", creation_attempted=False, restored=False, run_id="f" * 12)
    for field in ("policy", "rule", "policy_uid", "blocked_at", "physical_restored"):
        record.pop(field, None)
    if world.config.environment == "local":
        record["original_rules_sha256"] = applied.rules_hash()
    else:
        record["original_policies"] = applied.kube.policies()
    raw = faults.canonical_json(record)
    document["data"]["record.json"] = raw
    annotations["plane-demo/record-sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    annotations["plane-demo/intent-sha256"] = faults.intent_fingerprint(record, "fault")


def restore_from_fresh_cli(world, monkeypatch):
    factory = Mock()
    factory.from_journal.side_effect = lambda *args: world.selected.from_journal(
        *args,
        clock=world.operator.clock,
        sleep=world.operator.clock.sleep,
        probe_factory=world.operator.probe,
    )
    monkeypatch.setattr(faults, "fault_class", lambda _: factory)
    return faults.main(
        ["--execute", "--slot", "shared-control", "--component", "control-reconciler", "--restore"],
        configuration_factory=lambda path: faults.LiveConfiguration(path, execute=world.operator),
    )


def test_forged_baseline_cannot_hide_applied_fault_from_unpinned_restore(
    world, monkeypatch, capsys
):
    applied = world.fault()
    applied.activate()
    forge_unsealed_baseline_containing_applied_fault(world, applied)
    before = copy.deepcopy(world.operator.journals)
    applied.probe.close()
    world.config.close()
    assert restore_from_fresh_cli(world, monkeypatch) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "failed"
    assert world.operator.journals == before
    assert world.operator.active("shared-control")
    assert world.operator.deleted == []


@pytest.mark.parametrize("world", ["local"], indirect=True)
@pytest.mark.parametrize("phase", ["unsealed", "sealed_prepared"])
def test_unsealed_cancellation_after_pod_replacement_never_rebinds_stored_sandbox(
    world, monkeypatch, capsys, phase
):
    if phase == "unsealed":
        crash_between_create_and_seal(world, monkeypatch)
    else:
        fault = world.fault()
        monkeypatch.setattr(
            fault.journal, "commit_fault", Mock(side_effect=RuntimeError("pre-mutation crash"))
        )
        with pytest.raises(RuntimeError, match="pre-mutation crash"):
            fault.activate()
        fault.probe.close()
        world.config.close()
    document = next(iter(world.operator.journals.values()))
    before = json.loads(document["data"]["record.json"])
    world.operator.pod_generation[("shared-control", "control-reconciler")] = 1
    world.operator.extra_rules["shared-control"] = "-A OUTPUT -d 172.18.0.50/32 -j ACCEPT\n"
    world.operator.calls.clear()
    assert restore_from_fresh_cli(world, monkeypatch) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == (
        "cancelled_before_mutation" if phase == "unsealed" else "restored_only_not_acceptance"
    )
    record = json.loads(next(iter(world.operator.journals.values()))["data"]["record.json"])
    assert record["sandbox"] == before["sandbox"]
    assert record["pod_uid"] == before["pod_uid"]
    assert world.operator.deleted == []
    entered = [argv for argv, _ in world.operator.calls if network.NETWORK_COMMAND in argv]
    assert entered
    assert all(argv[argv.index(network.NETWORK_COMMAND) + 2] == "124" for argv in entered)
    assert all(network.RULE_COMMAND not in argv for argv, _ in world.operator.calls)
    assert world.operator.extra_rules["shared-control"].endswith("-j ACCEPT\n")


@pytest.mark.parametrize("world", ["local"], indirect=True)
def test_cancellation_does_not_enter_an_orphan_or_unrelated_namespace(world, monkeypatch, capsys):
    old = world.operator.pod("shared-control", "control-reconciler")
    crash_between_create_and_seal(world, monkeypatch)
    before = copy.deepcopy(world.operator.journals)
    world.operator.pod_generation[("shared-control", "control-reconciler")] = 1
    world.operator.old_sandboxes["shared-control"] = [
        {
            "id": "d" * 64,
            "metadata": {key: old["metadata"][key] for key in ("uid", "name", "namespace")},
        }
    ]
    world.operator.calls.clear()
    assert restore_from_fresh_cli(world, monkeypatch) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "local_cancellation_other_reconciler_sandbox_present"
    assert world.operator.journals == before
    assert not any(network.NETWORK_COMMAND in argv for argv, _ in world.operator.calls)
    assert world.operator.deleted == []


@pytest.mark.parametrize("world", ["azure"], indirect=True)
@pytest.mark.parametrize("marker", ["name", "label", "annotation"])
def test_cancellation_recognizes_fault_markers_independently_of_candidate_identity(
    world, monkeypatch, capsys, marker
):
    applied = world.fault()
    applied.activate()
    policy = next(iter(world.operator.policies["shared-control"].values()))
    metadata = policy["metadata"]
    if marker != "name":
        metadata["name"] = "another-policy-name"
    metadata["labels"] = {"plane-demo/fault-run": applied.run_id} if marker == "label" else {}
    metadata["annotations"] = (
        {"plane-demo/journal-uid": applied.journal.uid} if marker == "annotation" else {}
    )
    forge_unsealed_baseline_containing_applied_fault(world, applied)
    before = copy.deepcopy(world.operator.journals)
    applied.probe.close()
    world.config.close()
    assert restore_from_fresh_cli(world, monkeypatch) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "unattempted_fault_artifact_present"
    assert world.operator.journals == before
    assert world.operator.active("shared-control")
    assert world.operator.deleted == []
