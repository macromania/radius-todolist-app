import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENT = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def installation(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir(mode=0o700)
    kubeconfig, config = workspace / "kubeconfig", workspace / "radius.yaml"
    for path in (kubeconfig, config):
        path.write_text("{}")
        path.chmod(0o600)
    compiler = tmp_path / "bicep"
    compiler.write_text("#!/bin/sh\nexit 0\n")
    compiler.chmod(0o700)
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands.jsonl"
    program = tools / "fake"
    program.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ["TEST_COMMAND_LOG"], "a") as stream:
    stream.write(json.dumps({"args": [name, *sys.argv[1:]], "home": os.environ["HOME"],
                             "azure": os.environ.get("AZURE_CONFIG_DIR"),
                             "kubeconfig": os.environ.get("KUBECONFIG")}) + "\\n")
if name == "kubectl" and "current-context" in sys.argv:
    print(os.environ["TEST_CONTEXT"])
elif name == "kubectl" and "pods" in sys.argv:
    accounts = ["applications-rp", "bicep-de", "ucp", "dynamic-rp"]
    if os.environ.get("TEST_MISSING_ACCOUNT"):
        accounts.pop()
    print(json.dumps({"items": [{"metadata": {}, "spec": {"serviceAccountName": account,
        "containers": [{"env": [
            {"name": "AZURE_CLIENT_ID", "value": os.environ["TEST_CLIENT"]},
            {"name": "AZURE_TENANT_ID", "value": os.environ["TEST_TENANT"]},
            {"name": "AZURE_FEDERATED_TOKEN_FILE", "value": "/projected/token"}
        ]}]}} for account in accounts]}))
"""
    )
    program.chmod(0o700)
    for tool in ("rad", "kubectl"):
        (tools / tool).symlink_to(program)
    context = "sample-demo-azure-management"
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "BICEP_BIN": str(compiler),
        "TEST_COMMAND_LOG": str(log),
        "TEST_CONTEXT": context,
        "TEST_CLIENT": CLIENT,
        "TEST_TENANT": TENANT,
    }
    args = [
        "bash",
        str(ROOT / "scripts/operations/install-radius.sh"),
        "--context",
        context,
        "--kubeconfig",
        str(kubeconfig),
        "--config",
        str(config),
        "--client-id",
        CLIENT,
        "--tenant-id",
        TENANT,
        "--workspace-root",
        str(workspace),
    ]
    return args, env, workspace, log


@pytest.mark.parametrize("explicit_cache", [False, True])
def test_native_entrypoint_scopes_every_command_and_removes_its_temporary_home(
    installation, explicit_cache
):
    args, env, workspace, log = installation
    if explicit_cache:
        env["AZURE_CONFIG_DIR"] = str(workspace.parent / "selected-azure-cache")
    else:
        env.pop("AZURE_CONFIG_DIR", None)
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["workload_identity_verified"] is True
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    homes = set()
    for call in commands:
        command = call["args"]
        if "current-context" in command:
            continue
        homes.add(call["home"])
        assert Path(call["home"]).is_relative_to(workspace)
        assert call["kubeconfig"] == str(workspace / "kubeconfig")
        assert call["azure"] == (env.get("AZURE_CONFIG_DIR") or str(Path(env["HOME"]) / ".azure"))
        if command[0] == "kubectl":
            assert command[1:5] == [
                "--kubeconfig",
                str(workspace / "kubeconfig"),
                "--context",
                env["TEST_CONTEXT"],
            ]
        else:
            assert command[:3] == ["rad", "--config", str(workspace / "radius.yaml")]
    assert homes and all(not Path(home).exists() for home in homes)
    for account in ("applications-rp", "bicep-de", "ucp", "dynamic-rp"):
        related = [call["args"] for call in commands if f"deployment/{account}" in call["args"]]
        assert "restart" in related[0] and "status" in related[1]
    assert not (workspace.parent / ".state").exists()


@pytest.mark.parametrize("invalid", ["context", "outside", "permissions", "missing-account"])
def test_native_installer_refuses_invalid_access_or_incomplete_identity(installation, invalid):
    args, env, workspace, log = installation
    if invalid == "context":
        args[args.index("--context") + 1] = "foreign-context"
    elif invalid == "outside":
        foreign = workspace.parent / "foreign.kubeconfig"
        foreign.write_text("{}")
        foreign.chmod(0o600)
        args[args.index("--kubeconfig") + 1] = str(foreign)
    elif invalid == "permissions":
        (workspace / "kubeconfig").chmod(0o644)
    else:
        env["TEST_MISSING_ACCOUNT"] = "yes"
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert not list(workspace.glob("radius-install-*"))
    if invalid != "missing-account":
        commands = (
            [json.loads(line)["args"] for line in log.read_text().splitlines()]
            if log.exists()
            else []
        )
        assert all("current-context" in command for command in commands)


def test_management_install_caller_forwards_the_explicit_workspace(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "management_install_project", ROOT / "scripts/operations/project.py"
    )
    project = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(project)
    state = tmp_path / "work"
    state.mkdir(mode=0o700)
    (state / "bootstrap.outputs.json").write_text(
        json.dumps(
            {
                "managementCluster": {
                    "name": "aks-radplanes-management",
                    "resourceGroup": "rg-radplanes-management-cluster",
                },
                "allocations": [
                    {"slot": "management", "identities": {"radius": {"clientId": CLIENT}}}
                ],
                "foundation": {"tenantId": TENANT},
            }
        )
    )
    calls = []
    monkeypatch.setattr(project, "require_confirmation", lambda _: None)
    monkeypatch.setattr(project, "state_dir", lambda _: state)
    monkeypatch.setattr(project, "run", lambda args, **kwargs: calls.append(args) or "")
    project.install_management_radius()
    command = calls[-1]
    assert command[:2] == ["bash", "scripts/operations/install-radius.sh"]
    assert command[command.index("--workspace-root") + 1] == str(state)
