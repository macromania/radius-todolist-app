import base64
import gzip
import json
import threading
from unittest.mock import Mock

import pytest
from local_support import common, gate, prepare


def docker_node(name, node_id, address):
    return {
        "Id": node_id,
        "Name": f"/{name}-control-plane",
        "Config": {"Labels": {"io.x-k8s.kind.cluster": name}},
        "NetworkSettings": {"Networks": {"kind": {"IPAddress": address}}},
        "HostConfig": {
            "PortBindings": {
                "6443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "35496"}],
                "31480/tcp": [{"HostIp": "127.0.0.1", "HostPort": "35491"}],
            }
        },
    }


def stored_state():
    state = {
        "terraform_version": "1.15.8",
        "serial": 1,
        "lineage": "offline-test-lineage",
        "resources": [
            {
                "module": "module.default",
                "mode": "managed",
                "type": "kind_cluster",
                "name": "child",
                "instances": [
                    {
                        "attributes": {
                            "name": common.CHILD,
                            "node_image": common.NODE_IMAGE,
                            "id": f"{common.CHILD}-{common.NODE_IMAGE}",
                            "completed": True,
                            "kubeconfig": "offline-test-kubeconfig",
                            "client_key": "offline-test-key",
                        }
                    }
                ],
            },
            {
                "module": "module.default",
                "mode": "managed",
                "type": "kubernetes_secret_v1",
                "name": "access",
                "instances": [
                    {
                        "attributes": {
                            "metadata": [
                                {
                                    "name": common.ACCESS_SECRET,
                                    "namespace": common.NAMESPACE,
                                }
                            ]
                        }
                    }
                ],
            },
        ],
    }
    return {
        "metadata": {
            "name": common.state_secret_name(),
            "uid": "offline-state-uid",
            "labels": {"tfstate": "true", "app.kubernetes.io/managed-by": "terraform"},
        },
        "data": {"tfstate": base64.b64encode(gzip.compress(json.dumps(state).encode())).decode()},
    }


class GateCommands:
    """Offline command double. It never contacts Docker, Kubernetes, or Radius."""

    def __init__(self):
        self.calls = []
        self.created = False
        self.observed = threading.Event()
        self.objects = {}
        self.fail_child = False
        self.logs = ""
        self.run_id = ""
        self.child_id = "child-id"
        self.state_uid = "offline-state-uid"
        self.emit_process = True
        self.allowed_secret_reads = set()

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == "docker":
            if "ps" in args:
                if f"label=io.x-k8s.kind.cluster={common.CHILD}" in args:
                    return self.child_id if self.created else ""
                return "management-id\nunrelated-id" + (
                    f"\n{self.child_id}" if self.created else ""
                )
            if "inspect" in args:
                child = args[-1] == f"{common.CHILD}-control-plane"
                return json.dumps(
                    [
                        docker_node(
                            common.CHILD if child else common.MANAGEMENT,
                            self.child_id if child else "management-id",
                            "172.18.0.3" if child else "172.18.0.2",
                        )
                    ]
                )
        if args[0] == "rad":
            if "create" in args:
                self.created = True
                self.observed.wait(timeout=1)
                return ""
            if "delete" in args:
                self.created = False
                return ""
            if "list" in args:
                return json.dumps([{"name": "shared-control"}] if self.created else [])
            if "show" in args:
                return json.dumps(
                    {
                        "id": common.RESOURCE_ID,
                        "properties": {
                            "provisioningState": "Succeeded",
                            "clusterId": f"kind://{common.CHILD}",
                            "clusterName": common.CHILD,
                            "bootstrapAccessRef": (
                                f"kubernetes://{common.NAMESPACE}/{common.ACCESS_SECRET}#kubeconfig"
                            ),
                        },
                    }
                )
        if args[0] != "kubectl":
            raise AssertionError(f"Unexpected command: {args}")
        if "etcdctl" in args:
            return json.dumps(
                {
                    "kvs": [
                        {
                            "value": base64.b64encode(
                                b"k8s:enc:aescbc:v1:local-key:offline-ciphertext",
                            ).decode()
                        }
                    ]
                }
            )
        if args[-1] == gate.PROCESS_PROBE:
            self.observed.set()
            return (
                "/proc/42/exe /terraform/request/terraform-provider-kind_v0.11.0"
                if self.emit_process
                else ""
            )
        if "can-i" in args:
            subject = next(
                value.removeprefix("--as=") for value in args if value.startswith("--as=")
            )
            namespace = args[args.index("-n") + 1]
            verb = args[args.index("can-i") + 1]
            return "yes" if (subject, namespace, verb) in self.allowed_secret_reads else "no"
        if "get" in args:
            kind, name = args[args.index("get") + 1 :][:2]
            if kind == "services":
                return '{"items":[]}'
            if kind == "pods":
                if any(a.startswith("radplanes.local/gate-run=") for a in args):
                    return '{"items":[]}'
                return json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "dynamic-rp-test", "uid": "executor-uid"},
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "dynamic-rp",
                                            "image": common.image_names()["executor"],
                                            "securityContext": {"runAsUser": 65532},
                                            "env": [
                                                {"name": "RADIUS_LOGGING_LEVEL", "value": "error"},
                                                {
                                                    "name": "DOCKER_HOST",
                                                    "value": "unix:///run/radplanes/docker.sock",
                                                },
                                            ],
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                )
            if kind == "configmap" and name == "dynamic-rp-config":
                return json.dumps(
                    {
                        "data": {
                            "radius-self-host.yaml": (
                                'terraform:\n  path: /terraform\n  logLevel: "OFF"\n'
                            ),
                        }
                    }
                )
            if kind == "secret" and name == common.state_secret_name():
                value = stored_state()
                value["metadata"]["uid"] = self.state_uid
                return json.dumps(value) if self.created else ""
            if kind == "secret" and name == common.ACCESS_SECRET:
                return (
                    json.dumps(
                        {
                            "metadata": {
                                "annotations": {
                                    "radplanes.local/radius-resource": common.RESOURCE_ID,
                                }
                            }
                        }
                    )
                    if self.created
                    else ""
                )
            value = self.objects.get((kind, name))
            return json.dumps(value) if value else ""
        if "create" in args:
            payload = json.loads(kwargs["data"])
            for obj in payload["items"]:
                self.objects[(obj["kind"].lower(), obj["metadata"]["name"])] = obj
                if obj["kind"] == "ConfigMap":
                    self.run_id = json.loads(obj["data"]["inputs.json"])["runId"]
            return ""
        if "delete" in args:
            kind, name = args[args.index("delete") + 1 :][:2]
            self.objects.pop((kind, name), None)
            return ""
        if "logs" in args:
            if "dynamic-rp-test" in args:
                return self.logs
            return json.dumps(
                {
                    "runId": self.run_id,
                    "childTLS": True,
                    "wrongServerNameRejected": True,
                    "childRadius": True,
                    "radiusWorkload": True,
                    "envoyReady": True,
                    "parentPostgreSQL": {
                        "authenticated": True,
                        "tlsRequired": False,
                        "nodePort": 31543,
                    },
                }
            )
        if "wait" in args and any(a.startswith("job/gate-") for a in args) and self.fail_child:
            raise common.LocalError("Offline child Job failure")
        return ""

    def json(self, args, **kwargs):
        return json.loads(self.run(args, **kwargs))


@pytest.fixture
def gate_commands(local_state, monkeypatch):
    commands = GateCommands()
    common.write_private(local_state / "management-created.json", {"nodeId": "management-id"})
    inputs = prepare.prepare()
    common.write_private(local_state / "installed.json", inputs)
    monkeypatch.setattr(gate, "require_inspection", Mock())
    monkeypatch.setattr(gate, "Commands", lambda: commands)
    monkeypatch.setattr(gate.time, "sleep", lambda _: None)
    response = Mock()
    response.status = 200
    response.read.side_effect = lambda _: f"local-cluster-gate:{commands.run_id}".encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(gate.urllib.request, "build_opener", Mock(return_value=opener))
    return commands


def test_complete_gate_entrypoint_wires_invocation_state_tls_workload_and_radius_deletion(
    local_state,
    gate_commands,
):
    assert gate.main(["run", "--execute"]) == 0
    record = json.loads(next((local_state / "runs").glob("*.json")).read_text())
    assert record["status"] == "passed"
    assert record["terraformState"]["uid"] == "offline-state-uid"
    assert record["observedKindProvider"].endswith("terraform-provider-kind_v0.11.0")
    assert record["childProof"]["childTLS"]
    assert record["envoyHostPort"] == 35491
    assert record["childDeletedThroughRadius"] and record["childDockerAndKubernetesAbsent"]
    assert "offline-test-key" not in json.dumps(record)
    assert not gate_commands.created
    assert not gate_commands.objects
    commands = [args for args, _ in gate_commands.calls]
    create = next(i for i, a in enumerate(commands) if a[0] == "rad" and "create" in a)
    delete = next(i for i, a in enumerate(commands) if a[0] == "rad" and "delete" in a)
    assert any(a[-1] == gate.PROCESS_PROBE for a in commands[create:delete])
    assert sum("etcdctl" in a for a in commands) == 3
    assert not any(a[0] == "kind" for a in commands)
    assert not any("--force" in a or "rm" in a for a in commands)
    assert "--yes" in commands[delete]


def test_child_failure_stays_failed_and_explicit_cleanup_does_not_relabel_success(
    local_state,
    gate_commands,
):
    gate_commands.fail_child = True
    assert gate.main(["run", "--execute"]) == 1
    path = next((local_state / "runs").glob("*.json"))
    record = json.loads(path.read_text())
    assert record["status"] == "failed"
    assert gate_commands.created
    assert not any(a[0] == "rad" and "delete" in a for a, _ in gate_commands.calls)
    assert gate.main(["delete", "--execute", "--run", str(path)]) == 0
    result = json.loads(path.read_text())
    assert result["status"] == "failed"
    assert result["cleanupStatus"] == "passed"
    assert not gate_commands.created


@pytest.mark.parametrize("namespace", ["radius-system", common.NAMESPACE])
@pytest.mark.parametrize("account_namespace", ["default", "radius-system", common.NAMESPACE])
@pytest.mark.parametrize("verb", ["get", "list", "watch"])
def test_secret_access_by_any_checked_default_account_blocks_creation(
    local_state, gate_commands, namespace, account_namespace, verb
):
    subject = f"system:serviceaccount:{account_namespace}:default"
    gate_commands.allowed_secret_reads.add((subject, namespace, verb))
    assert gate.main(["run", "--execute"]) == 1
    record = json.loads(next((local_state / "runs").glob("*.json")).read_text())
    assert record["status"] == "failed"
    assert (
        f"{account_namespace}:default can {verb} protected Secrets in {namespace}"
        in record["error"]
    )
    assert not gate_commands.created
    assert not any(args[0] == "rad" and "create" in args for args, _ in gate_commands.calls)


def test_state_metadata_alone_never_passes_without_observed_execution(local_state, gate_commands):
    gate_commands.emit_process = False
    assert gate.main(["run", "--execute"]) == 1
    record = json.loads(next((local_state / "runs").glob("*.json")).read_text())
    assert record["status"] == "failed"
    assert "No actual kind-provider process" in record["error"]
    assert "terraformState" in record and "childNodeId" in record
    assert gate_commands.created


def test_existing_child_is_not_automatically_adopted(local_state, gate_commands):
    gate_commands.created = True
    assert gate.main(["run", "--execute"]) == 1
    assert not any(a[0] == "rad" and "create" in a for a, _ in gate_commands.calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("state_uid", "different-state-owner"),
        ("child_id", "different-child"),
    ],
)
def test_explicit_delete_rechecks_state_and_docker_ownership(
    local_state,
    gate_commands,
    field,
    value,
):
    gate_commands.fail_child = True
    assert gate.main(["run", "--execute"]) == 1
    path = next((local_state / "runs").glob("*.json"))
    setattr(gate_commands, field, value)
    assert gate.main(["delete", "--execute", "--run", str(path)]) == 1
    record = json.loads(path.read_text())
    assert record["error"] == "Offline child Job failure"
    assert record["cleanupStatus"] == "failed"
    assert record["cleanupError"]
    assert not any(a[0] == "rad" and "delete" in a for a, _ in gate_commands.calls)


def test_credential_log_marker_fails_without_exporting_contents(
    local_state,
    gate_commands,
    capsys,
):
    gate_commands.logs = "client-key-data: do-not-export-this-value"
    assert gate.main(["run", "--execute"]) == 1
    text = next((local_state / "runs").glob("*.json")).read_text()
    assert "do-not-export-this-value" not in text
    assert "do-not-export-this-value" not in capsys.readouterr().err
    assert gate_commands.created


def test_wrong_state_and_nonloopback_ports_fail_closed():
    stored = stored_state()
    stored["metadata"]["labels"]["tfstate"] = "false"
    with pytest.raises(common.LocalError):
        gate.state_summary(stored)
    node = docker_node(common.CHILD, "child-id", "172.18.0.3")
    node["HostConfig"]["PortBindings"]["6443/tcp"][0]["HostIp"] = "0.0.0.0"
    with pytest.raises(common.LocalError, match="loopback"):
        gate.verify_ports(node)
