import copy
import json
import subprocess
import sys
from pathlib import Path
from uuid import NAMESPACE_DNS, uuid5

import pytest

from plane_demo.management.providers.identity import IDENTITY_PURPOSES, SLOTS, DemoConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.operations.azure import plane_policy as policy  # noqa: E402


@pytest.fixture
def world():
    config = DemoConfig(
        "azure", "sample", "demo", "11111111-1111-1111-1111-111111111111", "centralus"
    )
    document = {
        "foundation": {
            "resourceGroupLayout": "plane-v2",
            "projectName": config.project,
            "deploymentName": config.deployment,
            "environment": "azure",
            "subscriptionId": config.subscription,
            "location": config.location,
            "resourcePrefix": config.stem,
            "roleDefinitionIds": {
                key: policy.role_id(config.subscription, config.stem, value[0])
                for key, value in policy.ROLE_NAMES.items()
            },
        },
        "allocations": [
            {
                "slot": slot,
                "clusterResourceGroup": config.plane_group(slot),
                "appResourceGroup": config.plane_group(slot),
                "identities": {
                    key: {
                        "id": config.managed_identity_id(slot, purpose),
                        "principalId": str(uuid5(NAMESPACE_DNS, slot + purpose)),
                    }
                    for key, purpose in IDENTITY_PURPOSES.items()
                },
            }
            for slot in SLOTS
        ],
    }
    return config, document


class Azure:
    def __init__(self, config, document):
        self.config = config
        self.principals, grants, self.contracts = policy.expected_grants(config, document)
        self.rows = [
            {
                "id": scope
                + "/providers/Microsoft.Authorization/roleAssignments/"
                + str(uuid5(NAMESPACE_DNS, str(grant))),
                "properties": dict(
                    zip(("principalId", "roleDefinitionId", "scope"), grant, strict=True)
                ),
            }
            for grant in sorted(grants)
            for _, _, scope in [grant]
        ]
        self.groups, self.inherited, self.calls = [], [], []
        self.fail_graph = self.extra_page = self.truncated = False
        self.change_definition = False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert argv[:4] == ["az", "rest", "--method", "get"]
        assert argv[argv.index("--subscription") + 1] == self.config.subscription
        url = argv[argv.index("--url") + 1]
        if "graph.microsoft.com" in url:
            if self.fail_graph:
                return subprocess.CompletedProcess(argv, 1, "")
            value = {"value": self.groups}
        elif "/roleAssignments?" in url:
            if "atScope()" in url:
                value = {"value": self.inherited}
            elif "page=2" in url:
                value = {"value": self.rows[1:]}
            else:
                value = {"value": self.rows[:1] if self.extra_page else self.rows}
                if self.extra_page:
                    value["nextLink"] = url + "&page=2"
                if self.truncated:
                    value.pop("value")
        else:
            identifier = url.removeprefix("https://management.azure.com").split("?")[0]
            definition = (
                {"type": "CustomRole", **copy.deepcopy(self.contracts[identifier])}
                if identifier in self.contracts
                else {"type": "BuiltInRole"}
            )
            if self.change_definition and identifier in self.contracts:
                definition["permissions"][0]["actions"].append("*")
            value = {"id": identifier, "properties": definition}
        return subprocess.CompletedProcess(argv, 0, json.dumps(value))


def test_real_verifier_reads_all_principals_scopes_and_pages(world):
    config, document = world
    azure = Azure(config, document)
    azure.extra_page = True
    result = policy.Verifier(config, runner=azure).verify(document)
    assert result["status"] == "conformant" and result["radiusIdentities"] == 5
    assert result["missingAssignments"] == 0
    assert sum("graph.microsoft.com" in " ".join(call) for call in azure.calls) == 5
    assert any("atScope()" in " ".join(call) for call in azure.calls)
    assert any("page=2" in " ".join(call) for call in azure.calls)


@pytest.mark.parametrize("source", ["direct", "inherited", "group"])
@pytest.mark.parametrize(
    "role",
    ["b24988ac-6180-42a0-ab88-20f7382dd24c", "1c849693-6b3a-4e4c-9e83-e68910ca10f7"],
)
def test_additional_grants_are_not_hidden_by_expected_roles(world, source, role):
    config, document = world
    azure = Azure(config, document)
    extra = copy.deepcopy(azure.rows[0])
    extra["id"] += "-extra"
    extra["properties"]["roleDefinitionId"] = (
        f"/subscriptions/{config.subscription}/providers/Microsoft.Authorization/roleDefinitions/{role}"
    )
    if source == "group":
        group = str(uuid5(NAMESPACE_DNS, "group"))
        azure.groups = [{"id": group, "@odata.type": "#microsoft.graph.group"}]
        extra["properties"]["principalId"] = group
    (azure.inherited if source == "inherited" else azure.rows).append(extra)
    with pytest.raises(policy.PolicyError, match="Unexpected Radius grant"):
        policy.Verifier(config, runner=azure).verify(document)


@pytest.mark.parametrize("mode", ["condition", "foreign-scope", "definition", "graph", "page"])
def test_uncertain_or_changed_authority_blocks_verification(world, mode):
    config, document = world
    azure = Azure(config, document)
    if mode == "condition":
        azure.rows[0]["properties"]["condition"] = "unknown"
    elif mode == "foreign-scope":
        azure.rows[0]["properties"]["scope"] += "-foreign"
    elif mode == "definition":
        azure.change_definition = True
    elif mode == "graph":
        azure.fail_graph = True
    else:
        azure.truncated = True
    with pytest.raises(policy.PolicyError):
        policy.Verifier(config, runner=azure).verify(document)


def test_only_preflight_allows_missing_expected_assignments(world):
    config, document = world
    azure = Azure(config, document)
    azure.rows.pop()
    verifier = policy.Verifier(config, runner=azure)
    assert verifier.verify(document, allow_missing=True)["missingAssignments"] == 1
    with pytest.raises(policy.PolicyError, match="missing"):
        verifier.verify(document)


def test_conflicting_duplicate_assignment_is_not_hidden_by_pagination(world):
    config, document = world
    azure = Azure(config, document)
    changed = copy.deepcopy(azure.rows[0])
    changed["properties"]["condition"] = "changed"
    azure.inherited = [changed]
    with pytest.raises(policy.PolicyError, match="changed during verification"):
        policy.Verifier(config, runner=azure).verify(document)


def test_membership_reads_need_no_advanced_eventual_query(world):
    config, document = world
    azure = Azure(config, document)
    policy.Verifier(config, runner=azure).verify(document)
    urls = [
        call[call.index("--url") + 1]
        for call in azure.calls
        if "graph.microsoft.com" in " ".join(call)
    ]
    assert all(url.endswith("/transitiveMemberOf") for url in urls)
    azure.groups = [
        {"id": str(uuid5(NAMESPACE_DNS, "admin")), "@odata.type": "#microsoft.graph.directoryRole"}
    ]
    with pytest.raises(policy.PolicyError, match="directory membership"):
        policy.Verifier(config, runner=azure).verify(document)


@pytest.mark.parametrize("mode", ["layout", "identity", "duplicate", "scope"])
def test_identity_and_layout_fail_before_authorization_reads(world, mode):
    config, document = world
    azure = Azure(config, document)
    if mode == "layout":
        document["foundation"].pop("resourceGroupLayout")
    elif mode == "identity":
        document["allocations"][0]["identities"]["radius"]["id"] += "-foreign"
    elif mode == "duplicate":
        document["allocations"][1] = document["allocations"][0]
    else:
        document["allocations"][0]["appResourceGroup"] += "-app"
    with pytest.raises(policy.PolicyError):
        policy.Verifier(config, runner=azure).verify(document)
    assert not azure.calls


def test_application_roles_cannot_mutate_bootstrap_or_delegate():
    for key in policy.POLICY["roles"]:
        actions = policy.application_actions(key)
        assert len(actions) == len(set(actions))
        assert not any("*" in action for action in actions)
        assert not any(
            action.startswith(
                (
                    "Microsoft.Authorization/",
                    "Microsoft.ManagedIdentity/",
                    "Microsoft.ContainerService/",
                    "Microsoft.Resources/tags/",
                )
            )
            for action in actions
        )
        assert not any(action.endswith("resourceGroups/delete") for action in actions)
    assert not any(
        "networkInterfaces" in action
        for action in policy.application_actions("postgresApplication")
    )
    assert "Microsoft.Network/networkInterfaces/write" in policy.application_actions(
        "redisApplication"
    )
    assert not any(
        "listKeys" in action for action in policy.application_actions("postgresApplication")
    )


def test_plane_roles_are_assigned_only_to_matching_slots(world):
    config, document = world
    _, grants, contracts = policy.expected_grants(config, document)
    for key in policy.POLICY["roles"]:
        identifier = document["foundation"]["roleDefinitionIds"][key].lower()
        actual = {scope for _, role, scope in grants if role == identifier}
        assert actual == {config.plane_group_id(slot).lower() for slot in policy.role_slots(key)}
        assert set(contracts[identifier]["assignableScopes"]) == {
            config.plane_group_id(slot) for slot in policy.role_slots(key)
        }


def test_cli_runs_the_verifier_and_reports_failure(world, tmp_path, monkeypatch, capsys):
    config, document = world
    azure = Azure(config, document)
    azure.fail_graph = True
    factory = policy.Verifier
    monkeypatch.setattr(policy, "Verifier", lambda config: factory(config, runner=azure))
    path = tmp_path / "foundation.json"
    path.write_text(json.dumps(document))
    monkeypatch.setattr(sys, "argv", ["plane_policy.py", "--foundation", str(path)])
    assert policy.main() == 1
    output = capsys.readouterr()
    assert not output.out and "incomplete" in output.err
