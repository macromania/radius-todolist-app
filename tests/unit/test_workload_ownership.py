import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_local_deploy_caller import caller as caller
from test_provisioner import raw_config as raw_config
from test_provisioner import selected_config as selected_config

from plane_demo.management.providers.azure import AzureProvider
from plane_demo.management.providers.credentials import Credentials
from plane_demo.management.providers.identity import DemoConfig
from plane_demo.management.providers.local import LocalProvider

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("environment", ["azure", "local"])
@pytest.mark.parametrize("slot", ["management", "shared-control", "shared-data"])
def test_real_deployment_passes_the_same_owners_to_namespaces_and_workloads(
    selected_config, caller, tmp_path, monkeypatch, environment, slot
):
    config = (
        selected_config
        if environment == "azure"
        else caller.script.selected_config(DemoConfig("local", "demo", "one"), caller.public)
    )
    provider_class = AzureProvider if environment == "azure" else LocalProvider
    provider = provider_class(
        config,
        tmp_path,
        Credentials(tmp_path / "credentials.json", environment=environment),
        workspace=tmp_path / "work",
    )
    manifests, deployments = [], []
    monkeypatch.setattr(
        provider,
        "apply",
        lambda slot, values, **kwargs: manifests.extend(
            values if isinstance(values, list) else [values]
        ),
    )
    monkeypatch.setattr(provider, "kube_get", lambda *args: None)
    monkeypatch.setattr(provider, "initialize_database", lambda *_: None)
    monkeypatch.setattr(provider, "runtime_secrets", lambda *_: None)
    monkeypatch.setattr(
        provider, "deploy", lambda slot, role, app, values: deployments.append(dict(values))
    )
    monkeypatch.setattr(provider, "kubectl", MagicMock(return_value=""))
    monkeypatch.setattr(provider.commands, "run", MagicMock(return_value=""))
    monkeypatch.setattr(provider, "record_endpoint", MagicMock())
    if environment == "azure":
        uri = (
            f"https://{config.foundation['vaultName']}.vault.azure.net/secrets/"
            + config.allocation(slot)["certificateName"]
        )
        url = "https://plane.centralus.cloudapp.azure.com"
        gateway = {
            "host": "plane.centralus.cloudapp.azure.com",
            "url": url,
            "certificateSecretUri": uri,
        }
        monkeypatch.setattr(provider, "current_certificate", lambda _: None)
        monkeypatch.setattr(provider, "certificate", lambda *_: uri)
        monkeypatch.setattr(provider, "tag_redis_nic", MagicMock())
    else:
        url = f"http://127.0.0.1:{config.allocation(slot)['gatewayPort']}"
        gateway = {"host": "127.0.0.1", "url": url}

    def current_gateway(*_):
        if environment == "azure" and len(deployments) == 1:
            return {**gateway, "url": url.replace("https:", "http:"), "certificateSecretUri": ""}
        return gateway

    monkeypatch.setattr(provider, "resource", current_gateway)
    assert provider.deploy_plane(slot) == url
    expected = {
        "plane-demo/project": config.identity.project,
        "plane-demo/deployment": config.identity.deployment,
        "plane-demo/environment": environment,
    }
    namespace = next(item for item in manifests if item["kind"] == "Namespace")
    assert namespace["metadata"]["labels"] == expected
    assert len(deployments) == (2 if environment == "azure" else 1)
    assert all(parameters["ownershipLabels"] == expected for parameters in deployments)
    assert not any(item["kind"] in {"StorageClass", "PersistentVolumeClaim"} for item in manifests)


@pytest.mark.parametrize("application", ["management", "control", "data"])
def test_compiled_applications_forward_owners_to_every_workload_and_challenge(application):
    result = subprocess.run(
        [
            str(Path.home() / ".rad/bin/bicep"),
            "build",
            str(ROOT / f"infra/radius/apps/{application}.bicep"),
            "--stdout",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    template = json.loads(result.stdout)
    count = 0

    def visit(template):
        nonlocal count
        resources = template.get("resources", {})
        for resource in resources.values() if isinstance(resources, dict) else resources:
            if resource["type"] == "Microsoft.Resources/deployments":
                properties = resource["properties"]
                nested = properties["template"]
                if "ownershipLabels" in nested.get("parameters", {}):
                    assert properties["parameters"]["ownershipLabels"]["value"] == (
                        "[parameters('ownershipLabels')]"
                    )
                visit(nested)
            elif resource["type"].startswith("Applications.Core/containers@"):
                properties = resource["properties"]["properties"]
                metadata = next(
                    item
                    for item in properties["extensions"]
                    if item["kind"] == "kubernetesMetadata"
                )
                assert metadata["labels"] == (
                    "[union(parameters('ownershipLabels'), "
                    "createObject('azure.workload.identity/use', "
                    "if(parameters('workloadIdentity'), 'true', 'false'), "
                    "'plane-demo/component', parameters('name')))]"
                )
                count += 1

    visit(template)
    assert count == 3
