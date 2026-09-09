import copy
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BICEP = os.environ.get("RADIUS_BICEP", str(Path.home() / ".rad/bin/bicep"))
BARE_COPY_INDEX = re.compile(r"\bcopyIndex\(\s*\)")


def compile_template(relative_path):
    result = subprocess.run(
        [BICEP, "build", str(ROOT / relative_path), "--stdout"],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stderr:
        raise AssertionError(f"{relative_path}: {result.stderr}")
    return json.loads(result.stdout)


def resources(template):
    entries = template.get("resources", [])
    return list(entries.values()) if isinstance(entries, dict) else entries


def nested_resources(template):
    for resource in resources(template):
        yield resource
        nested = resource.get("properties", {}).get("template")
        if nested is not None:
            yield from nested_resources(nested)


def assert_bound_copy_indices(node, anonymous_allowed=False, path="$"):
    if isinstance(node, str):
        if BARE_COPY_INDEX.search(node) and not anonymous_allowed:
            raise AssertionError(f"Unbound copyIndex() at {path}: {node}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            assert_bound_copy_indices(value, anonymous_allowed, f"{path}[{index}]")
    elif isinstance(node, dict):
        resource_loop = isinstance(node.get("copy"), dict)
        for key, value in node.items():
            # Each nested deployment template has its own expression/loop scope.
            child_scope = False if key == "template" else anonymous_allowed or resource_loop
            assert_bound_copy_indices(value, child_scope, f"{path}.{key}")


class CompiledInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = compile_template("infra/bootstrap/azure.bicep")
        cls.platform_access = compile_template("infra/bootstrap/platform-access.bicep")
        cls.network = compile_template("infra/bootstrap/network.bicep")
        cls.cluster_recipe = compile_template("infra/radius/recipes/azure/cluster.bicep")
        cls.postgresql_recipe = compile_template("infra/radius/recipes/azure/postgresql.bicep")

    def test_bootstrap_has_no_unbound_copy_indices(self):
        assert_bound_copy_indices(self.bootstrap)

    def test_checker_rejects_the_reported_management_reference_regression(self):
        invalid = copy.deepcopy(self.bootstrap)
        management = next(
            item for item in resources(invalid) if item["name"] == "management-cluster"
        )
        management["properties"]["parameters"]["controlPlaneIdentityId"]["value"] = (
            "[variables('slots')[copyIndex()]]"
        )
        with self.assertRaisesRegex(AssertionError, "controlPlaneIdentityId"):
            assert_bound_copy_indices(invalid)

    def test_management_scopes_and_group_dependencies_are_explicit(self):
        names = {
            "coordinator-identity",
            "management-cluster",
            "management-radius-federation",
            "management-issuer-federation",
            "coordinator-federation",
        }
        modules = [item for item in resources(self.bootstrap) if item["name"] in names]
        self.assertEqual(len(modules), len(names))
        for module in modules:
            with self.subTest(module=module["name"]):
                self.assertEqual(
                    module["resourceGroup"],
                    "[format('rg-{0}-management-cluster', variables('prefix'))]",
                )
                self.assertIn("clusterGroups", module["dependsOn"])
                body = {key: value for key, value in module.items() if key != "properties"}
                self.assertNotRegex(json.dumps(body), BARE_COPY_INDEX)
        for key in ("coordinatorIdentity", "managementCluster"):
            self.assertNotRegex(json.dumps(self.bootstrap["outputs"][key]), BARE_COPY_INDEX)

    def test_child_identity_module_keeps_child_and_management_references_distinct(self):
        module = next(
            item
            for item in resources(self.bootstrap)
            if item.get("copy", {}).get("name") == "childIdentityAccess"
        )
        self.assertEqual(
            module["resourceGroup"],
            "[format('rg-{0}-{1}-cluster', variables('prefix'), "
            "parameters('childSlots')[copyIndex()])]",
        )
        management_id = module["properties"]["parameters"]["managementRadiusPrincipalId"]["value"]
        self.assertNotIn("copyIndex", management_id)
        self.assertEqual(management_id.count("variables('slots')[0]"), 2)
        child_id = module["properties"]["parameters"]["identities"]["value"]
        self.assertEqual(child_id.count("variables('slots')[add(copyIndex(), 1)]"), 2)

    def test_federation_is_serialized_at_bootstrap_and_recipe_call_sites(self):
        for template in (self.bootstrap, self.cluster_recipe):
            credentials = [
                item
                for item in nested_resources(template)
                if item["type"]
                == "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials"
            ]
            self.assertGreater(len(credentials), 0)
            for credential in credentials:
                if "copy" in credential:
                    self.assertEqual(credential["copy"]["mode"], "serial")
                    self.assertEqual(credential["copy"]["batchSize"], 1)
                else:
                    self.assertIn("'certificate-issuer'", credential["name"])

    def test_cluster_recipe_is_flat_and_retains_bootstrap_aks_security(self):
        template = self.cluster_recipe
        self.assertNotIn("Microsoft.Resources/deployments", json.dumps(template))
        self.assertFalse(
            any(
                item["type"] == "Microsoft.Authorization/roleAssignments"
                for item in resources(template)
            )
        )
        cluster_type = "Microsoft.ContainerService/managedClusters"
        child = next(item for item in resources(template) if item["type"] == cluster_type)
        management = next(
            item for item in nested_resources(self.bootstrap) if item["type"] == cluster_type
        )
        self.assertEqual(child["apiVersion"], management["apiVersion"])
        self.assertEqual(child["identity"]["type"], "UserAssigned")
        for field in (
            "enableRBAC",
            "disableLocalAccounts",
            "aadProfile",
            "apiServerAccessProfile",
            "oidcIssuerProfile",
            "securityProfile",
            "networkProfile",
            "autoUpgradeProfile",
        ):
            with self.subTest(field=field):
                self.assertEqual(child["properties"][field], management["properties"][field])
        child_pool = child["properties"]["agentPoolProfiles"][0]
        management_pool = management["properties"]["agentPoolProfiles"][0]
        for field, expected in management_pool.items():
            if field not in {"vnetSubnetID", "tags"}:
                self.assertEqual(child_pool[field], expected)
        kubelet = child["properties"]["identityProfile"]["kubeletidentity"]
        self.assertEqual(set(kubelet), {"resourceId", "clientId", "objectId"})
        self.assertIn("allocation", kubelet["resourceId"])
        self.assertIn(".identities.kubelet.id", kubelet["resourceId"])
        self.assertEqual(template["parameters"]["nodeCount"]["defaultValue"], 2)
        self.assertEqual(template["parameters"]["nodeVmSize"]["defaultValue"], "Standard_D4s_v5")
        self.assertEqual(
            set(template["outputs"]["result"]["value"]["values"]),
            {
                "clusterId",
                "clusterName",
                "resourceGroup",
                "fqdn",
                "oidcIssuer",
                "bootstrapAccessRef",
                "radiusIdentityId",
                "radiusClientId",
            },
        )

    def test_allocation_exposes_deterministic_certificate_and_state_names(self):
        allocation = self.network["outputs"]["allocations"]["copy"]["input"]
        self.assertEqual(
            allocation["certificateName"],
            "[format('gateway-{0}', parameters('slots')[copyIndex()])]",
        )
        self.assertEqual(
            allocation["acmeStateSecretName"],
            "[format('acme-{0}', parameters('slots')[copyIndex()])]",
        )
        self.assertIn(
            "outputs.allocations.value",
            self.bootstrap["outputs"]["allocations"]["copy"]["input"],
        )

    def test_gateway_and_issuer_data_grants_target_only_their_slot_objects(self):
        expected = {
            "gatewaySecrets": ("secrets", "certificateName", "gateway", "secretsUser"),
            "issuerCertificates": (
                "certificates",
                "certificateName",
                "certificateIssuer",
                "certificateIssuerRoleId",
            ),
            "issuerCertificateSecrets": (
                "secrets",
                "certificateName",
                "certificateIssuer",
                "secretsUser",
            ),
            "issuerStateSecrets": (
                "secrets",
                "acmeStateSecretName",
                "certificateIssuer",
                "acmeStateRoleId",
            ),
        }
        modules = {
            item["copy"]["name"]: item
            for item in resources(self.platform_access)
            if item["type"] == "Microsoft.Resources/deployments"
        }
        self.assertEqual(set(modules), set(expected))
        for direct_grant in resources(self.platform_access):
            if direct_grant["type"] == "Microsoft.Authorization/roleAssignments":
                body = json.dumps(direct_grant)
                for vault_role in ("secretsUser", "certificateIssuerRoleId", "acmeStateRoleId"):
                    self.assertNotIn(vault_role, body)
        for name, (kind, object_name, identity, role) in expected.items():
            with self.subTest(grant=name):
                module = modules[name]
                parameters = module["properties"]["parameters"]
                self.assertEqual(
                    parameters["objectScope"]["value"],
                    f"[resourceId('Microsoft.KeyVault/vaults/{kind}', "
                    f"parameters('foundation').vaultName, "
                    f"parameters('allocations')[copyIndex()].{object_name})]",
                )
                self.assertIn(
                    f".identities.{identity}.principalId", parameters["principalId"]["value"]
                )
                self.assertIn(role, parameters["roleDefinitionId"]["value"])
                assignment = module["properties"]["template"]["resources"][0]
                self.assertEqual(assignment["scope"], "[parameters('objectScope')]")
                self.assertEqual(assignment["properties"]["principalType"], "ServicePrincipal")

    def test_certificate_and_acme_roles_have_separate_minimal_data_actions(self):
        definitions = [
            item
            for item in resources(self.bootstrap)
            if item["type"] == "Microsoft.Authorization/roleDefinitions"
        ]
        importer = next(item for item in definitions if "'certificate-importer'" in item["name"])
        state_writer = next(item for item in definitions if "'acme-state-writer'" in item["name"])
        prefix = "Microsoft.KeyVault/vaults/"
        for role in (importer, state_writer):
            self.assertEqual(role["properties"]["permissions"][0]["actions"], [])
        self.assertEqual(
            set(importer["properties"]["permissions"][0]["dataActions"]),
            {
                prefix + "certificates/read",
                prefix + "certificates/import/action",
                prefix + "certificates/update/action",
            },
        )
        self.assertEqual(
            set(state_writer["properties"]["permissions"][0]["dataActions"]),
            {
                prefix + "secrets/readMetadata/action",
                prefix + "secrets/getSecret/action",
                prefix + "secrets/setSecret/action",
            },
        )

    def test_bootstrap_keeps_one_vault_without_placeholder_secret_material(self):
        all_resources = list(nested_resources(self.bootstrap))
        self.assertEqual(
            sum(item["type"] == "Microsoft.KeyVault/vaults" for item in all_resources), 1
        )
        self.assertFalse(
            any(
                item["type"]
                in {"Microsoft.KeyVault/vaults/secrets", "Microsoft.KeyVault/vaults/certificates"}
                for item in all_resources
            )
        )

    def test_postgresql_setup_secret_is_owned_by_the_real_mixed_provider_recipe(self):
        template = self.postgresql_recipe
        self.assertEqual(template["imports"]["kubernetes"]["provider"], "Kubernetes")
        self.assertEqual(template["imports"]["kubernetes"]["version"], "1.0.0")
        setup = template["resources"]["setup"]
        self.assertEqual(setup["type"], "core/Secret@v1")
        self.assertEqual(setup["import"], "kubernetes")
        self.assertEqual(setup["properties"]["type"], "Opaque")
        self.assertEqual(
            setup["properties"]["metadata"]["name"],
            "[format('{0}-setup', parameters('context').resource.name)]",
        )
        self.assertEqual(
            setup["properties"]["metadata"]["namespace"],
            "[parameters('context').runtime.kubernetes.namespace]",
        )
        data = setup["properties"]["stringData"]
        self.assertEqual(set(data), {"host", "port", "database", "username", "password"})
        self.assertEqual(data["port"], "5432")
        self.assertEqual(data["password"], "[parameters('administratorPassword')]")
        self.assertIn("database", setup["dependsOn"])
        self.assertIn("server", setup["dependsOn"])
        self.assertIn("tls", setup["dependsOn"])
        result = template["outputs"]["result"]
        self.assertEqual(result["type"].lower(), "secureobject")
        self.assertEqual(
            template["parameters"]["administratorPassword"]["type"].lower(), "securestring"
        )
        self.assertEqual(
            result["value"]["secrets"]["password"], "[parameters('administratorPassword')]"
        )
        self.assertNotIn("password", result["value"]["values"])
        self.assertIn("setupSecretName", result["value"]["values"])
        self.assertTrue(
            any("/providers/core/Secret/" in item for item in result["value"]["resources"])
        )


if __name__ == "__main__":
    unittest.main()
