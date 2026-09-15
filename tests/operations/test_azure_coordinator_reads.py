"""Exact-resource Reader contracts; no subscription-wide discovery grant."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"


@pytest.fixture(scope="module")
def foundation():
    compiler = Path.home() / ".rad/bin/bicep"
    if not compiler.is_file():
        pytest.skip("Radius Bicep is not installed")
    result = subprocess.run(
        [str(compiler), "build", str(ROOT / "infra/bootstrap/azure.bicep"), "--stdout"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    return json.loads(result.stdout)


def module(foundation, suffix):
    return next(
        resource
        for resource in foundation["resources"]
        if resource["type"] == "Microsoft.Resources/deployments" and suffix in resource["name"]
    )


def test_bootstrap_read_is_wired_to_the_selected_record_and_actual_coordinator(foundation):
    read_module = module(foundation, "-bootstrap-read")
    inputs = read_module["properties"]["parameters"]
    assert inputs["bootstrapDeploymentName"]["value"] == (
        "[format('{0}-bootstrap', variables('prefix'))]"
    )
    assert "coordinator-identity" in inputs["coordinatorPrincipalId"]["value"]
    assert ".outputs.identity.value.principalId" in inputs["coordinatorPrincipalId"]["value"]
    assert "scope" not in read_module
    assert read_module["properties"]["mode"] == "Incremental"


def test_deployment_reader_cannot_read_sibling_deployments_or_write_any_resource(foundation):
    template = module(foundation, "-bootstrap-read")["properties"]["template"]
    assert template["variables"]["reader"] == READER
    (assignment,) = template["resources"]
    assert assignment["type"] == "Microsoft.Authorization/roleAssignments"
    assert assignment["scope"] == (
        "[subscriptionResourceId('Microsoft.Resources/deployments', "
        "parameters('bootstrapDeploymentName'))]"
    )
    assert assignment["properties"]["principalId"] == "[parameters('coordinatorPrincipalId')]"
    assert assignment["properties"]["principalType"] == "ServicePrincipal"
    assert assignment["properties"]["roleDefinitionId"] == (
        "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', variables('reader'))]"
    )
    assert "parameters('coordinatorPrincipalId')" in assignment["name"]
    assert "bootstrapDeploymentName" in assignment["name"]
    assert set(assignment["properties"]) == {
        "principalId",
        "principalType",
        "roleDefinitionId",
        "description",
    }
    assert "dependsOn" not in assignment


def test_registry_reader_is_exact_resource_scope_and_has_no_delegation(foundation):
    template = module(foundation, "platform-access")["properties"]["template"]
    assert template["variables"]["reader"] == READER
    assignments = [
        resource
        for resource in template["resources"]
        if resource["type"] == "Microsoft.Authorization/roleAssignments"
        and resource["properties"].get("principalId") == "[parameters('coordinatorPrincipalId')]"
    ]
    (assignment,) = assignments
    assert assignment["scope"] == (
        "[resourceId('Microsoft.ContainerRegistry/registries', "
        "parameters('foundation').registryName)]"
    )
    assert assignment["properties"]["principalType"] == "ServicePrincipal"
    assert assignment["properties"]["roleDefinitionId"] == (
        "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', variables('reader'))]"
    )
    assert "parameters('coordinatorPrincipalId')" in assignment["name"]
    assert set(assignment["properties"]) == {
        "principalId",
        "principalType",
        "roleDefinitionId",
        "description",
    }


def test_no_direct_subscription_reader_or_new_permission_definition_is_added(foundation):
    for resource in foundation["resources"]:
        if resource["type"] == "Microsoft.Authorization/roleAssignments":
            assert resource.get("scope") not in (None, "/", "[subscription().id]")
    read_template = module(foundation, "-bootstrap-read")["properties"]["template"]
    assert not any(
        item["type"] == "Microsoft.Authorization/roleDefinitions"
        for item in read_template["resources"]
    )
