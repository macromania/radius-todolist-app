import json
import subprocess
from pathlib import Path


def test_recreate_base_agrees_with_service_account_before_radius_patching():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            str(Path.home() / ".rad/bin/bicep"),
            "build",
            str(root / "infra/radius/modules/workload.bicep"),
            "--stdout",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    template = json.loads(result.stdout)
    account_name = template["variables"]["accountBase"]["metadata"]["name"]
    deployment = template["variables"]["deploymentBase"]
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == account_name
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    properties = template["resources"]["workload"]["properties"]["properties"]
    pod = properties["runtimes"]["kubernetes"]["pod"]
    assert pod["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    assert "if(parameters('api'), createObject('livenessProbe'" in properties["container"]
    assert "null()" not in properties["container"]
