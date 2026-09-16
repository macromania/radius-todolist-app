import copy
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from plane_demo.management.providers import redis_nic_tags as metadata
from plane_demo.management.provisioning import ProvisioningError

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
CLIENT = "33333333-3333-3333-3333-333333333333"
RADIUS = "/planes/radius/local/resourceGroups/radplanes/providers/"


@pytest.fixture
def target_data():
    return {
        "slot": "shared-data",
        "subscription_id": SUBSCRIPTION,
        "tenant_id": TENANT,
        "client_id": CLIENT,
        "resource_group": "rg-radplanes-shared-data-app",
        "subnet_id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-radplanes-platform"
        "/providers/Microsoft.Network/virtualNetworks/vnet/subnets/endpoints",
        "location": "centralus",
        "resource_id": RADIUS + "Applications.Datastores/redisCaches/redis",
        "environment_id": RADIUS + "Applications.Core/environments/shared-data",
        "application_id": RADIUS + "Applications.Core/applications/data",
        "tags": {**metadata.BASE_TAGS, "costCenter": "fixture"},
    }


class Azure:
    def __init__(self, target):
        self.target = target
        self.cache_id = (
            target.group_id + "/providers/Microsoft.Cache/redisEnterprise/amr-abcdefghijklm"
        )
        self.pe_id = (
            target.group_id + "/providers/Microsoft.Network/privateEndpoints/pe-amr-abcdefghijklm"
        )
        self.nic_id = (
            target.group_id + "/providers/Microsoft.Network/networkInterfaces/nic-amr-abcdefghijklm"
        )

        def resource(identifier, kind, properties, tags):
            return {
                "id": identifier,
                "name": identifier.rsplit("/", 1)[1],
                "type": kind,
                "location": "centralus",
                "properties": {"provisioningState": "Succeeded", **properties},
                "tags": dict(tags),
            }

        self.group = {
            "id": target.group_id,
            "name": target.resource_group,
            "type": "Microsoft.Resources/resourceGroups",
            "tags": dict(target.tags),
        }
        self.cache = resource(
            self.cache_id,
            "Microsoft.Cache/redisEnterprise",
            {
                "publicNetworkAccess": "Disabled",
            },
            {**target.required_tags, "cache-only": "leave-on-cache"},
        )
        self.cache["location"] = "Central US"
        self.pe = resource(
            self.pe_id,
            "Microsoft.Network/privateEndpoints",
            {
                "subnet": {"id": target.subnet_id},
                "customNetworkInterfaceName": "nic-amr-abcdefghijklm",
                "networkInterfaces": [{"id": self.nic_id}],
                "privateLinkServiceConnections": [
                    {
                        "properties": {
                            "privateLinkServiceId": self.cache_id,
                            "groupIds": ["redisEnterprise"],
                            "privateLinkServiceConnectionState": {"status": "Approved"},
                        }
                    }
                ],
            },
            {**target.required_tags, "pe-only": "leave-on-pe"},
        )
        self.nic = resource(
            self.nic_id,
            "Microsoft.Network/networkInterfaces",
            {
                "privateEndpoint": {"id": self.pe_id},
                "enableIPForwarding": False,
                "ipConfigurations": [
                    {"name": "private", "properties": {"privateIPAddress": "10.64.34.4"}}
                ],
            },
            {"keep": "untouched", "Project": "radplanes"},
        )
        self.listing = {"value": [{"id": self.cache_id, "tags": dict(target.required_tags)}]}
        self.requests = []
        self.patches = []
        self.ignore_patch = False
        self.change_properties = False
        self.change_etag = False

    def handle(self, request):
        self.requests.append(request)
        assert request.url.host == "management.azure.com"
        assert request.headers["authorization"] == "Bearer fixture-token"
        assert "listKeys" not in request.url.path
        path = request.url.path
        if request.method == "PATCH":
            assert path == self.nic_id
            assert request.url.params["api-version"] == "2024-07-01"
            body = json.loads(request.content)
            self.patches.append(body)
            assert set(body) == {"tags"}
            if not self.ignore_patch:
                self.nic["tags"] = dict(body["tags"])
            if self.change_properties:
                self.nic["properties"]["enableIPForwarding"] = True
            if self.change_etag:
                self.nic["properties"]["ipConfigurations"][0]["etag"] = "new-revision"
            return httpx.Response(200, json={"properties": {"tags": self.nic["tags"]}})
        assert request.method == "GET"
        documents = {
            self.target.group_id: self.group,
            self.target.group_id + "/providers/Microsoft.Cache/redisEnterprise": self.listing,
            self.cache_id: self.cache,
            self.pe_id: self.pe,
            self.nic_id: self.nic,
        }
        assert path in documents
        return httpx.Response(200, json=documents[path])

    def run(self):
        with httpx.Client(transport=httpx.MockTransport(self.handle)) as client:
            return metadata.tag_nic(
                self.target,
                metadata.Arm(client, "fixture-token"),
                pause=lambda _: None,
            )


@pytest.fixture
def azure(target_data):
    return Azure(metadata.Target.parse(target_data))


@pytest.mark.parametrize("location", ["centralus", "Central US"])
def test_canonical_and_verified_display_region_names_allow_tagging(azure, location):
    azure.cache["location"] = location
    assert azure.run()["cacheId"] == azure.cache_id
    assert len(azure.patches) == 1


@pytest.mark.parametrize(("display", "allowed"), [("North Europe", True), ("Central US", False)])
def test_selected_project_scope_and_region_are_checked_on_the_real_tagging_path(
    target_data, display, allowed
):
    prefix = "sample-demo-azure"
    for key in ("resource_group", "subnet_id", "resource_id", "environment_id", "application_id"):
        target_data[key] = target_data[key].replace("radplanes", prefix)
    target_data.update(project_name="sample", resource_prefix=prefix, location="northeurope")
    target_data["resource_group"] = target_data["resource_group"].removesuffix("-app")
    target_data["tags"]["project"] = "sample"
    azure = Azure(metadata.Target.parse(target_data))
    for resource in (azure.cache, azure.pe, azure.nic):
        resource["location"] = "northeurope"
    azure.cache["location"] = display
    azure.nic["tags"]["Project"] = "sample"
    if not allowed:
        with pytest.raises(ProvisioningError, match="redis_nic_ownership_mismatch"):
            azure.run()
        assert not azure.patches
        return
    assert azure.run()["nicId"] == azure.nic_id
    assert len(azure.patches) == 1
    assert azure.nic["tags"]["project"] == "sample"
    assert all(prefix in request.url.path for request in azure.requests)


@pytest.mark.parametrize("location", ["westus2", "West US 2", None, {}, 42])
def test_other_or_malformed_regions_fail_before_tagging(azure, location):
    azure.cache["location"] = location
    with pytest.raises(ProvisioningError, match="redis_nic_ownership_mismatch"):
        azure.run()
    assert not azure.patches


def test_verified_merge_preserves_other_tags_and_network_properties_and_is_idempotent(azure):
    before = copy.deepcopy(azure.nic["properties"])
    result = azure.run()
    assert result == {
        "cacheId": azure.cache_id,
        "privateEndpointId": azure.pe_id,
        "nicId": azure.nic_id,
    }
    assert len(azure.patches) == 1
    assert azure.nic["tags"]["keep"] == "untouched"
    assert "cache-only" not in azure.nic["tags"] and "pe-only" not in azure.nic["tags"]
    assert azure.nic["properties"] == before
    assert azure.requests[-1].method == "GET" and azure.requests[-1].url.path == azure.nic_id
    assert azure.run() == result
    assert len(azure.patches) == 1


def test_concurrent_tag_change_before_patch_is_not_overwritten(azure):
    original = azure.handle
    reads = 0

    def concurrent(request):
        nonlocal reads
        if request.method == "GET" and request.url.path == azure.nic_id:
            reads += 1
            if reads == 2:
                azure.nic["tags"]["other-writer"] = "retained"
        return original(request)

    azure.handle = concurrent
    with pytest.raises(ProvisioningError, match="redis_nic_tag_conflict"):
        azure.run()
    assert not azure.patches
    assert azure.nic["tags"]["other-writer"] == "retained"


@pytest.mark.parametrize(
    "change",
    [
        lambda a: a.group.update(id="/subscriptions/foreign/resourceGroups/foreign"),
        lambda a: a.group["tags"].update(project="foreign"),
        lambda a: a.cache.update(id=a.cache_id + "/databases/default"),
        lambda a: a.cache.update(type="Microsoft.Network/networkInterfaces"),
        lambda a: a.cache.update(name="another-cache"),
        lambda a: a.cache.update(location="westus2"),
        lambda a: a.cache["properties"].update(publicNetworkAccess="Enabled"),
        lambda a: a.cache["properties"].update(provisioningState="Creating"),
        lambda a: a.cache["tags"].update(
            {"radapp.io-application": RADIUS + "Applications.Core/applications/foreign"}
        ),
        lambda a: a.pe["tags"].update(SecurityControl="Wrong"),
        lambda a: a.pe["properties"]["subnet"].update(id=a.target.subnet_id + "-foreign"),
        lambda a: a.pe["properties"].update(customNetworkInterfaceName="another-nic"),
        lambda a: a.pe["properties"]["networkInterfaces"][0].update(id=a.nic_id + "-foreign"),
        lambda a: a.pe["properties"]["networkInterfaces"].append({"id": a.nic_id}),
        lambda a: a.pe["properties"]["privateLinkServiceConnections"][0]["properties"].update(
            privateLinkServiceId=a.cache_id + "-foreign"
        ),
        lambda a: a.pe["properties"]["privateLinkServiceConnections"][0]["properties"].update(
            groupIds=["redisEnterprise", "another-subresource"]
        ),
        lambda a: a.pe["properties"]["privateLinkServiceConnections"][0]["properties"][
            "privateLinkServiceConnectionState"
        ].update(status="Pending"),
        lambda a: a.pe["properties"].update(manualPrivateLinkServiceConnections={}),
        lambda a: a.pe["properties"].update(
            manualPrivateLinkServiceConnections=[{"id": "foreign"}]
        ),
        lambda a: a.nic["properties"]["privateEndpoint"].update(id=a.pe_id + "-foreign"),
        lambda a: a.nic.update(id=a.nic_id.replace(a.target.resource_group, "rg-foreign")),
        lambda a: a.nic["properties"].update(provisioningState="Updating"),
        lambda a: a.nic.update(tags=[]),
        lambda a: a.nic.update(tags={"keep": None}),
        lambda a: a.listing["value"][0].update(id="https://foreign.test/nic"),
        lambda a: a.listing["value"][0].update(id=a.cache_id + "?api-version=wrong"),
        lambda a: a.listing["value"][0].update(id=a.cache_id + "/../foreign"),
        lambda a: a.listing["value"].append(copy.deepcopy(a.listing["value"][0])),
        lambda a: a.listing.update(value=[]),
        lambda a: a.listing.update(nextLink="https://foreign.test/more"),
    ],
)
def test_all_parent_ownership_relationships_are_required_before_patch(azure, change):
    change(azure)
    with pytest.raises(ProvisioningError):
        azure.run()
    assert azure.patches == []


def test_existing_conflicting_nic_tags_are_not_overwritten(azure):
    azure.nic["tags"]["Project"] = "foreign"
    with pytest.raises(ProvisioningError, match="redis_nic_tag_conflict"):
        azure.run()
    assert azure.patches == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("subscription_id", "bad-guid"),
        ("tenant_id", "bad-guid"),
        ("client_id", "bad-guid"),
        ("slot", "../shared-data"),
        ("resource_group", "rg-radplanes-other-data-app"),
        ("subnet_id", "/subscriptions/foreign/subnet"),
        ("resource_id", "https://foreign.test/resource"),
        ("environment_id", RADIUS + "Applications.Core/environments/../../foreign"),
        ("application_id", RADIUS + "Applications.Core/applications/data?query=bad"),
        ("location", "westus2"),
    ],
)
def test_malformed_target_ids_are_rejected(target_data, field, value):
    target_data[field] = value
    with pytest.raises(ProvisioningError, match="redis_nic_input_invalid"):
        metadata.Target.parse(target_data)


@pytest.mark.parametrize("response", [301, 401, 403, 404, 429, 500])
def test_arm_errors_do_not_fall_back_to_unverified_data_or_patch(azure, response):
    seen = []

    def transport(request):
        seen.append(request)
        return httpx.Response(
            response,
            json={"secret": "never-log-this"},
            headers={"Location": "https://foreign.test/"},
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ProvisioningError, match="redis_nic_arm_failed"):
            metadata.tag_nic(azure.target, metadata.Arm(client, "fixture-token"))
    assert len(seen) == 1 and seen[0].method == "GET"


@pytest.mark.parametrize("failure", ["network", "deadline", "late-response", "json"])
def test_timeouts_and_invalid_responses_fail_before_patch(azure, failure):
    now = [0.0]
    seen = []

    def transport(request):
        seen.append(request)
        if failure == "network":
            raise httpx.ReadTimeout("private diagnostic", request=request)
        if failure == "late-response":
            now[0] = 61
        if failure == "json":
            return httpx.Response(200, text="not-json")
        return httpx.Response(200, json=azure.group)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        arm = metadata.Arm(client, "fixture-token", clock=lambda: now[0])
        if failure == "deadline":
            now[0] = 61
        with pytest.raises(ProvisioningError):
            metadata.tag_nic(azure.target, arm)
    assert not any(request.method == "PATCH" for request in seen)


@pytest.mark.parametrize("changed", ["tags", "properties"])
def test_patch_requires_live_readback_and_preserved_properties(azure, changed):
    azure.ignore_patch = changed == "tags"
    azure.change_properties = changed == "properties"
    with pytest.raises(ProvisioningError, match="redis_nic_verification_failed"):
        azure.run()
    assert len(azure.patches) == 1


def test_readonly_etag_changes_are_not_mistaken_for_network_reconfiguration(azure):
    azure.nic["properties"]["ipConfigurations"][0]["etag"] = "old-revision"
    azure.change_etag = True
    assert azure.run()["nicId"] == azure.nic_id
    assert len(azure.patches) == 1


def main_environment(target_data, monkeypatch):
    values = {
        "REDIS_NIC_TAG_TARGET": json.dumps(target_data),
        "AZURE_CLIENT_ID": CLIENT,
        "AZURE_TENANT_ID": TENANT,
        "AZURE_FEDERATED_TOKEN_FILE": metadata.TOKEN_FILE,
        "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.com/",
        "POD_NAMESPACE": "radius-system",
        "POD_SERVICE_ACCOUNT": "applications-rp",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    original = Path.is_file
    monkeypatch.setattr(
        Path, "is_file", lambda path: True if str(path) == metadata.TOKEN_FILE else original(path)
    )
    messages = []
    original_write = Path.write_text

    def write(path, text, *args, **kwargs):
        if str(path) == "/dev/termination-log":
            messages.append(json.loads(text))
            return len(text)
        return original_write(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    return messages


@pytest.mark.parametrize(
    "field",
    [
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_FEDERATED_TOKEN_FILE",
        "AZURE_AUTHORITY_HOST",
        "POD_NAMESPACE",
        "POD_SERVICE_ACCOUNT",
    ],
)
def test_main_refuses_wrong_workload_identity_before_any_arm_action(
    target_data,
    monkeypatch,
    field,
    caplog,
):
    messages = main_environment(target_data, monkeypatch)
    monkeypatch.setenv(field, "wrong-value")
    token = MagicMock()
    monkeypatch.setattr(metadata, "workload_token", token)
    with caplog.at_level(logging.ERROR):
        assert metadata.main() == 1
    token.assert_not_called()
    assert messages == [{"error_code": "redis_nic_identity_mismatch"}]
    assert "wrong-value" not in caplog.text


def test_main_invokes_real_tagging_and_returns_only_nonsecret_identifiers(
    azure,
    target_data,
    monkeypatch,
    capsys,
):
    messages = main_environment(target_data, monkeypatch)
    token = MagicMock(return_value="fixture-token")
    monkeypatch.setattr(metadata, "workload_token", token)
    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(azure.handle), **kwargs),
    )
    assert metadata.main() == 0
    token.assert_called_once()
    assert len(azure.patches) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == messages[0]
    assert set(messages[0]) == {"cacheId", "privateEndpointId", "nicId"}
    assert "fixture-token" not in output.out + output.err
