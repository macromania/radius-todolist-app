import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICY = json.loads((ROOT / "scripts/operations/azure/registry-policy.json").read_text())
SPEC = importlib.util.spec_from_file_location(
    "registry_policy", ROOT / "scripts/operations/azure/registry_policy.py"
)
policy_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy_check)
PREFIX = "Microsoft.ContainerRegistry/registries/repositories/"


def definitions():
    return [
        {
            "name": POLICY["repositoryReaderRoleId"],
            "permissions": [
                {
                    "actions": [],
                    "dataActions": [PREFIX + "content/read", PREFIX + "metadata/read"],
                }
            ],
        },
        {
            "name": POLICY["repositoryWriterRoleId"],
            "permissions": [
                {
                    "actions": [],
                    "dataActions": [
                        PREFIX + kind + "/" + action
                        for kind in ("content", "metadata")
                        for action in ("read", "write")
                    ],
                }
            ],
        },
        {
            "name": POLICY["dataImporterRoleId"],
            "permissions": [
                {
                    "actions": [
                        "Microsoft.ContainerRegistry/registries/importImage/action",
                        "Microsoft.ContainerRegistry/registries/read",
                        "Microsoft.ContainerRegistry/registries/pull/read",
                    ],
                    "dataActions": [
                        PREFIX + "content/read",
                        PREFIX + "metadata/read",
                        "Microsoft.ContainerRegistry/registries/catalog/read",
                    ],
                }
            ],
        },
    ]


def assignments():
    return [
        {
            "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/"
            + role["name"],
            **(
                {"conditionVersion": "2.0", "condition": POLICY["writerCondition"]}
                if role["name"] == POLICY["repositoryWriterRoleId"]
                else {}
            ),
        }
        for role in definitions()
    ]


def condition_allows(condition, action, repository):
    actions = re.findall(r"ActionMatches\{'([^']+)'\}", condition)
    exact = re.findall(r"StringEqualsIgnoreCase '([^']+)'", condition)
    prefixes = re.findall(r"StringStartsWithIgnoreCase '([^']+)'", condition)
    return action in actions and (
        repository.lower() in exact
        or any(repository.lower().startswith(prefix) for prefix in prefixes)
    )


@pytest.fixture(scope="module")
def platform_template():
    compiler = Path.home() / ".rad/bin/bicep"
    if not compiler.is_file():
        pytest.skip("Radius Bicep is not installed")
    result = subprocess.run(
        [str(compiler), "build", str(ROOT / "infra/bootstrap/platform-access.bicep"), "--stdout"],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_compiled_writer_condition_is_the_documented_policy(platform_template):
    roles = [
        item
        for item in platform_template["resources"]
        if item["type"] == "Microsoft.Authorization/roleAssignments"
    ]
    (writer,) = [item for item in roles if "condition" in item["properties"]]
    embedded = platform_template["variables"]["registryPolicy"]
    while isinstance(embedded, str):
        alias = re.fullmatch(r"\[variables\('([^']+)'\)\]", embedded)
        assert alias is not None
        embedded = platform_template["variables"][alias[1]]
    assert embedded == POLICY
    assert (
        writer["properties"]["conditionVersion"] == "[variables('registryPolicy').conditionVersion]"
    )
    assert writer["properties"]["condition"] == "[variables('registryPolicy').writerCondition]"
    assert (
        "variables('registryPolicy').repositoryWriterRoleId"
        in writer["properties"]["roleDefinitionId"]
    )
    assert re.findall(r"ActionMatches\{'([^']+)'\}", POLICY["writerCondition"]) == [
        PREFIX + "content/read",
        PREFIX + "metadata/read",
        PREFIX + "content/write",
        PREFIX + "metadata/write",
    ]


@pytest.mark.parametrize(
    "repository",
    [
        "radius-recipes/cluster",
        "radius-recipes/postgresql",
        "radius-recipes/gateway",
        "radius-recipes/redis",
        "RADIUS-RECIPES/cluster",
        "radius-recipe-staging-foreign/cluster",
        "plane-api-foreign",
        "plane-provisioner/foreign",
    ],
)
@pytest.mark.parametrize(
    "action",
    [
        PREFIX + "content/write",
        PREFIX + "metadata/write",
        PREFIX + "content/delete",
        PREFIX + "metadata/delete",
    ],
)
def test_canonical_content_tags_and_unlock_operations_are_excluded(repository, action):
    assert condition_allows(POLICY["writerCondition"], action, repository) is False


@pytest.mark.parametrize(
    "repository", ["plane-api", "plane-provisioner", "radius-recipe-staging/cluster"]
)
@pytest.mark.parametrize("action", [PREFIX + "content/write", PREFIX + "metadata/write"])
def test_only_image_and_staging_repositories_allow_data_plane_writes(repository, action):
    assert condition_allows(POLICY["writerCondition"], action, repository) is True


def test_runtime_grants_are_read_only_without_catalog_or_import(platform_template):
    readers = [
        item
        for item in platform_template["resources"]
        if item.get("copy", {}).get("name") == "registryPull"
    ]
    assert len(readers) == 1
    assert (
        "variables('registryPolicy').repositoryReaderRoleId"
        in readers[0]["properties"]["roleDefinitionId"]
    )
    assert "condition" not in readers[0]["properties"]
    contents = json.dumps(platform_template)
    assert "7f951dda-4ed3-4680-a7ca-43fe172d538d" not in contents
    assert "8311e382-0749-4cb8-b61a-304f252e45ec" not in contents
    assert "bfdb9389-c9a5-478a-bb2f-ba9ca092c3c7" not in contents
    assert POLICY["dataImporterRoleId"] in contents


def test_import_is_control_plane_and_does_not_enable_metadata_or_content_write():
    importer = definitions()[2]
    assert (
        "Microsoft.ContainerRegistry/registries/importImage/action"
        in importer["permissions"][0]["actions"]
    )
    assert not policy_check.repository_writes(importer)
    assert all("importImage" not in action for action in importer["permissions"][0]["dataActions"])
    assert "repositories/importImage" not in json.dumps(POLICY)


def test_legacy_role_actions_and_owner_actions_do_not_grant_abac_repository_writes():
    for actions in [
        ["*"],
        ["Microsoft.ContainerRegistry/registries/pull/read"],
        ["Microsoft.ContainerRegistry/registries/push/write"],
        ["Microsoft.ContainerRegistry/registries/artifacts/delete"],
    ]:
        assert (
            policy_check.repository_writes(
                {"permissions": [{"actions": actions, "dataActions": []}]}
            )
            is False
        )


def test_actual_assignment_validation_rejects_broad_or_custom_canonical_writers():
    policy_check.verify_assignments(assignments(), definitions(), POLICY)
    unsafe = assignments()
    unsafe[1]["condition"] = None
    with pytest.raises(
        policy_check.RegistryPolicyError, match="canonical_recipe_writer_not_isolated"
    ):
        policy_check.verify_assignments(unsafe, definitions(), POLICY)
    custom = {"name": "custom", "permissions": [{"dataActions": ["Microsoft.ContainerRegistry/*"]}]}
    unsafe = assignments() + [{"roleDefinitionId": "/roles/custom"}]
    with pytest.raises(
        policy_check.RegistryPolicyError, match="canonical_recipe_writer_not_isolated"
    ):
        policy_check.verify_assignments(unsafe, definitions() + [custom], POLICY)
