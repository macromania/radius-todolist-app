import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plane_demo.management.providers import local
from plane_demo.management.providers import local_artifacts as recipe_assets
from plane_demo.management.providers.commands import Commands
from plane_demo.management.providers.identity import DemoConfig
from plane_demo.management.providers.local_config import SLOTS, LocalConfig
from plane_demo.management.provisioning import Cluster, ProvisioningError

ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 40
UID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def prepared_provider(tmp_path, monkeypatch):
    identity = DemoConfig("local", "demo", "one", revision=REVISION)
    directory = tmp_path / "prepared"
    recipe_assets.package(ROOT, directory, REVISION)
    dependencies = [
        {"reference": "kindest/node:v1.35.0@sha256:" + "b" * 64, "id": "sha256:" + "c" * 64}
    ]
    (directory / "images.json").write_text(json.dumps(dependencies))
    (directory / "radius.tgz").write_bytes(b"synthetic prepared chart")
    inputs = {
        "revision": REVISION,
        "stem": identity.stem,
        "dependencies": dependencies,
        "images": {
            role: {
                "reference": f"localhost/{identity.stem}-{role}:{REVISION}",
                "id": "sha256:" + hashlib.sha256(role.encode()).hexdigest(),
            }
            for role in ("api", "provisioner", "operator")
        },
    }
    plan = recipe_assets.setup_plan(
        directory, inputs, identity.stem, identity.stem, identity.stem + "-access", "172.18.0.2"
    )
    raw = {
        "version": 1,
        "provider": "local",
        "projectName": identity.project,
        "allocations": {
            slot: {
                "slot": slot,
                "clusterName": identity.slot_name(slot),
                "context": identity.slot_name(slot),
                "gatewayPort": 35490 + index,
                "apiPort": 35495 + index,
            }
            for index, slot in enumerate(SLOTS)
        },
        "recipes": plan["recipes"],
        "images": {
            role: {
                "reference": inputs["images"][role]["reference"],
                "imageId": inputs["images"][role]["id"],
            }
            for role in ("api", "provisioner")
        },
        "managementCluster": {
            "clusterId": f"kind://{identity.stem}-management",
            "uid": UID,
            "nodeAddress": "172.18.0.2",
            "serviceAddress": "10.96.0.1",
            "caSHA256": "d" * 64,
        },
    }
    config = LocalConfig.from_dict(raw, identity=identity)
    commands = MagicMock(spec=Commands)
    commands.guard = MagicMock()
    commands.radius_environment.return_value = {}
    commands.run.return_value = ""
    provider = local.LocalProvider(
        config,
        ROOT,
        MagicMock(environment="local"),
        commands,
        workspace=tmp_path / "operation",
        prepared_assets=directory,
    )
    modules = {
        item["metadata"]["name"]: item for item in plan["objects"] if item["kind"] == "ConfigMap"
    }
    monkeypatch.setattr(provider, "kube_get", lambda slot, namespace, kind, name: modules.get(name))
    commands.run.return_value = json.dumps(
        {
            **plan["environment"],
            "id": f"{provider.radius_scope}/providers/Applications.Core/environments/management",
        }
    )
    provider.verify_recipes()
    commands.run.reset_mock()
    return provider, plan, inputs, modules


def test_prepared_recipes_verify_actual_owner_and_selected_images(prepared_provider):
    provider, _, _, modules = prepared_provider
    assert provider._verified
    module = modules[provider.config.recipes["cluster"]["moduleServer"]]
    module["metadata"]["labels"]["plane-demo/resource-prefix"] = "foreign"
    with pytest.raises(ProvisioningError, match="local_recipe_digest_mismatch"):
        provider.verify_recipes()


def test_real_verification_consumes_the_public_nondefault_asset_directory(prepared_provider):
    provider, _, _, _ = prepared_provider
    assert provider.prepared_assets != local.PREPARED_ASSETS
    provider.verify_recipes()
    archive = provider.prepared_assets / "modules/cluster/archive.tar.gz"
    archive.write_bytes(b"changed supplied directory")
    with pytest.raises(ProvisioningError, match="local_prepared_assets_missing"):
        provider.verify_recipes()
    assert provider._verified is False


def test_worker_discovers_runtime_images_from_consumed_radius_parameters(prepared_provider):
    provider, plan, inputs, modules = prepared_provider
    assert provider._prepared_inputs == inputs
    for module in modules.values():
        value = json.loads(module["data"]["module.json"])
        assert "images" not in value and "dependencies" not in value
    provider.verify_recipes()
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert len(calls) == 1
    assert calls[0][0] == "rad"
    assert "Applications.Core/environments" in calls[0] and "management" in calls[0]
    params = plan["environment"]["properties"]["recipes"]["Demo.Platform/clusters"]["default"][
        "parameters"
    ]
    assert set(params["runtime_images"]) == {"api", "provisioner", "operator"}


@pytest.mark.parametrize("missing", ["runtime_images", "dependency_images"])
def test_missing_live_image_parameters_never_fall_back_to_module_inventory(
    prepared_provider,
    missing,
):
    provider, plan, inputs, modules = prepared_provider
    parameters = plan["environment"]["properties"]["recipes"]["Demo.Platform/clusters"]["default"][
        "parameters"
    ]
    del parameters[missing]
    provider.commands.run.return_value = json.dumps(
        {
            **plan["environment"],
            "id": f"{provider.radius_scope}/providers/Applications.Core/environments/management",
        }
    )
    for module in modules.values():
        value = json.loads(module["data"]["module.json"])
        value.update(images=inputs["images"], dependencies=inputs["dependencies"])
        module["data"]["module.json"] = json.dumps(value)
    with pytest.raises(ProvisioningError, match="local_prepared_bindings_mismatch"):
        provider.verify_recipes()
    assert provider._verified is False


@pytest.mark.parametrize("slot,index", list(zip(SLOTS[1:], range(1, 5), strict=True)))
def test_cluster_environment_uses_selected_bindings_and_complete_prepared_images(
    prepared_provider,
    monkeypatch,
    slot,
    index,
):
    provider, _, inputs, _ = prepared_provider
    monkeypatch.setattr(provider, "rad", MagicMock(return_value=""))
    provider.register_environment(slot, cluster=True)
    body = json.loads((provider.state / f"{slot}-cluster-environment.json").read_text())
    params = body["properties"]["recipes"]["Demo.Platform/clusters"]["default"]["parameters"]
    assert params["resource_prefix"] == params["radius_group"] == provider.config.resource_prefix
    assert params["access_namespace"] == provider.config.access_namespace
    assert params["runtime_images"] == {
        role: {"reference": image["reference"], "image_id": image["id"]}
        for role, image in inputs["images"].items()
    }
    assert params["dependency_images"] == [
        {"reference": image["reference"], "image_id": image["id"]}
        for image in inputs["dependencies"]
    ]
    assert (
        body["properties"]["compute"]["namespace"] == f"{provider.config.resource_prefix}-p-{index}"
    )


@pytest.mark.parametrize("slot", SLOTS)
@pytest.mark.parametrize("project,deployment", [("demo", "one"), ("abcdefghijklmnop", "ab")])
def test_environment_producer_composes_exact_radius_application_namespace(
    prepared_provider,
    project,
    deployment,
    slot,
):
    provider, _, inputs, _ = prepared_provider
    identity = DemoConfig("local", project, deployment)
    value = recipe_assets.environment(
        identity.stem,
        identity.stem,
        identity.stem + "-access",
        slot,
        provider.config.recipes,
        inputs,
        "172.18.0.2",
    )
    environment_namespace = value["properties"]["compute"]["namespace"]
    application = "management" if slot == "management" else slot.rsplit("-", 1)[1]
    assert environment_namespace == f"{identity.stem}-{slot}"
    assert f"{environment_namespace}-{application}" == identity.namespace(slot)
    assert len(f"{environment_namespace}-{application}") <= 63
    assert environment_namespace != identity.namespace(slot)


@pytest.mark.parametrize("slot,index", list(zip(SLOTS[1:], range(1, 5), strict=True)))
def test_long_selected_prefix_uses_short_provisioning_namespace(
    prepared_provider,
    slot,
    index,
):
    provider, _, inputs, _ = prepared_provider
    identity = DemoConfig("local", "abcdefghijklmnop", "ab")
    assert len(identity.stem) == 25
    assert recipe_assets.environment_namespace(identity.stem, "management", cluster=True) == (
        identity.stem + "-p-0"
    )
    value = recipe_assets.environment(
        identity.stem,
        identity.stem,
        identity.stem + "-access",
        slot,
        provider.config.recipes,
        inputs,
        None,
        cluster=True,
    )
    prefix = value["properties"]["compute"]["namespace"]
    assert prefix == f"{identity.stem}-p-{index}"
    final_namespace = f"{prefix}-cluster-{slot}"
    assert len(final_namespace) <= 63
    if slot.endswith("-control"):
        assert len(f"{identity.stem}-p-{slot}-cluster-{slot}") > 63


def test_live_binding_reader_rejects_app_namespace_as_environment_prefix(prepared_provider):
    provider, plan, inputs, _ = prepared_provider
    resource = {
        **plan["environment"],
        "id": f"{provider.radius_scope}/providers/Applications.Core/environments/management",
    }
    assert (
        recipe_assets.binding_inputs(
            resource,
            provider.config.resource_prefix,
            provider.config.radius_group,
        )
        == inputs
    )
    resource["properties"]["compute"]["namespace"] = provider.config.namespace("management")
    with pytest.raises(ValueError, match="management_environment_mismatch"):
        recipe_assets.binding_inputs(
            resource,
            provider.config.resource_prefix,
            provider.config.radius_group,
        )


def test_radius_environment_owner_blocks_replay_without_host_intents(
    prepared_provider, monkeypatch
):
    provider, _, _, _ = prepared_provider
    environments = []
    monkeypatch.setattr(provider, "rad", lambda *args, **kwargs: json.dumps(environments))
    monkeypatch.setattr(provider, "resource_exists", lambda *args: False)
    monkeypatch.setattr(provider, "kube_get", lambda *args: None)

    def register(slot, *, cluster):
        environments.append({"name": "provision-" + slot})
        return "provision-" + slot

    monkeypatch.setattr(provider, "register_environment", register)
    submission = MagicMock(side_effect=ProvisioningError("command_timeout"))
    monkeypatch.setattr(provider, "deploy", submission)
    stale = provider.state / "shared-control-cluster-intent.json"
    stale.write_text("old host files are not an authority")
    with pytest.raises(ProvisioningError, match="command_timeout"):
        provider.ensure_child_cluster("shared-control")
    with pytest.raises(ProvisioningError, match="local_cluster_creation_incomplete"):
        provider.ensure_child_cluster("shared-control")
    submission.assert_called_once()
    assert stale.read_text() == "old host files are not an authority"


def test_child_bootstrap_uses_packaged_chart_and_operator_without_downloads(
    prepared_provider,
    monkeypatch,
):
    provider, _, inputs, _ = prepared_provider
    objects = {}
    for name in ("dynamic-rp", "applications-rp"):
        objects[("deployment", name)] = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": name, "image": f"ghcr.io/radius-project/{name}:0.60"}
                        ],
                        "volumes": [
                            {"name": "config", "configMap": {"name": name + "-config"}},
                            {"name": "terraform", "emptyDir": {}},
                        ],
                    }
                }
            }
        }
        objects[("configmap", name + "-config")] = {
            "data": {
                "radius-self-host.yaml": "terraform:\n  path: /terraform\n  logLevel: TRACE\n"
            },
        }
    monkeypatch.setattr(
        provider, "kube_get", lambda slot, namespace, kind, name: objects.get((kind, name))
    )
    monkeypatch.setattr(
        provider,
        "apply",
        lambda slot, value, **kwargs: objects.update({("namespace", "radius-system"): value}),
    )
    install = MagicMock()
    monkeypatch.setattr(provider, "rad", install)
    monkeypatch.setattr(provider, "publish_modules", MagicMock())
    monkeypatch.setattr(provider, "register", MagicMock())
    cluster = Cluster(
        "shared-control",
        provider.expected_cluster_id("shared-control"),
        *provider.paths("shared-control"),
    )
    provider.bootstrap_child(cluster)
    argv = install.call_args.args
    assert "--chart" in argv and str(provider.prepared_assets / "radius.tgz") in argv
    assert str(local.PREPARED_ASSETS / "radius.tgz") not in argv
    assert "global.terraform.enabled=false" in argv
    patches = [
        json.loads(call.args[0][-1])
        for call in provider.commands.run.call_args_list
        if "patch" in call.args[0] and "deployment" in call.args[0]
    ]
    assert len(patches) == 2
    for patch in patches:
        pod = patch["spec"]["template"]["spec"]
        assert pod["initContainers"][0]["image"] == inputs["images"]["operator"]["reference"]
        assert pod["initContainers"][0]["command"] == ["python3", local.TERRAFORM_INIT]
        assert pod["initContainers"][0]["imagePullPolicy"] == "Never"
        assert "image" not in pod["containers"][0]
        assert {"name": "TF_CLI_CONFIG_FILE", "value": "/terraform/terraform.tfrc"} in (
            pod["containers"][0]["env"]
        )
        assert "hostPath" not in json.dumps(patch) and "docker.sock" not in json.dumps(patch)
        assert pod["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    with pytest.raises(ProvisioningError, match="local_child_bootstrap_incomplete"):
        provider.bootstrap_child(cluster)
    assert not (provider.state / "shared-control-radius-intent.json").exists()


def test_published_modules_use_prepared_operator_files_not_host_configmaps(
    prepared_provider,
    monkeypatch,
):
    provider, _, inputs, _ = prepared_provider
    monkeypatch.setattr(provider, "kube_get", lambda *args: None)
    publications = []
    monkeypatch.setattr(
        provider, "apply", lambda slot, values, **kwargs: publications.extend(values)
    )
    monkeypatch.setattr(provider, "kubectl", MagicMock())
    provider.publish_modules("shared-data")
    deployments = [item for item in publications if item["kind"] == "Deployment"]
    assert len(deployments) == 2
    for deployment in deployments:
        pod = deployment["spec"]["template"]["spec"]
        assert "volumes" not in pod
        assert pod["automountServiceAccountToken"] is False
        assert pod["containers"][0]["image"] == inputs["images"]["operator"]["reference"]
        assert pod["containers"][0]["imagePullPolicy"] == "Never"
