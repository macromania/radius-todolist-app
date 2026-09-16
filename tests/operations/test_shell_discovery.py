import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FAKE = r"""
import base64,json,os,sys
from pathlib import Path
tool=Path(sys.argv[0]).name
args=sys.argv[1:]
spec=json.loads(Path(os.environ["FAKE_SPEC"]).read_text())
with Path(os.environ["FAKE_LOG"]).open("a") as log:
    log.write(json.dumps({"tool":tool,"args":args})+"\n")
name=spec["name"];namespace=spec["namespace"];slot=spec["slot"];mode=spec.get("mode")
def argument(key):
    return args[args.index(key)+1]
def profile(local):
    context=("kind-" if local else "")+name
    return {
        "apiVersion":"v1","kind":"Config","current-context":context,
        "contexts":[{"name":context,"context":{"cluster":context,"user":context}}],
        "clusters":[{"name":context,"cluster":{
            "server":"https://wrong.invalid" if mode=="wrong-server" else spec["server"],
            "certificate-authority-data":"c3ludGhldGljLWNh",
            **({"insecure-skip-tls-verify":True} if mode=="insecure" else {}),
        }}],
        "users":[{"name":context,"user":
            {"client-certificate-data":"Y2VydA==","client-key-data":"a2V5"} if local else
            {"exec":{"command":"kubelogin","apiVersion":"client.authentication.k8s.io/v1beta1",
                     "args":["get-token"],"interactiveMode":"IfAvailable"}}
        }],
    }
if tool=="az":
    if args[:2]==["aks","show"]:
        print(json.dumps({"id":spec["aks_id"],"fqdn":"api.example.azmk8s.io",
                          "provisioningState":"Succeeded","tags":{"project":"radplanes","deployment":"learning"}}))
    elif args[:2]==["aks","get-credentials"]:
        Path(argument("--file")).write_text(json.dumps(profile(False)))
    else: raise SystemExit("unexpected Azure command")
elif tool=="docker":
    if args[:3]==["context","inspect","desktop-linux"]:
        print(json.dumps("unix:///test/docker.sock"))
    elif args[2]=="ps":
        print("a"*64)
    elif args[2]=="inspect":
        print(json.dumps([{"Id":"a"*64,"Name":"/"+name+"-control-plane","State":{"Running":True},
                           "Config":{"Labels":{"io.x-k8s.kind.cluster":name}},
                           "HostConfig":{"PortBindings":{"6443/tcp":[{"HostIp":"127.0.0.1","HostPort":str(spec["port"])}]}}}]))
    else: raise SystemExit("unexpected Docker command")
elif tool=="kind":
    print(json.dumps(profile(True)))
elif tool=="kubelogin":
    if args[0]!="convert-kubeconfig": raise SystemExit("unexpected credential command")
elif tool=="kubectl":
    path=Path(argument("--kubeconfig"))
    if "config" in args:
        if "rename-context" in args:
            value=json.loads(path.read_text());value["current-context"]=name;value["contexts"][0]["name"]=name
            path.write_text(json.dumps(value))
        elif "view" in args: print(path.read_text())
    elif "--raw" in args:
        resource=argument("--raw").split("?")[0].removeprefix("/apis/api.ucp.dev/v1alpha3")
        app=resource.split("/providers/")[0]+"/providers/Applications.Core/applications/"+spec["role"]
        environment=resource.split("/providers/")[0]+"/providers/Applications.Core/environments/"+slot
        print(json.dumps({"id":resource,"properties":{
            "application":app+"/foreign" if mode=="foreign-radius" else app,
            "environment":environment,
            "provisioningState":"Failed" if mode=="unready" else "Succeeded",
            "url":"http://untrusted.invalid" if mode=="wrong-url" else spec["url"],
            "host":spec["url"].split("://")[1].split(":")[0],
        }}))
    elif "namespace" in args:
        if mode=="denied":
            raise SystemExit("synthetic namespace access denied")
        print(json.dumps({"metadata":{"name":namespace,"uid":"11111111-1111-1111-1111-111111111111",
             "labels":{"plane-demo/project":"other" if mode=="foreign-namespace" else "radplanes",
                       "plane-demo/deployment":"learning","plane-demo/environment":spec["environment"]}}}))
    elif "secret" in args:
        name=args[args.index("secret")+1]
        print(name+"\n"+namespace+"\n"+base64.b64encode(spec["api_key"].encode()).decode())
    elif "configmap" in args:
        name=args[args.index("configmap")+1]
        assert name.startswith("plane-demo-fault-")
        if mode=="missing-journal": raise SystemExit("journal not found")
        record=spec.get("fault_record", {
            "slot":slot,"component":name.removeprefix("plane-demo-fault-"),
            "outcome":"blocked_verified","restored":False,
        })
        print(json.dumps({
            "kind":"ConfigMap",
            "metadata":{"name":"foreign" if mode=="wrong-journal" else name,"namespace":namespace},
            "data":{"record.json":
                "invalid json" if mode=="malformed-journal" else json.dumps(record)}
        }))
    elif "get" in args and "pods" in args:
        if mode=="command-failed": raise SystemExit(23)
        print(json.dumps({"items":[]}))
    else: raise SystemExit("unexpected Kubernetes command")
elif tool=="curl":
    lines=Path(argument("--config")).read_text().splitlines()
    headers=[json.loads(line.split("=",1)[1].strip()) for line in lines]
    status="200" if "X-Demo-Key: "+spec["api_key"] in headers else "401"
    payload={"tenant_id":"alpha","counter":7} if status=="200" else {"detail":"unauthorized"}
    if "--data-binary" in args:
        payload["received"]=json.loads(Path(argument("--data-binary")[1:]).read_text())
    Path(argument("--output")).write_text(json.dumps(payload))
    print(status,end="")
else: raise SystemExit("unexpected tool")
"""


@pytest.fixture
def checkout(tmp_path):
    for relative in (
        "scripts/lib/output.sh",
        "scripts/lib/env.sh",
        "scripts/lib/discovery.sh",
        "scripts/operations/endpoints.sh",
        "scripts/operations/api.sh",
        "scripts/operations/kube.sh",
        "scripts/operations/fault-status.sh",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    (tmp_path / "bin").mkdir()
    (tmp_path / "work").mkdir()
    for tool in ("az", "kind", "docker", "kubectl", "kubelogin", "curl"):
        path = tmp_path / "bin" / tool
        path.write_text(f"#!{ROOT / '.venv/bin/python'}\n" + FAKE)
        path.chmod(0o700)
    return tmp_path


def configure(root, environment, slot="management", mode=None):
    index = [
        "management",
        "shared-control",
        "shared-data",
        "isolated-1-control",
        "isolated-1-data",
    ].index(slot)
    role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
    name = f"radplanes-learning-{environment}-{slot}"
    values = {"DEMO_ENV": environment, "DEMO_PROJECT": "radplanes", "DEMO_DEPLOYMENT": "learning"}
    if environment == "azure":
        values.update(
            AZURE_SUBSCRIPTION_ID="11111111-1111-1111-1111-111111111111", AZURE_LOCATION="centralus"
        )
    (root / ".env").write_text(
        "".join(f"{key}={json.dumps(value)}\n" for key, value in values.items())
    )
    (root / ".env").chmod(0o600)
    spec = {
        "environment": environment,
        "slot": slot,
        "name": name,
        "namespace": f"{name}-{role}",
        "role": role,
        "port": 35495 + index,
        "mode": mode,
        "server": "https://api.example.azmk8s.io"
        if environment == "azure"
        else f"https://127.0.0.1:{35495 + index}",
        "url": "https://one.centralus.cloudapp.azure.com"
        if environment == "azure"
        else f"http://127.0.0.1:{35490 + index}",
        "aks_id": (
            f"/subscriptions/{values.get('AZURE_SUBSCRIPTION_ID')}/resourceGroups/rg-{name}-cluster"
            f"/providers/Microsoft.ContainerService/managedClusters/aks-{name}"
        ),
        "api_key": 'synthetic-key-with-\\-and-"-' + "x" * 40,
    }
    (root / "spec.json").write_text(json.dumps(spec))
    return spec


@pytest.mark.parametrize("environment", ["azure", "local"])
@pytest.mark.parametrize("slot", ["shared-control", "isolated-1-data"])
def test_make_fault_status_reads_only_the_selected_live_journal(checkout, environment, slot):
    configure(checkout, environment, slot)
    shutil.copyfile(ROOT / "Makefile", checkout / "Makefile")
    component = "control-reconciler" if slot.endswith("control") else "data-reconciler"
    result = subprocess.run(
        ["make", "--no-print-directory", "fault-status", f"ARGS={slot} {component}"],
        cwd=checkout,
        env={
            **os.environ,
            "PATH": str(checkout / "bin") + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(checkout / "work"),
            "FAKE_SPEC": str(checkout / "spec.json"),
            "FAKE_LOG": str(checkout / "calls.jsonl"),
            "CONFIRM_AZURE": "",
            "CONFIRM_LOCAL": "",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "slot": slot,
        "component": component,
        "outcome": "blocked_verified",
        "restored": False,
    }
    calls = [json.loads(line) for line in (checkout / "calls.jsonl").read_text().splitlines()]
    assert not any(
        set(call["args"]) & {"create", "apply", "patch", "delete", "secret"} for call in calls
    )
    assert not (checkout / ".state").exists()
    assert list((checkout / "work").iterdir()) == []
    if environment == "local":
        assert not any(call["tool"] in {"az", "kubelogin"} for call in calls)


@pytest.mark.parametrize(
    "failure",
    [
        "missing-journal",
        "malformed-journal",
        "wrong-journal",
        "foreign-namespace",
        "target",
        "shape",
    ],
)
def test_fault_status_reports_read_or_record_failures(checkout, failure):
    spec = configure(checkout, "local", "shared-data", mode=failure)
    if failure in {"target", "shape"}:
        spec["fault_record"] = (
            []
            if failure == "shape"
            else {"slot": "shared-control", "component": "control-reconciler"}
        )
        (checkout / "spec.json").write_text(json.dumps(spec))
    result = subprocess.run(
        [
            "bash",
            str(checkout / "scripts/operations/fault-status.sh"),
            "shared-data",
            "data-reconciler",
        ],
        env={
            **os.environ,
            "PATH": str(checkout / "bin") + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(checkout / "work"),
            "FAKE_SPEC": str(checkout / "spec.json"),
            "FAKE_LOG": str(checkout / "calls.jsonl"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0 and result.stderr
    assert not result.stdout
    assert list((checkout / "work").iterdir()) == []


def test_fault_status_rejects_mismatched_slot_before_discovery(checkout):
    result = subprocess.run(
        [
            "bash",
            str(checkout / "scripts/operations/fault-status.sh"),
            "shared-data",
            "control-reconciler",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "matching control/data slot" in result.stderr
    assert not (checkout / "calls.jsonl").exists()


@pytest.mark.parametrize("environment", ["azure", "local"])
@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_make_api_preserves_query_arguments_and_json_stdin(checkout, environment, method):
    spec = configure(checkout, environment, "shared-control")
    shutil.copyfile(ROOT / "Makefile", checkout / "Makefile")
    route = (
        "/tenants/alpha?limit=2&after_event_id=7"
        if method == "GET"
        else "/tenants/alpha/configuration"
    )
    body = {"message": "$(touch should-not-exist) | quoted ' text & spaces"}
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "api",
            f"ARGS=control:shared {method} '{route}'",
        ],
        cwd=checkout,
        env={
            **os.environ,
            "PATH": str(checkout / "bin") + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(checkout / "work"),
            "FAKE_SPEC": str(checkout / "spec.json"),
            "FAKE_LOG": str(checkout / "calls.jsonl"),
        },
        input=json.dumps(body) if method == "PUT" else "",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (checkout / "calls.jsonl").read_text().splitlines()]
    curl = next(call["args"] for call in calls if call["tool"] == "curl")
    assert curl[curl.index("--url") + 1] == spec["url"] + route
    if method == "PUT":
        assert json.loads(result.stdout)["received"] == body
    assert not (checkout / "should-not-exist").exists()
    assert list((checkout / "work").iterdir()) == []


def discover(root, slot):
    return subprocess.run(
        ["bash", str(root / "scripts/operations/endpoints.sh"), slot],
        cwd=root,
        env={
            **os.environ,
            "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(root / "work"),
            "FAKE_SPEC": str(root / "spec.json"),
            "FAKE_LOG": str(root / "calls.jsonl"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("environment", ["azure", "local"])
@pytest.mark.parametrize("failure", [False, True])
def test_kube_entrypoint_uses_fresh_scoped_access_and_preserves_exit_status(
    checkout, environment, failure
):
    spec = configure(checkout, environment, "shared-data", "command-failed" if failure else None)
    result = subprocess.run(
        [
            "bash",
            str(checkout / "scripts/operations/kube.sh"),
            "shared-data",
            "get",
            "pods",
            "-o",
            "json",
        ],
        env={
            **os.environ,
            "PATH": str(checkout / "bin") + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(checkout / "work"),
            "FAKE_SPEC": str(checkout / "spec.json"),
            "FAKE_LOG": str(checkout / "calls.jsonl"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == (23 if failure else 0), result.stderr
    records = [json.loads(line) for line in (checkout / "calls.jsonl").read_text().splitlines()]
    command = records[-1]
    assert command["tool"] == "kubectl"
    arguments = command["args"]
    assert arguments[arguments.index("--context") + 1] == spec["name"]
    assert arguments[arguments.index("--namespace") + 1] == spec["namespace"]
    assert list((checkout / "work").iterdir()) == []
    assert not (checkout / ".state").exists()


@pytest.mark.parametrize(
    "override", ["--context=other", "--namespace=other", "-nother", "--server=x"]
)
def test_kube_entrypoint_refuses_connection_overrides_before_discovery(checkout, override):
    configure(checkout, "local")
    result = subprocess.run(
        [
            "bash",
            str(checkout / "scripts/operations/kube.sh"),
            "management",
            "get",
            "pods",
            override,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0 and "overrides are not supported" in result.stderr
    assert not (checkout / "calls.jsonl").exists()


@pytest.mark.parametrize("environment", ["azure", "local"])
@pytest.mark.parametrize("slot", ["management", "shared-data", "isolated-1-control"])
def test_native_discovery_has_no_state_dependency_and_cleans_profiles(checkout, environment, slot):
    spec = configure(checkout, environment, slot)
    result = discover(checkout, slot)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"slot": slot, "url": spec["url"]}
    calls = [json.loads(line) for line in (checkout / "calls.jsonl").read_text().splitlines()]
    assert not (checkout / ".state").exists()
    assert list((checkout / "work").iterdir()) == []
    if environment == "local":
        assert not any(call["tool"] in {"az", "kubelogin"} for call in calls)
    else:
        for call in calls:
            if call["tool"] == "az":
                assert "--subscription" in call["args"]
                assert "--admin" not in call["args"]
    assert any("--raw" in call["args"] for call in calls)


@pytest.mark.parametrize("environment", ["azure", "local"])
def test_bootstrap_cluster_access_is_separate_from_application_namespace_access(
    checkout, environment
):
    configure(checkout, environment, mode="denied")
    env = {
        **os.environ,
        "PATH": str(checkout / "bin") + os.pathsep + os.environ["PATH"],
        "TMPDIR": str(checkout / "work"),
        "FAKE_SPEC": str(checkout / "spec.json"),
        "FAKE_LOG": str(checkout / "calls.jsonl"),
    }
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -e; source scripts/lib/env.sh; source scripts/lib/discovery.sh; "
            "demo_load_env .env; demo_workspace; trap demo_remove_workspace EXIT; "
            'demo_open_cluster management; printf "%s\\n" "$DEMO_CONTEXT"',
        ],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"radplanes-learning-{environment}-management"
    calls = [json.loads(line) for line in (checkout / "calls.jsonl").read_text().splitlines()]
    assert not any("namespace" in call["args"] for call in calls)
    assert discover(checkout, "management").returncode != 0
    assert not (checkout / ".state").exists()
    assert list((checkout / "work").iterdir()) == []


@pytest.mark.parametrize("environment", ["azure", "local"])
@pytest.mark.parametrize(
    "mode",
    [
        "wrong-server",
        "insecure",
        "foreign-namespace",
        "foreign-radius",
        "wrong-url",
        "unready",
        "denied",
    ],
)
def test_native_discovery_refuses_wrong_or_unavailable_owners(checkout, environment, mode):
    configure(checkout, environment, mode=mode)
    result = discover(checkout, "management")
    assert result.returncode != 0
    assert result.stdout == ""
    assert list((checkout / "work").iterdir()) == []


def test_next_independent_command_reads_changed_live_endpoint(checkout):
    spec = configure(checkout, "azure")
    first = discover(checkout, "management")
    assert first.returncode == 0, first.stderr
    spec["url"] = "https://two.centralus.cloudapp.azure.com"
    (checkout / "spec.json").write_text(json.dumps(spec))
    second = discover(checkout, "management")
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["url"] != json.loads(first.stdout)["url"]
    assert list((checkout / "work").iterdir()) == []


def test_native_aks_profile_accepts_explicit_https_default_port(checkout):
    spec = configure(checkout, "azure")
    spec["server"] += ":443"
    (checkout / "spec.json").write_text(json.dumps(spec))
    result = discover(checkout, "management")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["url"] == spec["url"]


@pytest.mark.parametrize(
    "target,args",
    [
        ("endpoints", "management"),
        ("api", "management GET /tenants/alpha"),
    ],
)
def test_root_make_entrypoints_need_no_state_directory(checkout, target, args):
    configure(checkout, "local")
    shutil.copyfile(ROOT / "Makefile", checkout / "Makefile")
    result = subprocess.run(
        ["make", "--no-print-directory", target, "ARGS=" + args],
        cwd=checkout,
        env={
            **os.environ,
            "PATH": str(checkout / "bin") + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(checkout / "work"),
            "FAKE_SPEC": str(checkout / "spec.json"),
            "FAKE_LOG": str(checkout / "calls.jsonl"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert isinstance(json.loads(result.stdout), dict)
    assert not (checkout / ".state").exists()
    assert list((checkout / "work").iterdir()) == []


@pytest.mark.parametrize("environment", ["azure", "local"])
def test_native_api_queries_live_owner_and_sends_key_without_cli_exposure(checkout, environment):
    spec = configure(checkout, environment)
    result = subprocess.run(
        ["bash", str(checkout / "scripts/operations/api.sh"), "management", "POST", "/tenants"],
        cwd=checkout,
        env={
            **os.environ,
            "PATH": str(checkout / "bin") + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(checkout / "work"),
            "FAKE_SPEC": str(checkout / "spec.json"),
            "FAKE_LOG": str(checkout / "calls.jsonl"),
        },
        input='{"tenant_id":"alpha"}',
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["received"] == {"tenant_id": "alpha"}
    assert "HTTP 200" in result.stderr
    log = (checkout / "calls.jsonl").read_text()
    assert spec["api_key"] not in log + result.stdout + result.stderr
    calls = [json.loads(line) for line in log.splitlines()]
    credential_read = next(call for call in calls if "secret" in call["args"])
    assert ".data.DEMO_KEY" in credential_read["args"][-1]
    assert ".data.CONTROL_DSN" not in credential_read["args"][-1]
    curl = next(call for call in calls if call["tool"] == "curl")
    assert curl["args"][0] == "-q"
    assert "--location" not in curl["args"]
    assert list((checkout / "work").iterdir()) == []
    assert not (checkout / ".state").exists()


def test_real_curl_ignores_user_redirect_configuration(checkout):
    spec = configure(checkout, "local")
    real_curl = shutil.which("curl")
    assert real_curl
    home = checkout / "home"
    home.mkdir()
    (home / ".curlrc").write_text("location\n")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.headers.get("Host"), self.headers.get("X-Demo-Key")))
            if len(requests) == 1:
                self.send_response(302)
                self.send_header("Location", "http://foreign.invalid/redirected")
            else:
                self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"detail":"redirect"}')

        def log_message(self, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="curl-proof-", dir="/tmp") as temporary:
        socket_path = Path(temporary) / "http.sock"
        with socketserver.UnixStreamServer(str(socket_path), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            shim = checkout / "bin/curl"
            shim.write_text('#!/bin/sh\nexec "$REAL_CURL" "$@" --unix-socket "$HTTP_SOCKET"\n')
            shim.chmod(0o700)
            try:
                result = subprocess.run(
                    [
                        "bash",
                        str(checkout / "scripts/operations/api.sh"),
                        "management",
                        "GET",
                        "/tenants/alpha",
                    ],
                    cwd=checkout,
                    env={
                        **os.environ,
                        "PATH": str(checkout / "bin") + os.pathsep + os.environ["PATH"],
                        "TMPDIR": str(checkout / "work"),
                        "FAKE_SPEC": str(checkout / "spec.json"),
                        "FAKE_LOG": str(checkout / "calls.jsonl"),
                        "HOME": str(home),
                        "CURL_HOME": str(home),
                        "REAL_CURL": real_curl,
                        "HTTP_SOCKET": str(socket_path),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
    assert result.returncode != 0
    assert "HTTP 302" in result.stderr
    assert requests == [("127.0.0.1:35490", spec["api_key"])]
    assert spec["api_key"] not in result.stdout + result.stderr
    assert list((checkout / "work").iterdir()) == []
