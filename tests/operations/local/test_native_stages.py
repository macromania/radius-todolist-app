"""Native entrypoint tests use command doubles; artifact proofs use actual synthetic tar bytes."""

import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
REVISION = "a" * 40
STEM = "demo-one-local"
NODE_IMAGE = (
    "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
)
FILES = (
    "build.sh",
    "bootstrap.sh",
    "setup.sh",
    "assets.py",
    "terraform-init.py",
    "module-server.py",
    "prepare.py",
    "common.py",
    "docker_desktop.py",
    "bootstrap-assets.Dockerfile",
    "terraform.tfrc",
)

DOUBLE = r"""
import hashlib,io,json,os,shutil,subprocess,sys,tarfile
from pathlib import Path
root = Path(__file__).resolve().parent.parent
repo = root / "repository"
store = root / "store.json"
state = json.loads(store.read_text())
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
stem, revision = "demo-one-local", "a" * 40
cluster, namespace = stem + "-management", stem + "-management-management"
node = cluster + "-control-plane"
node_id = "c" * 64
def save(): store.write_text(json.dumps(state))
def out(value): print(json.dumps(value))
def fail(): print("synthetic command failure",file=sys.stderr); sys.exit(8)
def info(ref):
    identifier = "sha256:" + hashlib.sha256(ref.encode()).hexdigest()
    role = next((name for name in ("api","provisioner","executor","operator")
                 if f"-{name}:" in ref), "base")
    return {"Id":identifier,"Architecture":"arm64","Os":"linux","Config":{
        "User":"10001:10001" if role in {"api","provisioner"} else "65532:65532",
        "Entrypoint":["/dynamic-rp"] if role == "executor" else ["python3"],
        "Cmd":["python","-m","plane_demo.management." +
               ("api" if role == "api" else "provisioner")],
        "Labels":{"org.opencontainers.image.revision":revision,
                  "plane-demo/project":"demo","plane-demo/deployment":"one"}}}
def image(ref):
    if ref in state["images"]: return state["images"][ref]
    return next(value for value in state["images"].values() if value["Id"] == ref)
with (root/"calls.jsonl").open("a") as stream:
    stream.write(json.dumps({"tool":tool,"args":args,"home":os.environ.get("HOME"),
        "env":{k:v for k,v in os.environ.items()
               if k.startswith(("DEMO_KEY_", "AZURE_"))
               or k in {"DOCKER_CONTEXT","DOCKER_CONFIG"}}})+"\n")
if tool == "git":
    args=args[2:]
    if args == ["rev-parse","HEAD"]: print(revision)
    elif args[0] == "status":
        if state.get("dirty"): print(" M scripts/changed.py")
    elif args[0] == "ls-files": pass
    elif args[0] == "archive":
        with tarfile.open(fileobj=sys.stdout.buffer,mode="w|") as archive:
            for path in repo.rglob("*"):
                relative = path.relative_to(repo)
                if ".state" not in relative.parts and relative.name != ".env":
                    archive.add(path,arcname=str(relative),recursive=False)
    else: raise AssertionError(args)
elif tool == "docker":
    if args[:3] == ["context","inspect","desktop-linux"]:
        out("unix:///Users/operator/.docker/run/docker.sock"); sys.exit(0)
    assert args[:2] == ["--host","unix:///Users/operator/.docker/run/docker.sock"]
    args=args[2:]
    if args == ["info","--format","{{json .}}"]:
        out({"OperatingSystem":"Docker Desktop","OSType":"linux","Architecture":"aarch64"})
    elif args == ["info","--format","{{.ID}}"]: print("desktop-daemon")
    elif args[:2] == ["image","ls"]:
        if args[-1] in state["images"]: print(state["images"][args[-1]]["Id"])
    elif args[:2] == ["image","inspect"]:
        try: value=image(args[-1])
        except StopIteration: fail()
        out([value])
    elif args[0] == "build":
        assert "--progress=plain" in args
        assert Path(args[args.index("--file")+1]).is_file()
        if state.get("build_failure"): fail()
        ref=args[args.index("--tag")+1]
        assert ref not in state["images"]
        context=Path(args[-1])
        selected=("scripts", "scripts/operations/local/build.sh",
                  "scripts/recipes/local/cluster/check-images.sh",
                  "infra/radius/types/clusters.yaml", "infra/radius/types/clusters.tgz")
        state.setdefault("source_modes",{})[ref]={
            name: (context/name).stat().st_mode & 0o777 for name in selected}
        state.setdefault("private_modes",[]).append({
            "work": context.parent.stat().st_mode & 0o777,
            "home": Path(os.environ["HOME"]).stat().st_mode & 0o777,
            "proofs": (context.parent/"proofs").stat().st_mode & 0o777})
        state["images"][ref]=info(ref); save()
        packaged=Path(args[-1])/"scripts/operations/local/.packaged"
        if packaged.is_dir() and "-operator:" in ref:
            shutil.copytree(packaged,root/"prepared",dirs_exist_ok=True)
        print("visible-build-output")
    elif args[0] == "pull":
        ref=args[-1]
        state["images"][ref]=info(ref); save()
        print("visible-pull-output")
    elif args[0] == "run":
        assert "--network" in args and "none" in args and "--pull=never" in args
        print("b"*64 + "  /dynamic-rp")
    elif args[0] == "create":
        assert args[1:5] == ["--network","none","--entrypoint","/bin/true"]
        identifier=f"{state.get('sequence',0)+100:064x}"
        state["sequence"]=state.get("sequence",0)+1
        state["containers"][identifier]=image(args[-1]); save(); print(identifier)
    elif args[0] == "export":
        assert args[-1] in state["containers"]
        path=Path(args[args.index("--output")+1])
        with tarfile.open(path,"w") as archive: pass
    elif args[0] == "rm":
        assert args[-1] in state["containers"]
        state["containers"].pop(args[-1]); save()
    elif args[0] == "cp":
        assert args[1].split(":")[0] in state["containers"]
        shutil.copytree(root/"prepared",Path(args[2]),dirs_exist_ok=True)
    elif args[0] == "ps":
        if any(a.startswith("label=") for a in args) and state.get("node"):
            print(node_id)
        elif state.get("occupied") and any(a.startswith("name=") for a in args):
            print("d"*64)
    elif args[0] == "inspect":
        assert args[-1] == node
        out([{"Id":node_id,"Name":"/"+node,"State":{"Running":True},
              "Config":{"Image":state["node_image"],
                        "Labels":{"io.x-k8s.kind.cluster":cluster}}}])
    elif args[0] == "exec":
        assert args[1] == node and args[2] == "stat"
        print("0 0 660")
    else: raise AssertionError(args)
elif tool == "rad":
    assert args[0] == "--config" and "plane-local-" in args[1]
    args=args[2:]
    if args == ["version","--cli"]: print("0.60.2")
    elif args[:2] == ["bicep","publish-extension"]:
        Path(args[args.index("--target")+1]).write_bytes(b"generated-extension")
    elif args[:2] == ["install","kubernetes"]:
        assert state.get("encrypted")
        assert "--chart" in args and Path(args[args.index("--chart")+1]).exists()
        assert "--reinstall" not in args
        state["installed"]=True; save()
    elif args[:2] == ["environment","create"]:
        assert args[2] == "management"
        assert args[args.index("--group")+1] == stem
        assert args[args.index("--kubernetes-namespace")+1] == cluster
        state["radius_environment"]=True; save()
    elif args[:3] == ["resource","create","Applications.Core/environments"]:
        assert args[3] == "management"
        state["registered_environment"]=json.loads(Path(args[args.index("--from-file")+1]).read_text())
        save()
    elif args[0] in {"workspace","group","resource-type"}: pass
    else: raise AssertionError(args)
elif tool == "helm":
    if args[0] == "pull":
        destination=Path(args[args.index("--destination")+1])
        (destination/"helm-chart-0.60.2.tgz").write_bytes(b"downloaded-chart")
    elif args[0] == "template":
        print("kind: Pod\nspec:\n  containers:\n  - image: ghcr.io/radius-project/ucpd:0.60")
    else: raise AssertionError(args)
elif tool == "uv":
    action=args[args.index("python")+2:]
    if action[0] == "inspect":
        assert len(action) == 7 and action[3] == "arm64"
        assert Path(action[1]).name == "source" and Path(action[4]).exists()
        if state.get("proof_failure"): fail()
        dependencies=json.loads((root/"prepared/images.json").read_text())
        out({"source_hashes":{"synthetic-source":"verified-by-proof-double"},
             "radiusBinarySHA256":"b"*64,"dependencies":dependencies})
    elif action[0] == "images":
        assert sys.stdin.read()
        out(["ghcr.io/radius-project/ucpd:0.60"])
    elif action[0] == "overlay":
        out({"spec":{"template":{"metadata":{"labels":{
                "radplanes.local/executor":"management-only"}},"spec":{
                "securityContext":{"fsGroup":65532,"fsGroupChangePolicy":"OnRootMismatch",
                                   "supplementalGroups":[int(action[-1])]},
                "containers":[{"name":"dynamic-rp"}]}}}})
    elif action[0] in {"package-recipes", "setup-plan", "live-config", "owned-object",
                       "terraform-settings"}:
        sys.exit(subprocess.run(
            [sys.executable,str(repo/"scripts/operations/local/assets.py"),*action],
            env={**os.environ, "PYTHONPATH":str(repo)}
        ).returncode)
    else: raise AssertionError(action)
elif tool == "kind":
    if args == ["version"]: print("kind v0.31.0")
    elif args[:2] == ["create","cluster"]:
        assert args[args.index("--name")+1] == cluster
        assert not state.get("node")
        configuration=json.loads(Path(args[args.index("--config")+1]).read_text())
        state["kind_config"]=configuration; state["node"]=True; save()
    elif args[:2] == ["get","kubeconfig"]:
        assert args[-1] == cluster and state.get("node")
        out({"synthetic":"access"})
    elif args[:2] == ["load","docker-image"]:
        assert args[args.index("--name")+1] == cluster
        assert args[-1] in state["images"]
    else: raise AssertionError(args)
elif tool == "encryption-double":
    assert args[args.index("--cluster")+1] == cluster
    assert args[args.index("--node")+1] == node
    assert args[args.index("--context")+1] == "kind-"+cluster
    if state.get("encryption_failure"): fail()
    state["encrypted"]=True; save()
    out({"cluster":cluster,"nodeId":node_id,"syntheticCiphertextVerified":True})
elif tool == "kubectl":
    assert args[:4] == ["--kubeconfig",os.environ["KUBECONFIG"],"--context","kind-"+cluster]
    assert args[4] == "--request-timeout=30s"
    path=Path(args[1])
    assert path.is_file() and path.stat().st_mode & 0o777 == 0o600
    args=args[5:]
    selected_namespace=None
    if args[:1] == ["-n"]:
        selected_namespace=args[1]
        assert selected_namespace in {"radius-system","default"}
        args=args[2:]
    if args in (["config","view","--minify","-o","json"],
                ["config","view","--minify","--raw","-o","json"]):
        out({"current-context":"kind-"+cluster,
            "contexts":[{"name":"kind-"+cluster,"context":{"cluster":cluster,"user":cluster}}],
            "clusters":[{"name":cluster,"cluster":{"server":"https://127.0.0.1:35495",
                       "certificate-authority-data":"c3ludGhldGljLWNh"}}],
            "users":[{"name":cluster,"user":{"client-certificate-data":"cert","client-key-data":"key"}}]})
    elif args[:2] == ["get","node"]:
        assert args[2] == node
        out({"status":{"addresses":[{"type":"InternalIP","address":"172.18.0.2"}]},
             "metadata":{"name":node,"labels":{
            "plane-demo/project":"foreign" if state.get("foreign_node") else "demo",
            "plane-demo/deployment":"one","plane-demo/environment":"local",
            "radplanes.local/slot":"management"}}})
    elif args[:2] == ["get","namespace"] and args[2] == "kube-system":
        out({"kind":"Namespace","metadata":{"name":"kube-system",
             "uid":"11111111-1111-4111-8111-111111111111"}})
    elif args[:2] == ["get","namespace"] and args[2] == stem+"-access":
        if "access_namespace" in state: out(state["access_namespace"])
        else: assert "--ignore-not-found" in args
    elif args[:2] == ["get","namespace"]:
        assert args[2] == namespace
        if not state.get("complete"): fail()
        out({"metadata":{"name":namespace,"labels":{"plane-demo/project":"demo","plane-demo/deployment":"one",
                                  "plane-demo/environment":"local"},
             "annotations":{"plane-demo/bootstrap-node":node_id,
                            "plane-demo/source-revision":revision}}})
    elif args[:2] == ["patch","deployment"]:
        assert state.get("installed") and args[2] in {"dynamic-rp","applications-rp"}
        patch=json.loads(Path(args[args.index("--patch-file")+1]).read_text())
        if args[2] == "applications-rp":
            state["rp"]=patch
        else: state["overlay"]=patch
        save()
    elif args[:1] == ["create"]:
        raw=sys.stdin.read() if args[-1] == "-" else Path(args[-1]).read_text()
        value=json.loads(raw)
        if value["kind"] == "List":
            for item in value["items"]:
                key=item["kind"]+"/"+item["metadata"]["name"]
                state.setdefault("objects",{})[key]=item
        elif value["metadata"]["name"] == stem+"-access":
            state["access_namespace"]=value
        else:
            assert value["kind"] == "Namespace" and value["metadata"]["name"] == namespace
            state["namespace"]=value
        save()
    elif args[:2] == ["rollout","status"]: assert state.get("installed")
    elif args[:3] == ["get","deployment","applications-rp"]:
        value=state.get("rp",{"spec":{"template":{"spec":{
            "containers":[{"name":"applications-rp"}],"volumes":[]}}}})
        out(value)
    elif args[:3] == ["get","configmap","applications-rp-config"]:
        out(state.get("tf_config",{"data":{"radius-self-host.yaml":
            "terraform:\n  path: /terraform\n  logLevel: TRACE\n"}}))
    elif args[:3] == ["patch","configmap","applications-rp-config"]:
        state["tf_config"]=json.loads(Path(args[args.index("--patch-file")+1]).read_text()); save()
    elif args[:3] == ["get","service","kubernetes"]:
        assert selected_namespace == "default"
        out({"metadata":{"name":"kubernetes","namespace":"default"},"spec":{"clusterIP":"10.96.0.1"}})
    elif args[:1] == ["get"] and args[1] in {"ConfigMap","Deployment","Service"}:
        value=state.get("objects",{}).get(args[1]+"/"+args[2])
        if value is not None: out(value)
        else: assert "--ignore-not-found" in args
    elif args[:2] == ["get","deployment"]:
        assert args[2] == "dynamic-rp"
        out({"spec":{"template":{"metadata":{"labels":{
            "radplanes.local/executor":"management-only"}},"spec":{
            "containers":[{"image":"localhost/"+stem+"-executor:"+revision}],
            "securityContext":{"fsGroupChangePolicy":"OnRootMismatch"}}}}})
    elif args[:1] == ["exec"]:
        assert args[1:5] == ["deployment/dynamic-rp","-c","dynamic-rp","--"]
        if args[5] == "sha256sum": print("b"*64+"  /dynamic-rp")
        else: print("foreign-daemon" if state.get("bad_daemon") else "desktop-daemon")
    elif args[:2] == ["get","--raw"]:
        assert state.get("radius_environment")
        identifier=f"/planes/radius/local/resourceGroups/{stem}/providers/Applications.Core/environments/management"
        assert args[2] == "/apis/api.ucp.dev/v1alpha3"+identifier+"?api-version=2023-10-01-preview"
        value=state.get("registered_environment",{"location":"global","properties":{"compute":{
            "kind":"kubernetes","resourceId":"self",
            "namespace":"foreign" if state.get("bad_environment") else cluster}}})
        out({**value,"id":identifier})
    elif args[:2] == ["annotate","namespace"]:
        assert args[2] == namespace
        state["complete"]=True; save()
    else: raise AssertionError(args)
else: raise AssertionError(tool)
"""


@pytest.fixture
def stage_lab(tmp_path):
    repo = tmp_path / "repository"
    local = repo / "scripts/operations/local"
    local.mkdir(parents=True)
    (repo / "scripts/lib").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/lib/env.sh", repo / "scripts/lib/env.sh")
    shutil.copy(ROOT / "scripts/lib/output.sh", repo / "scripts/lib/output.sh")
    for name in FILES:
        shutil.copy(ROOT / "scripts/operations/local" / name, local / name)
    (repo / "ports.env").write_text("PORT_BLOCK_START=35490\nPORT_BLOCK_END=35499\n")
    (repo / ".env").write_text(
        "DEMO_ENV=local\nDEMO_PROJECT=demo\nDEMO_DEPLOYMENT=one\n"
        'DEMO_KEY_MANAGEMENT="synthetic-private-key-that-must-not-reach-tools"\n'
    )
    (repo / ".env").chmod(0o600)
    for path in (
        "images/api/Dockerfile",
        "images/local-provisioner/Dockerfile",
        "images/radius-kind/Dockerfile",
        "infra/radius/types/clusters.yaml",
        "infra/radius/types/postgresql.yaml",
        "infra/radius/types/gateways.yaml",
    ):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic committed input\n")
    shutil.copytree(
        ROOT / "infra/radius/recipes/local",
        repo / "infra/radius/recipes/local",
        ignore=shutil.ignore_patterns(".terraform"),
        dirs_exist_ok=True,
    )
    shutil.copytree(ROOT / "scripts/recipes/local", repo / "scripts/recipes/local")
    (repo / ".state").mkdir()
    (repo / ".state/do-not-read").write_text("unrelated retained state")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "double"
    executable.write_text(f"#!{sys.executable}\n" + DOUBLE)
    executable.chmod(0o700)
    for name in ("docker", "git", "kind", "kubectl", "rad", "helm", "uv", "encryption-double"):
        (bin_dir / name).symlink_to(executable)
    (local / "encryption.sh").write_text(f'#!/bin/bash\nexec "{bin_dir}/encryption-double" "$@"\n')
    for path in repo.rglob("*"):
        if ".state" in path.relative_to(repo).parts or path.name == ".env":
            continue
        path.chmod(0o755 if path.is_dir() or path.suffix == ".sh" else 0o644)
    home = tmp_path / "operator-home"
    (home / ".rad/bin").mkdir(parents=True)
    bicep = home / ".rad/bin/bicep"
    bicep.write_text('#!/bin/sh\nprintf "Bicep CLI version 0.42.1\\n"\n')
    bicep.chmod(0o700)
    (home / ".rad/config.yaml").write_text("untouched-global-radius")
    (home / ".kube").mkdir()
    (home / ".kube/config").write_text("untouched-global-kube")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "store.json").write_text(
        json.dumps(
            {
                "images": {},
                "containers": {},
                "node_image": NODE_IMAGE,
            }
        )
    )
    return tmp_path


def stored(lab):
    return json.loads((lab / "store.json").read_text())


def change(lab, **values):
    state = stored(lab)
    state.update(values)
    (lab / "store.json").write_text(json.dumps(state))


def calls(lab):
    path = lab / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def execute(lab, script, *args):
    return subprocess.run(
        ["/bin/bash", str(lab / "repository/scripts/operations/local" / script), *args],
        env={
            **os.environ,
            "PATH": str(lab / "bin") + os.pathsep + os.environ["PATH"],
            "HOME": str(lab / "operator-home"),
            "TMPDIR": str(lab / "scratch"),
            "AZURE_CLIENT_SECRET": "must-not-reach-platform-tools",
        },
        cwd=lab / "repository",
        text=True,
        capture_output=True,
        timeout=45,
    )


def report(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout[result.stdout.index("{") :])


def prepared(lab):
    return report(execute(lab, "build.sh"))


def test_build_uses_committed_source_and_inspects_every_image(stage_lab):
    result = execute(stage_lab, "build.sh")
    value = report(result)
    assert value["revision"] == REVISION and value["stem"] == STEM
    assert set(value["images"]) == {"api", "provisioner", "executor", "operator"}
    assert value["coldChildReady"] is False and value["pending"]
    assert "visible-build-output" in result.stdout and "visible-pull-output" in result.stdout
    inspection = [
        item for item in calls(stage_lab) if item["tool"] == "uv" and "inspect" in item["args"]
    ]
    assert len(inspection) == 4
    assert not stored(stage_lab)["containers"]
    assert not list((stage_lab / "scratch").iterdir())
    assert (stage_lab / "repository/.state/do-not-read").read_text() == "unrelated retained state"
    assert all(not item["env"] for item in calls(stage_lab) if item["tool"] != "git")
    state = stored(stage_lab)
    for modes in state["source_modes"].values():
        assert modes == {
            "scripts": 0o755,
            "scripts/operations/local/build.sh": 0o755,
            "scripts/recipes/local/cluster/check-images.sh": 0o755,
            "infra/radius/types/clusters.yaml": 0o644,
            "infra/radius/types/clusters.tgz": 0o644,
        }
    assert all(
        modes == {"work": 0o700, "home": 0o700, "proofs": 0o700} for modes in state["private_modes"]
    )


def test_inspect_never_builds_pulls_or_reuses_a_saved_review(stage_lab):
    prepared(stage_lab)
    (stage_lab / "calls.jsonl").unlink()
    result = report(execute(stage_lab, "build.sh", "inspect"))
    assert result["revision"] == REVISION
    native = [item["args"] for item in calls(stage_lab) if item["tool"] == "docker"]
    assert not any("build" in args or "pull" in args for args in native)
    assert sum("export" in args for args in native) == 4


@pytest.mark.parametrize("failure", ["dirty", "proof_failure", "build_failure"])
def test_build_failures_stop_without_completion_claims(stage_lab, failure):
    change(stage_lab, **{failure: True})
    result = execute(stage_lab, "build.sh")
    assert result.returncode != 0
    assert '"coldChildReady"' not in result.stdout
    assert not stored(stage_lab)["containers"]
    assert not list((stage_lab / "scratch").iterdir()), result.stderr


def test_foreign_image_is_not_overwritten(stage_lab):
    prepared(stage_lab)
    state = stored(stage_lab)
    key = f"localhost/{STEM}-api:{REVISION}"
    state["images"][key]["Config"]["Labels"]["plane-demo/deployment"] = "foreign"
    change(stage_lab, images=state["images"])
    before = len(calls(stage_lab))
    result = execute(stage_lab, "build.sh")
    assert result.returncode != 0
    assert not any("build" in item["args"] for item in calls(stage_lab)[before:])


def test_bootstrap_only_creates_management_and_observes_completed_rerun(stage_lab):
    prepared(stage_lab)
    first = report(execute(stage_lab, "bootstrap.sh"))
    assert first["namespace"] == f"{STEM}-management-management"
    assert first["environmentNamespace"] == f"{STEM}-management"
    assert first["environmentNamespace"] + "-management" == first["namespace"]
    assert first["radiusGroup"] == STEM
    assert first["observedExisting"] is False
    assert first["applicationsDeployed"] is False
    configuration = stored(stage_lab)["kind_config"]
    assert configuration["networking"] == {"apiServerAddress": "127.0.0.1", "apiServerPort": 35495}
    assert configuration["nodes"][0]["extraMounts"] == [
        {
            "hostPath": "/var/run/docker.sock",
            "containerPath": "/run/radplanes/docker.sock",
            "readOnly": False,
        }
    ]
    trace = calls(stage_lab)
    encrypted = next(i for i, item in enumerate(trace) if item["tool"] == "encryption-double")
    install = next(
        i for i, item in enumerate(trace) if item["tool"] == "rad" and "install" in item["args"]
    )
    assert encrypted < install
    second = report(execute(stage_lab, "bootstrap.sh"))
    assert second["observedExisting"] is True
    assert (
        sum(
            item["tool"] == "kind" and item["args"][:2] == ["create", "cluster"]
            for item in calls(stage_lab)
        )
        == 1
    )
    assert (
        sum(item["tool"] == "rad" and "install" in item["args"] for item in calls(stage_lab)) == 1
    )
    assert (stage_lab / "operator-home/.rad/config.yaml").read_text() == "untouched-global-radius"
    assert (stage_lab / "operator-home/.kube/config").read_text() == "untouched-global-kube"
    assert not list((stage_lab / "scratch").iterdir())


@pytest.mark.parametrize("problem", ["partial", "foreign_node", "occupied", "encryption_failure"])
def test_bootstrap_does_not_adopt_or_continue_unsafe_work(stage_lab, problem):
    prepared(stage_lab)
    if problem == "partial":
        change(stage_lab, node=True)
    elif problem == "foreign_node":
        change(stage_lab, node=True, foreign_node=True)
    else:
        change(stage_lab, **{problem: True})
    before = len(calls(stage_lab))
    result = execute(stage_lab, "bootstrap.sh")
    assert result.returncode != 0
    assert not any(
        item["tool"] == "rad" and "install" in item["args"] for item in calls(stage_lab)[before:]
    )
    assert not stored(stage_lab).get("complete")


@pytest.mark.parametrize("problem", ["bad_daemon", "bad_environment"])
def test_bootstrap_completion_requires_live_owner_graph(stage_lab, problem):
    prepared(stage_lab)
    change(stage_lab, **{problem: True})
    result = execute(stage_lab, "bootstrap.sh")
    assert result.returncode != 0
    assert not stored(stage_lab).get("complete")
    assert not list((stage_lab / "scratch").iterdir())


@pytest.mark.parametrize("script", ["build.sh", "bootstrap.sh"])
def test_nonlocal_env_never_calls_platform_tools(stage_lab, script):
    env = stage_lab / "repository/.env"
    env.write_text(
        "DEMO_ENV=azure\nDEMO_PROJECT=demo\nDEMO_DEPLOYMENT=one\n"
        "AZURE_SUBSCRIPTION_ID=11111111-1111-4111-8111-111111111111\nAZURE_LOCATION=centralus\n"
    )
    result = execute(stage_lab, script)
    assert result.returncode != 0
    assert not calls(stage_lab)


@pytest.fixture
def assets_module():
    path = ROOT / "scripts/operations/local/assets.py"
    spec = importlib.util.spec_from_file_location("local_native_assets", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tar_bytes(values):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data in values.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def filesystem_tar(directory: Path) -> bytes:
    output = io.BytesIO()

    def docker_copy_owner(member):
        member.uid = member.gid = 0
        return member

    with tarfile.open(fileobj=output, mode="w") as archive:
        for path in sorted(directory.rglob("*")):
            archive.add(
                path,
                arcname=path.relative_to(directory),
                recursive=False,
                filter=docker_copy_owner,
            )
    return output.getvalue()


def image_tar(values: dict[str, bytes], directory: Path) -> bytes:
    """Materialize a public image fixture; exported modes come from stat, not TarInfo defaults."""
    directory.mkdir(exist_ok=True)
    for existing in directory.rglob("*"):
        if existing.is_file() and existing.relative_to(directory).as_posix() not in values:
            existing.unlink()
    for name, data in values.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        path.write_bytes(data)
        if new:
            path.chmod(0o644)
    for path in directory.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
    return filesystem_tar(directory)


def test_runtime_inspection_calls_existing_byte_proof_not_saved_records(
    assets_module, monkeypatch, tmp_path
):
    helper = assets_module.runtime_helpers(ROOT)
    source = b"committed synthetic API source"
    monkeypatch.setattr(
        helper,
        "expected_hashes",
        lambda role: {
            "src/plane_demo/api.py": hashlib.sha256(source).hexdigest(),
        },
    )
    monkeypatch.setattr(assets_module, "runtime_helpers", lambda root: helper)
    archive = tmp_path / "rootfs.tar"
    values = {"app/src/plane_demo/api.py": source, "usr/local/bin/python3.13": b"interpreter"}
    archive.write_bytes(image_tar(values, tmp_path / "image"))
    result = assets_module.inspect_image(ROOT, "api", "arm64", archive, None)
    assert result["source_hashes"]["src/plane_demo/api.py"] == hashlib.sha256(source).hexdigest()
    values["app/src/plane_demo/api.py"] = b"changed image bytes"
    archive.write_bytes(image_tar(values, tmp_path / "image"))
    with pytest.raises(RuntimeError, match="source or Python interpreter"):
        assets_module.inspect_image(ROOT, "api", "arm64", archive, None)


@pytest.mark.parametrize(
    "role,name",
    [
        ("api", "app/src/plane_demo/api.py"),
        ("provisioner", "app/scripts/operations/local/setup.sh"),
        ("operator", "opt/radplanes/bootstrap/radius.tgz"),
        ("executor", "opt/radplanes/providers/registry.terraform.io/example/provider.zip"),
    ],
)
def test_image_permissions_use_actual_exported_file_and_directory_modes(
    assets_module,
    tmp_path,
    role,
    name,
):
    directory = tmp_path / "image"
    image_tar({name: b"non-secret runtime input"}, directory)
    path = directory / name
    with tarfile.open(fileobj=io.BytesIO(filesystem_tar(directory))) as archive:
        modes = assets_module.runtime_permissions(archive, role)
    assert modes[name] == path.stat().st_mode & 0o7777
    path.chmod(0o600)
    with tarfile.open(fileobj=io.BytesIO(filesystem_tar(directory))) as archive:
        with pytest.raises(ValueError, match="cannot access"):
            assets_module.runtime_permissions(archive, role)
    path.chmod(0o644)
    path.parent.chmod(0o700)
    with tarfile.open(fileobj=io.BytesIO(filesystem_tar(directory))) as archive:
        with pytest.raises(ValueError, match="cannot access|cannot traverse"):
            assets_module.runtime_permissions(archive, role)


@pytest.mark.parametrize("role", ["operator", "provisioner"])
def test_dockerfile_normalizes_only_nonsecret_prepared_artifacts(assets_module, tmp_path, role):
    image = tmp_path / "image"
    artifacts = image / "opt/radplanes"
    package = artifacts / "bootstrap/modules/cluster"
    package.mkdir(parents=True)
    (image / "opt").chmod(0o755)
    (package / "archive.tar.gz").write_bytes(b"module")
    (artifacts / "terraform").write_bytes(b"executable")
    for path in artifacts.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    artifacts.chmod(0o700)
    profile = image / "home/operator/.kube/config"
    profile.parent.mkdir(parents=True, mode=0o700)
    profile.write_text("private configuration")
    profile.chmod(0o600)
    source = (ROOT / "scripts/operations/local/bootstrap-assets.Dockerfile").read_text()
    normalization = source.split("RUN find /opt/radplanes", 1)[1].split("\nENV ", 1)[0]
    command = ("find /opt/radplanes" + normalization).replace("/opt/radplanes", str(artifacts))
    subprocess.run(["/bin/sh", "-ec", command], check=True, capture_output=True)
    assert profile.stat().st_mode & 0o777 == 0o600
    assert profile.parent.stat().st_mode & 0o777 == 0o700
    with tarfile.open(fileobj=io.BytesIO(filesystem_tar(image))) as archive:
        modes = assets_module.runtime_permissions(archive, role)
    assert modes["opt/radplanes/terraform"] == 0o755
    assert modes["opt/radplanes/bootstrap/modules/cluster/archive.tar.gz"] == 0o644
    for target in ("operator", "provisioner"):
        stage = source.split(f"AS {target}\n", 1)[1].split("\nFROM ", 1)[0]
        assert "COPY --from=executor /opt/radplanes /opt/radplanes" in stage
        assert "COPY scripts/operations/local/.packaged" not in stage


@pytest.mark.parametrize(
    "relative,missing",
    [
        ("scripts/operations/install-radius.sh", True),
        ("scripts/operations/install-radius.sh", False),
        ("scripts/operations/management_job.py", True),
        ("scripts/operations/management_job.py", False),
        ("scripts/operations/azure/registry_policy.py", True),
        ("scripts/operations/azure/registry_policy.py", False),
        ("scripts/operations/azure/registry-policy.json", True),
        ("scripts/operations/azure/registry-policy.json", False),
        ("src/plane_demo/management/providers/identity.py", False),
        ("scripts/operations/config.py", False),
    ],
)
def test_native_provisioner_proof_checks_current_shared_source(
    assets_module,
    monkeypatch,
    tmp_path,
    relative,
    missing,
):
    helper = assets_module.runtime_helpers(ROOT)
    expected = helper.expected_hashes("provisioner")
    assert expected[relative] == hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    assert helper.OPERATOR_FILES.count("install-radius.sh") == 1
    assert helper.OPERATOR_FILES.count("management_job.py") == 1
    assert helper.OPERATOR_FILES.count("azure/registry_policy.py") == 1
    assert helper.OPERATOR_FILES.count("azure/registry-policy.json") == 1
    public = helper.expected_hashes("api")
    assert "scripts/operations/install-radius.sh" not in public
    assert "scripts/operations/management_job.py" not in public
    assert "scripts/operations/azure/registry_policy.py" not in public
    assert "scripts/operations/azure/registry-policy.json" not in public
    assert "src/plane_demo/management/providers/identity.py" not in public
    monkeypatch.setattr(assets_module, "runtime_helpers", lambda root: helper)
    monkeypatch.setattr(helper, "TOOL_HASHES", {"arm64": {}})
    monkeypatch.setattr(helper, "expected_extensions", lambda: {})
    monkeypatch.setattr(assets_module, "bundle_proof", lambda *args: {})
    values = {f"app/{name}": (ROOT / name).read_bytes() for name in expected}
    values["usr/local/bin/python3.13"] = b"synthetic interpreter"
    values["opt/radplanes/bootstrap/radius.tgz"] = gzip.compress(
        tar_bytes(
            {
                "radius/Chart.yaml": b"name: radius\nversion: 0.60.2\n",
            }
        )
    )
    archives, server = assets_module.recipe_assets.source_archives(ROOT)
    values["opt/radplanes/bootstrap/module-server.py"] = server
    values["opt/radplanes/bootstrap/terraform-init.py"] = (
        ROOT / "scripts/operations/local/terraform-init.py"
    ).read_bytes()
    for kind, content in archives.items():
        values[f"opt/radplanes/bootstrap/modules/{kind}/archive.tar.gz"] = content
    values["opt/radplanes/bootstrap/images.json"] = json.dumps(
        [
            {
                "reference": "ghcr.io/radius-project/ucpd:0.60",
                "id": "sha256:" + "d" * 64,
            }
        ]
    ).encode()
    archive = tmp_path / "provisioner-rootfs.tar"
    archive.write_bytes(image_tar(values, tmp_path / "image"))
    result = assets_module.inspect_image(ROOT, "provisioner", "arm64", archive, None)
    assert result["source_hashes"] == expected
    if missing:
        values.pop(f"app/{relative}")
    else:
        values[f"app/{relative}"] = b"stale pre-integration source"
    archive.write_bytes(image_tar(values, tmp_path / "image"))
    with pytest.raises(RuntimeError, match="source or Python interpreter"):
        assets_module.inspect_image(ROOT, "provisioner", "arm64", archive, None)


def test_provider_and_terraform_packages_are_compared_to_actual_pins(
    assets_module, monkeypatch, tmp_path
):
    provider = b"synthetic provider zip"
    lock = (
        'provider "registry.terraform.io/hashicorp/example" {\n  version = "1.0.0"\n'
        f'  hashes = ["zh:{hashlib.sha256(provider).hexdigest()}"]\n}}\n'
    )
    for name in ("cluster", "postgresql", "redis", "gateway"):
        directory = tmp_path / "infra/radius/recipes/local" / name
        directory.mkdir(parents=True)
        (directory / ".terraform.lock.hcl").write_text(lock)
    directory = tmp_path / "scripts/operations/local"
    directory.mkdir(parents=True)
    configuration = (ROOT / "scripts/operations/local/terraform.tfrc").read_bytes()
    (directory / "terraform.tfrc").write_bytes(configuration)
    binary = b"\x7fELFsynthetic-terraform"
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("terraform", binary)
    monkeypatch.setitem(
        assets_module.TERRAFORM_HASHES, "arm64", hashlib.sha256(zipped.getvalue()).hexdigest()
    )
    path = (
        "opt/radplanes/providers/registry.terraform.io/hashicorp/example/"
        "terraform-provider-example_1.0.0_linux_arm64.zip"
    )
    values = {
        path: provider,
        "opt/radplanes/terraform": binary,
        "opt/radplanes/terraform.zip": zipped.getvalue(),
        "opt/radplanes/terraform.tfrc": configuration,
    }
    with tarfile.open(fileobj=io.BytesIO(tar_bytes(values))) as archive:
        assert assets_module.bundle_proof(archive, tmp_path, "arm64")["providers"]
    values["opt/radplanes/terraform"] = b"\x7fELFwrong-binary"
    with tarfile.open(fileobj=io.BytesIO(tar_bytes(values))) as archive:
        with pytest.raises(ValueError, match="binary differs"):
            assets_module.bundle_proof(archive, tmp_path, "arm64")
    values["opt/radplanes/terraform"] = binary
    values[path] = b"foreign provider"
    with tarfile.open(fileobj=io.BytesIO(tar_bytes(values))) as archive:
        with pytest.raises(ValueError, match="provider differs"):
            assets_module.bundle_proof(archive, tmp_path, "arm64")


def test_chart_images_include_dynamic_bicep_jobs_and_reject_foreign_registry(assets_module):
    rendered = (
        yaml.safe_dump(
            {
                "kind": "ConfigMap",
                "data": {"radius-self-host.yaml": "image: ghcr.io/radius-project/bicep:0.60"},
            }
        )
        + "---\n"
        + yaml.safe_dump(
            {
                "kind": "Pod",
                "spec": {"containers": [{"image": "ghcr.io/radius-project/ucpd:0.60"}]},
            }
        )
    )
    assert assets_module.radius_images(rendered, "localhost/selected") == [
        "ghcr.io/radius-project/bicep:0.60",
        "ghcr.io/radius-project/ucpd:0.60",
    ]
    with pytest.raises(ValueError, match="registry"):
        assets_module.radius_images("spec:\n  containers:\n  - image: foreign.example/api:v1", "")


def test_overlay_keeps_management_only_docker_and_private_terraform(assets_module):
    value = assets_module.executor_overlay(ROOT, "localhost/selected-executor", 20)
    spec = value["spec"]["template"]["spec"]
    assert spec["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    assert spec["securityContext"]["supplementalGroups"] == [20]
    assert spec["initContainers"][0]["image"] == "localhost/selected-executor"
    assert spec["volumes"] == [
        {
            "name": "local-docker",
            "hostPath": {"path": "/run/radplanes/docker.sock", "type": "Socket"},
        }
    ]


def test_canonical_setup_registers_prepared_recipes_and_reports_live_factory_inputs(stage_lab):
    prepared(stage_lab)
    report(execute(stage_lab, "bootstrap.sh"))
    value = report(execute(stage_lab, "setup.sh"))
    assert value["bootstrapIdentity"]["DEMO_PROJECT"] == "demo"
    assert value["bootstrapIdentity"]["DEMO_DEPLOYMENT"] == "one"
    assert value["managementCluster"]["nodeAddress"] == "172.18.0.2"
    assert value["managementCluster"]["serviceAddress"] == "10.96.0.1"
    assert value["managementCluster"]["clusterId"] == f"kind://{STEM}-management"
    assert set(value["recipes"]) == {"cluster", "postgresql", "redis", "gateway"}
    state = stored(stage_lab)
    recipes = state["registered_environment"]["properties"]["recipes"]
    environment_namespace = state["registered_environment"]["properties"]["compute"]["namespace"]
    assert environment_namespace == f"{STEM}-management"
    assert environment_namespace + "-management" == state["namespace"]["metadata"]["name"]
    cluster = recipes["Demo.Platform/clusters"]["default"]["parameters"]
    assert cluster["resource_prefix"] == cluster["radius_group"] == STEM
    assert cluster["access_namespace"] == STEM + "-access"
    assert set(cluster["runtime_images"]) == {"api", "provisioner", "operator"}
    assert "-operator:" in cluster["runtime_images"]["operator"]["reference"]
    images = [*cluster["runtime_images"].values(), *cluster["dependency_images"]]
    assert not any("-executor:" in image["reference"] for image in images)
    assert all(image["image_id"].startswith("sha256:") for image in images)
    assert len(state["objects"]) == 12
    for item in state["objects"].values():
        if item["kind"] == "ConfigMap":
            descriptor = json.loads(item["data"]["module.json"])
            assert "images" not in descriptor and "dependencies" not in descriptor
        if item["kind"] == "Deployment":
            pod = item["spec"]["template"]["spec"]
            assert pod["automountServiceAccountToken"] is False
            assert pod["containers"][0]["imagePullPolicy"] == "Never"
            assert "/opt/radplanes/bootstrap/module-server.py" in pod["containers"][0]["command"]
            assert "volumes" not in pod
    assert report(execute(stage_lab, "setup.sh", "inspect")) == value
    assert not list((stage_lab / "scratch").iterdir())
    assert (
        sum(
            item["tool"] == "kind" and item["args"][:2] == ["create", "cluster"]
            for item in calls(stage_lab)
        )
        == 1
    )


def test_setup_does_not_bootstrap_a_missing_prerequisite(stage_lab):
    prepared(stage_lab)
    result = execute(stage_lab, "setup.sh")
    assert result.returncode != 0
    assert "canonical local bootstrap" in result.stderr
    assert not any(item["tool"] == "kind" and "create" in item["args"] for item in calls(stage_lab))


def test_operator_terraform_init_copies_prepared_assets_without_network(tmp_path, monkeypatch):
    source = tmp_path / "prepared"
    providers = source / "providers/registry.terraform.io/hashicorp/example"
    providers.mkdir(parents=True)
    (providers / "provider.zip").write_bytes(b"locked-provider-package")
    (source / "terraform").write_bytes(b"prepared-terraform")
    (source / "terraform.tfrc").write_bytes(
        (ROOT / "scripts/operations/local/terraform.tfrc").read_bytes()
    )
    spec = importlib.util.spec_from_file_location(
        "prepared_terraform_init", ROOT / "scripts/operations/local/terraform-init.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ownership = {}
    real_touch, real_chmod = Path.touch, Path.chmod

    def chown(path, uid, gid):
        ownership[Path(path)] = (uid, gid)

    def constrained_touch(path, *args, **kwargs):
        if path.exists() and ownership.get(path, (0, 0))[0] != 0:
            raise PermissionError("CHOWN-only root cannot touch another user's private marker")
        return real_touch(path, *args, **kwargs)

    def constrained_chmod(path, mode, *args, **kwargs):
        if ownership.get(path, (0, 0))[0] != 0:
            raise PermissionError("CHOWN-only root cannot chmod another user's file")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(module.os, "chown", chown)
    monkeypatch.setattr(Path, "touch", constrained_touch)
    monkeypatch.setattr(Path, "chmod", constrained_chmod)
    target = tmp_path / "terraform"
    module.initialize(source, target)
    marker = target / ".terraform-global/.terraform-ready"
    assert ownership[marker] == (65532, 65532)
    assert marker.stat().st_mode & 0o777 == 0o600
    real_chmod(marker, 0o644)
    with pytest.raises(RuntimeError, match="terraform_marker_not_private"):
        module.initialize(source, target)
    real_chmod(marker, 0o600)
    assert (target / "terraform").read_bytes() == b"prepared-terraform"
    assert (target / ".terraform-global/terraform").read_bytes() == b"prepared-terraform"
    assert (target / ".terraform-global/.terraform-ready").is_file()
    assert (
        target / "providers/registry.terraform.io/hashicorp/example/provider.zip"
    ).read_bytes() == (b"locked-provider-package")
    assert str(target / "providers") in (target / "terraform.tfrc").read_text()
    assert "direct {" not in (target / "terraform.tfrc").read_text()
    assert (target / "terraform").stat().st_mode & 0o777 == 0o700
    module.initialize(source, target)
    outside = tmp_path / "must-not-change"
    outside.write_bytes(b"preserve")
    (target / "providers/registry.terraform.io/hashicorp/example/provider.zip").unlink()
    (target / "providers/registry.terraform.io/hashicorp/example/provider.zip").symlink_to(outside)
    with pytest.raises(RuntimeError, match="symlink"):
        module.initialize(source, target)
    assert outside.read_bytes() == b"preserve"
    (target / "providers/registry.terraform.io/hashicorp/example/provider.zip").unlink()
    (source / "terraform").unlink()
    with pytest.raises(RuntimeError, match="prepared_terraform_assets_missing"):
        module.initialize(source, target)
