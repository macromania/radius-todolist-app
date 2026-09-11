import json
import subprocess
from pathlib import Path


def compile_template(relative):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            str(Path.home() / ".rad/bin/bicep"),
            "build",
            str(root / relative),
            "--stdout",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_recreate_base_agrees_with_service_account_before_radius_patching():
    template = compile_template("infra/radius/modules/workload.bicep")
    account_name = template["variables"]["accountBase"]["metadata"]["name"]
    deployment = template["variables"]["deploymentBase"]
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == account_name
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    properties = template["resources"]["workload"]["properties"]["properties"]
    pod = properties["runtimes"]["kubernetes"]["pod"]
    assert pod["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    assert "if(parameters('api'), createObject('livenessProbe'" in properties["container"]
    assert "null()" not in properties["container"]
    assert template["parameters"]["runtimeServiceAccount"]["defaultValue"] == (
        "[parameters('serviceAccount')]"
    )
    assert pod["serviceAccountName"] == "[parameters('runtimeServiceAccount')]"


def test_data_api_separates_pod_identity_without_changing_radius_connection():
    template = compile_template("infra/radius/apps/data.bicep")
    parameters = template["resources"]["api"]["properties"]["parameters"]
    assert parameters["serviceAccount"]["value"] == "data-api"
    assert parameters["runtimeServiceAccount"]["value"] == "data-api-runtime"
    assert parameters["automountToken"]["value"] is True
    assert set(parameters["connections"]["value"]) == {"redis"}
