import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from plane_demo.management.providers.identity import RESOURCE_SIZING
from plane_demo.management.providers.secret_store import CredentialScope

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts/operations/azure")]
from scripts.operations.config import load_config  # noqa: E402

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
REVISION = "c" * 40
COMPILER = "Bicep CLI version 0.42.1 (test)"
NODE_SIZE = "Standard_D4as_v7"
POSTGRES_SIZE = "Standard_D2ads_v5"
SIZES = {
    "aksTier": "Free",
    "nodeCount": 2,
    "nodeOsDiskSizeGb": 64,
    "postgresStorageSizeGb": 32,
    "redisSkuName": "Balanced_B1",
    "gatewayCapacity": 1,
    "registrySkuName": "Standard",
    "vaultSkuName": "standard",
}
TOOLS = ("az", "rad", "docker", "curl", "git", "bicep", "kubectl", "kubelogin")
FAKE = r"""
import base64,gzip,hashlib,importlib.util,io,json,marshal,os,re,stat,sys,tarfile
from pathlib import Path
from uuid import NAMESPACE_DNS, uuid5
root=Path(os.environ["FAKE_ROOT"])
sys.path.insert(0,str(root))
from scripts.operations.azure import plane_policy
from plane_demo.management.providers.identity import DemoConfig, IDENTITY_PURPOSES, RESOURCE_SIZING
spec=json.loads((root/"spec.json").read_text())
state_path=root/"fake-state.json"
state=json.loads(state_path.read_text()) if state_path.exists() else {"artifacts":{}}
tool=Path(sys.argv[0]).name
args=sys.argv[1:]
mode=spec.get("mode")
project=spec["project"]; deployment=spec["deployment"]; subscription=spec["subscription"]
stem=f"{project}-{deployment}-azure"
scope=f"/subscriptions/{subscription}"
platform=f"{scope}/resourceGroups/rg-{stem}-platform"
identity=hashlib.sha256(f"{subscription}/{project}/{deployment}/azure".encode()).hexdigest()[:20]
registry="acr"+identity; host=registry+".azurecr.io"
external=spec.get("extra_env",{}).get("DEMO_KEY_VAULT")
vault=external or "kv-"+identity
vault_group=spec.get("external_group","shared-secrets") if external else f"rg-{stem}-platform"
vault_id=f"{scope}/resourceGroups/{vault_group}/providers/Microsoft.KeyVault/vaults/{vault}"
tenant="33333333-3333-3333-3333-333333333333"
radius_client="55555555-5555-5555-5555-555555555555"
tags={"project":project,"deployment":deployment,"environment":"azure","managedBy":"radius-todolist-app"}
policy=json.loads((root/"scripts/operations/azure/registry-policy.json").read_text())
registry_mode=("LegacyRegistryPermissions" if mode=="legacy-registry"
               else "AbacRepositoryPermissions")
data_prefix="Microsoft.ContainerRegistry/registries/repositories/"
role_definitions=[
    {"name":policy["repositoryReaderRoleId"],"permissions":[{"actions":[],"dataActions":
        [data_prefix+"content/read",data_prefix+"metadata/read"]}]},
    {"name":policy["repositoryWriterRoleId"],"permissions":[{"actions":[],"dataActions":
        [data_prefix+"content/read",data_prefix+"metadata/read",
         data_prefix+"content/write",data_prefix+"metadata/write"]}]},
    {"name":policy["dataImporterRoleId"],"permissions":[{
        "actions":["Microsoft.ContainerRegistry/registries/importImage/action"],
        "dataActions":[data_prefix+"content/read",data_prefix+"metadata/read",
                      "Microsoft.ContainerRegistry/registries/catalog/read"]}]},
]
def writer_allows(repository, action):
    condition=policy["writerCondition"]
    actions=re.findall(r"ActionMatches\{'([^']+)'\}",condition)
    names=re.findall(r"StringEqualsIgnoreCase '([^']+)'",condition)
    prefixes=re.findall(r"StringStartsWithIgnoreCase '([^']+)'",condition)
    return action in actions and (
        repository.lower() in names or any(repository.lower().startswith(p) for p in prefixes))
slots=["management","shared-control","shared-data"]
def arg(name):
    return args[args.index(name)+1]
def save():
    state_path.write_text(json.dumps(state))
def emit(value):
    print(json.dumps(value))
def registry_credentials(directory):
    config=Path(directory)/"config.json"
    if config.is_symlink() or stat.S_IMODE(config.stat().st_mode)!=0o600:
        sys.exit("registry credentials must remain private")
    expected={"auths":{host:{"identitytoken":"synthetic-registry-token"}}}
    if json.loads(config.read_text())!=expected:
        sys.exit("registry credentials must use an explicit OAuth refresh token")
def compile_bytes(name):
    return json.dumps({
        "resources":[],"metadata":{"source":name},"contentVersion":"1.0.0.0",
        "parameters":{"nodeCount":{"defaultValue":2},
                      "childSlots":{"defaultValue":slots[1:]}}}).encode()
def artifact(name,source):
    blob=compile_bytes(source)
    if mode=="wrong-recipe-content":
        blob=json.dumps({"resources":[],"metadata":{"source":"foreign"}}).encode()
    layer="sha256:"+hashlib.sha256(blob).hexdigest()
    if mode=="wrong-blob-digest": layer="sha256:"+"f"*64
    manifest={"schemaVersion":2,"layers":[{"mediaType":"application/vnd.ms.bicep.module.layer.v1+json",
                                        "digest":layer,"size":len(blob)}]}
    digest="sha256:"+hashlib.sha256(json.dumps(manifest).encode()).hexdigest()
    locked=spec.get("locked_artifacts",False) and name.split(":")[0]!=spec.get("unlocked")
    state["artifacts"][name]={"digest":digest,"manifest":manifest,"blob":blob.decode(),"locked":locked}
def source_tag(name):
    base=root/"committed"
    paths=[f"infra/radius/recipes/azure/{name}.bicep","infra/radius/recipes/azure/bicepconfig.json"]
    if name=="cluster":
        paths += ["scripts/operations/azure/plane-policy.json"]
        paths += [str(p.relative_to(base))
                  for p in sorted((base/"infra/bootstrap").glob("*.bicep"))]
    content="Bicep CLI version 0.42.1 (test)\n"
    content+="".join(hashlib.sha256((base/p).read_bytes()).hexdigest()+"  "+p+"\n" for p in paths)
    return "src-"+hashlib.sha256(content.encode()).hexdigest()
def foundation():
    allocations=[]
    for index,slot in enumerate(slots):
        role="management" if slot=="management" else slot.rsplit("-",1)[1]
        allocations.append({"slot":slot,"clusterName":f"aks-{stem}-{slot}",
            "slotIndex":index,"gatewaySubnetCidr":f"10.64.{16+index}.0/24",
            "clusterResourceGroup":f"rg-{stem}-{slot}",
            "appResourceGroup":f"rg-{stem}-{slot}","namespace":f"{stem}-{slot}-{role}",
            "certificateName":f"gateway-{stem}-{slot}","acmeStateSecretName":f"acme-{stem}-{slot}",
            "identities":{"radius":{"clientId":radius_client,
                "id":f"{scope}/resourceGroups/rg-{stem}-{slot}/providers/"
                     f"Microsoft.ManagedIdentity/userAssignedIdentities/id-{stem}-{slot}-radius"}}})
        allocations[-1]["identities"] = {
            key: {"clientId":radius_client,"principalId":str(uuid5(NAMESPACE_DNS,slot+key)),
                  "id":f"{scope}/resourceGroups/rg-{stem}-{slot}/providers/"
                       f"Microsoft.ManagedIdentity/userAssignedIdentities/id-{stem}-{slot}-{purpose}"}
            for key,purpose in IDENTITY_PURPOSES.items()
        }
    if mode=="wrong-radius-identity": allocations[0]["identities"]["radius"]["id"]+="-foreign"
    if mode=="old-certificate-names":
        allocations[0]["certificateName"]="gateway-"+identity+"-management"
    values={"foundation":{"projectName":project,"deploymentName":deployment,"environment":"azure",
        "resourceGroupLayout":"plane-v2",
        "environmentMode":"prepared-v1",
        "virtualNetworkId":f"{platform}/providers/Microsoft.Network/virtualNetworks/vnet-{stem}",
        "roleDefinitionIds":{key:plane_policy.role_id(subscription,stem,entry[0])
                             for key,entry in plane_policy.ROLE_NAMES.items()},
        "resourcePrefix":stem,"radiusResourceGroup":stem,"subscriptionId":subscription,"tenantId":tenant,
        "location":spec["location"],"platformResourceGroup":f"rg-{stem}-platform",
        "nodeVmSize":state.get("node_vm_size",spec.get("node_vm_size","Standard_D4as_v7")),
        "nodeCount":2,
        "resourceSizingVersion":1,
        **state.get("resource_sizes",spec["resource_sizes"]),
        "postgresSkuName":state.get("postgres_sku",spec.get("postgres_sku","Standard_D2ads_v5")),
        "postgresSkuTier":"GeneralPurpose",
        "registryName":registry,"registryLoginServer":host,"registryRoleAssignmentMode":registry_mode,
        "registryId":f"{platform}/providers/Microsoft.ContainerRegistry/registries/{registry}",
        "vaultName":vault,"vaultId":vault_id,"vaultOwned":not bool(external),
        "vaultResourceGroup":vault_group},
        "allocations":allocations,"managementCluster":{"name":f"aks-{stem}-management",
        "id":f"{scope}/resourceGroups/rg-{stem}-management/providers/"
             f"Microsoft.ContainerService/managedClusters/aks-{stem}-management"}}
    if mode=="foreign-foundation": values["foundation"]["deploymentName"]="foreign"
    if mode=="old-group-layout": values["foundation"].pop("resourceGroupLayout")
    if mode=="old-postgres-selection":
        values["foundation"].pop("postgresSkuName")
        values["foundation"].pop("postgresSkuTier")
    if mode=="foreign-registry-host":
        values["foundation"]["registryLoginServer"]="foreign.azurecr.io"
    status="Failed" if mode=="unready-foundation" else "Succeeded"
    return {"properties":{"provisioningState":status,
        "parameters":{key:{"value":value} for key,value in
            {"projectName":project,"deploymentName":deployment,"environment":"azure",
             "vaultName":vault,
             "externalVaultResourceGroup":vault_group if external else ""}.items()},
        "outputs":{key:{"type":"Object","value":value} for key,value in values.items()}}}
entry={"tool":tool,"args":args,"cwd":os.getcwd(),"docker_config":os.environ.get("DOCKER_CONFIG"),
       "home":os.environ.get("HOME"),"azure_config_dir":os.environ.get("AZURE_CONFIG_DIR"),
       "remote_verification":os.environ.get("FAKE_REMOTE_VERIFICATION")=="yes"}
if "--config" in args and tool=="curl":
    file=Path(arg("--config"))
    entry["config_mode"]=stat.S_IMODE(file.stat().st_mode)
    entry["private_parent"]=stat.S_IMODE(file.parent.stat().st_mode)
if tool=="az" and args[:3]==["deployment","sub","create"]:
    entry["parameters"]=json.loads(Path(arg("--parameters")[1:]).read_text())["parameters"]
    entry["template_exists"]=Path(arg("--template-file")).is_file()
if tool=="az" and args[:2]==["acr","build"]:
    entry["source_value"]=(Path.cwd()/"src/selected.txt").read_text()
    entry["env_in_context"]=(Path.cwd()/".env").exists()
    entry["source_mode"]=stat.S_IMODE((Path.cwd()/"src/plane_demo/management/api.py").stat().st_mode)
    entry["source_directory_mode"]=stat.S_IMODE((Path.cwd()/"src/plane_demo/management").stat().st_mode)
    entry["extension_mode"]=stat.S_IMODE((Path.cwd()/"infra/radius/types/clusters.tgz").stat().st_mode)
    entry["workspace_mode"]=stat.S_IMODE(Path.cwd().parent.stat().st_mode)
with (root/"calls.jsonl").open("a") as stream:
    stream.write(json.dumps(entry)+"\n")
if spec.get("fail") and [tool,*args[:len(spec["fail"])-1]]==spec["fail"]:
    print("synthetic native command failure",file=sys.stderr)
    sys.exit(17)
if tool=="bicep":
    if args==["--version"]:
        print("Bicep CLI version "+("0.43.0" if mode=="wrong-bicep" else "0.42.1")+" (test)")
    elif args[0]=="build":
        if not Path(args[1]).is_file(): sys.exit("missing compile input")
        Path(arg("--outfile")).write_bytes(compile_bytes(Path(args[1]).stem))
    else: sys.exit("unexpected Bicep command")
elif tool=="git":
    command=args[2] if args[:1]==["-C"] else args[0]
    if command=="rev-parse":
        print("d"*40 if mode=="wrong-revision" else spec["revision"])
    elif command=="archive":
        with tarfile.open(fileobj=sys.stdout.buffer,mode="w|") as archive:
            for path in sorted((root/"committed").iterdir()):
                archive.add(path,arcname=path.name)
    elif command=="show": print(spec.get("commit_time",1700000000))
    else: sys.exit("unexpected git command")
elif tool=="az":
    if args[:2]==["vm","list-skus"]:
        sizes=[]
        for name,family,memory in (
            ("Standard_D4as_v7","StandardDasv7Family",16),
            ("Standard_D4s_v7","StandardDsv7Family",16),
            ("Standard_E4as_v7","StandardEasv7Family",32)):
            sizes.append({"name":name,"family":family,"locations":[spec["location"]],
                "restrictions":[],"capabilities":[{"name":key,"value":value} for key,value in
                {"vCPUs":"4","MemoryGB":str(memory),"CpuArchitectureType":"x64",
                 "HyperVGenerations":"V2","PremiumIO":"True",
                 "EphemeralOSDiskSupported":"False","DiskControllerTypes":"NVMe"}.items()]})
        emit(spec.get("vm_skus",sizes))
    elif args[:2]==["vm","list-usage"]:
        emit(spec.get("vm_usage",[
            {"name":{"value":name},"currentValue":0,"limit":100}
            for name in ("cores","StandardDasv7Family","StandardDsv7Family",
                         "StandardEasv7Family")]))
    elif args[:2]==["aks","list"]:
        emit(spec.get("existing_clusters",[]))
    elif args[:3]==["postgres","flexible-server","list-skus"]:
        emit(spec.get("postgres_capabilities",[{
            "supportedServerVersions":[{"name":"16","status":None,"reason":None}],
            "supportedServerEditions":[{"name":"GeneralPurpose",
                "supportedStorageEditions":[{"name":"ManagedDisk","supportedStorageMb":[
                    {"storageSizeMb":n*1024} for n in (32,64,128,256)]}],
                "supportedServerSkus":[
                {"name":name,"vCores":2,"supportedMemoryPerVcoreMb":4096,
                 "supportedZones":["1","2","3"],"status":None,"reason":None}
                for name in ("Standard_D2ads_v5","Standard_D2ds_v4","Standard_D2ds_v5")]}]}]))
    elif args[:2]==["provider","show"]:
        registered=arg("--namespace") in state.get("providers",[])
        if mode=="unregistered-providers":
            value=(spec.get("provider_registration_state","Registering")
                   if registered else "NotRegistered")
        else:
            value="Registered"
        phase="after-registration" if registered else "initial"
        if spec.get("malformed_provider_query")==phase:
            print("not json")
        else:
            kinds={
                "Microsoft.ContainerService":["managedClusters"],
                "Microsoft.DBforPostgreSQL":["flexibleServers"],
                "Microsoft.Cache":["redisEnterprise"],
                "Microsoft.Network":["applicationGateways","natGateways","publicIPAddresses"],
                "Microsoft.ContainerRegistry":["registries"],
                "Microsoft.KeyVault":["vaults"],
            }
            emit({"namespace":arg("--namespace"),"registrationState":value,
                  "resourceTypes":[{"resourceType":kind,"locations":[spec["location"]]}
                                   for kind in kinds.get(arg("--namespace"),[])]})
    elif args[:2]==["provider","register"]:
        state.setdefault("providers",[]).append(arg("--namespace"))
        save()
        print("WARNING: Registering is still on-going. You can monitor using "
              f"'az provider show -n {arg('--namespace')}'",file=sys.stderr)
    elif args[:2]==["feature","show"]:
        emit({"properties":{"state":state.get("feature",spec.get("feature_state","Registered"))}})
    elif args[:2]==["feature","register"]:
        state["feature"]="Registered"
        save()
    elif args[:3]==["deployment","sub","validate"]:
        emit({"properties":{"provisioningState":"Succeeded"}})
    elif args[:2]==["account","show"]:
        emit({"id":subscription,"tenantId":tenant,"user":{"type":"user"},"state":"Enabled"})
    elif args[:2]==["keyvault","list"]:
        name=vault.upper() if mode=="external-case" else vault
        emit([] if mode=="missing-external-vault" else [{"id":vault_id,"name":name}])
    elif args[:2]==["keyvault","show"]:
        properties={"tenantId":tenant,"provisioningState":"Succeeded","enableRbacAuthorization":True,
            "publicNetworkAccess":"Disabled","networkAcls":{"defaultAction":"Deny","bypass":"AzureServices"},
            "enableSoftDelete":True,"enablePurgeProtection":True,
            "vaultUri":f"https://{vault}.vault.azure.net/","sku":{"name":"standard"}}
        if mode=="external-public": properties["publicNetworkAccess"]="Enabled"
        if mode=="external-tenant": properties["tenantId"]="44444444-4444-4444-4444-444444444444"
        if mode=="external-access-policy": properties["enableRbacAuthorization"]=False
        if mode=="external-no-bypass": properties["networkAcls"]["bypass"]="None"
        name=vault.upper() if mode=="external-case" else vault
        emit({"id":vault_id,"name":name,"properties":properties,"tags":{"owner":"platform-team"}})
    elif args[:2]==["account","get-access-token"]:
        emit({"accessToken":"synthetic-graph-token"})
    elif args[:2]==["group","list"]:
        if mode in ("foreign-case-group","owned-case-group"):
            owner={**tags,**({"deployment":"foreign"} if mode=="foreign-case-group" else {})}
            emit([{"name":f"rg-{stem}-platform".upper(),"tags":owner}])
        elif mode in ("existing-owned-nodes","foreign-node-cluster"):
            emit([{"name":f"rg-{stem}-management-nodes","tags":{"aks-managed":"true"}}])
        elif mode in ("foreign-group","foreign-resource","existing-owned","old-postgres-selection"):
            owner={**tags,**({"deployment":"foreign"} if mode=="foreign-group" else {})}
            emit([{"name":f"rg-{stem}-platform","tags":owner}])
        else: emit([])
    elif args[:2]==["aks","show"]:
        emit({"id":f"{scope}/resourceGroups/rg-{stem}-management/providers/"
                   f"Microsoft.ContainerService/managedClusters/aks-{stem}-management",
              "nodeResourceGroup":f"rg-{stem}-management-nodes",
              "fqdn":"management.synthetic.azmk8s.io","provisioningState":"Succeeded",
              "tags":{**tags,**({"project":"foreign"} if mode in (
                  "foreign-node-cluster","foreign-management-access") else {})}})
    elif args[:2]==["aks","get-credentials"]:
        context=arg("--context")
        server="https://management.synthetic.azmk8s.io"
        if mode=="wrong-management-server": server="https://foreign.example"
        profile={"apiVersion":"v1","kind":"Config","current-context":context,
            "contexts":[{"name":context,"context":{"cluster":context,"user":context}}],
            "clusters":[{"name":context,"cluster":{
                "server":server,"certificate-authority-data":"c3ludGhldGljLWNh"}}],
            "users":[{"name":context,"user":{"exec":{"command":"kubelogin"}}}]}
        Path(arg("--file")).write_text(json.dumps(profile))
    elif args[:2]==["resource","list"]:
        if "--resource-group" in args:
            emit([{"type":"Microsoft.Network/virtualNetworks","tags":
                {**tags,**({"project":"foreign"} if mode=="foreign-resource" else {})}}])
        elif mode in ("global-foreign","existing-owned","legacy-registry"):
            name=arg("--name"); resource_type=arg("--resource-type")
            emit([{"id":f"{platform}/providers/{resource_type}/{name}",
                "tags":{**tags,**({"deployment":"foreign"} if mode=="global-foreign" else {})}}])
        else: emit([])
    elif args[:2]==["acr","check-name"]:
        emit({"nameAvailable":True})
    elif args[:1]==["rest"]:
        if arg("--method")=="get":
            url=arg("--url")
            if url.startswith("https://graph.microsoft.com/"):
                emit({"value":[]});sys.exit()
            config=DemoConfig("azure",project,deployment,subscription,spec["location"])
            doc={key:value["value"] for key,value in foundation()["properties"]["outputs"].items()}
            principals,grants,definitions=plane_policy.expected_grants(config,doc)
            if "/roleAssignments?" in url:
                rows=[{"id":sc+"/providers/Microsoft.Authorization/roleAssignments/"
                       +str(uuid5(NAMESPACE_DNS,str((p,r,sc)))),
                       "properties":{"principalId":p,"roleDefinitionId":r,"scope":sc}}
                      for p,r,sc in grants]
                if mode=="excess-plane-grant":
                    rows[0]["properties"]["roleDefinitionId"]=scope+"/providers/Microsoft.Authorization/roleDefinitions/b24988ac-6180-42a0-ab88-20f7382dd24c"
                emit({"value":rows});sys.exit()
            rid=url.removeprefix("https://management.azure.com").split("?")[0]
            if rid in definitions:
                emit({"id":rid,"properties":{"type":"CustomRole",**definitions[rid]}})
            else:
                emit({"id":rid,"properties":{"type":"BuiltInRole"}})
            sys.exit()
        expected_url=f"https://management.azure.com{scope}/providers/Microsoft.KeyVault/"
        expected_url+="checkNameAvailability?api-version=2024-11-01"
        body=json.loads(Path(arg("--body")[1:]).read_text())
        if arg("--method")!="post" or arg("--url")!=expected_url:
            sys.exit("unexpected ARM request")
        if body!={"name":vault,"type":"Microsoft.KeyVault/vaults"}:
            sys.exit("wrong vault availability request")
        emit({"nameAvailable":mode!="retained-vault"})
    elif args[:3]==["deployment","sub","list"]:
        if "environment-" in arg("--query"):
            emit([]);sys.exit()
        prior=foundation()
        if mode=="active-bootstrap":
            prior["properties"]["provisioningState"]="Running"
        if mode=="foreign-deployment":
            prior["properties"]["parameters"]["deploymentName"]["value"]="foreign"
        emit([prior] if mode in ("foreign-deployment","existing-owned","active-bootstrap",
                                "owned-case-group","existing-owned-nodes",
                                "old-group-layout","old-postgres-selection") else [])
    elif args[:3]==["deployment","sub","create"]:
        state["node_vm_size"]=entry["parameters"]["nodeVmSize"]["value"]
        state["postgres_sku"]=entry["parameters"]["postgresSkuName"]["value"]
        state["resource_sizes"]={
            field:entry["parameters"][field]["value"] for _,field,_ in RESOURCE_SIZING.values()}
        save()
        if not entry["parameters"]["registryExists"]["value"]:
            state["arm_tags"]={}
            save()
        status="Failed" if mode=="failed-create" else "Succeeded"
        emit({"properties":{"provisioningState":status}})
    elif args[:3]==["deployment","sub","show"]:
        emit(foundation())
    elif args[:2]==["acr","show"]:
        emit({"id":f"{platform}/providers/Microsoft.ContainerRegistry/registries/{registry}",
            "loginServer":host,"provisioningState":"Succeeded","roleAssignmentMode":registry_mode,
            "adminUserEnabled":False,"anonymousPullEnabled":False,"tags":
            {**tags,**state.get("arm_tags",{}),
             **({"project":"foreign"} if mode=="foreign-registry" else {})}})
    elif args[:3]==["role","assignment","list"]:
        if "--scope" in args and "--all" in args:
            sys.exit("group or scope are not required when --all is used")
        assignments=[]
        for definition in role_definitions:
            row={"roleDefinitionId":"/providers/Microsoft.Authorization/roleDefinitions/"
                 +definition["name"],"scope":arg("--scope")}
            if definition["name"]==policy["repositoryWriterRoleId"]:
                row.update(condition=policy["writerCondition"],conditionVersion="2.0")
                if mode=="unrestricted-writer": row["condition"]=None
            assignments.append(row)
        emit(assignments)
    elif args[:3]==["role","definition","list"]:
        emit([definition for definition in role_definitions if definition["name"]==arg("--name")]
             if "--name" in args else [])
    elif args[:2]==["tag","update"]:
        if arg("--operation")!="Merge": sys.exit("unsafe ARM tag replacement")
        key,value=arg("--tags").split("=",1)
        state.setdefault("arm_tags",{})[key]=value
        save();emit({"properties":{"tags":state["arm_tags"]}})
    elif args[:3]==["network","private-endpoint","show"]:
        emit({"id":f"{platform}/providers/Microsoft.Network/privateEndpoints/pe-{stem}-vault",
              "tags":tags,"provisioningState":"Succeeded","privateLinkServiceConnections":[{
                "privateLinkServiceId":vault_id,"groupIds":["vault"],
                "privateLinkServiceConnectionState":{
                    "status":"Pending" if mode=="pending-vault-endpoint" else "Approved"}}]})
    elif args[:2]==["acr","login"]:
        emit({"accessToken":"synthetic-registry-token","loginServer":host})
    elif args[:3]==["acr","repository","list"]:
        if mode in ("existing-recipes","wrong-recipe-content","wrong-blob-digest") or (
            spec.get("existing_recipes")):
            for name in ("cluster","postgresql","gateway","redis"):
                if name==spec.get("missing_recipe"): continue
                key=f"radius-recipes/{name}:{source_tag(name)}"
                if key not in state["artifacts"]: artifact(key,name)
        if mode=="existing-image" or spec.get("existing_images"):
            for component,digit in (("api","e"),("provisioner","d")):
                if component==spec.get("missing_image"): continue
                locked=(spec.get("locked_artifacts",False)
                        and "plane-"+component!=spec.get("unlocked"))
                state["artifacts"].setdefault("plane-"+component+":"+spec["revision"],
                                             {"digest":"sha256:"+digit*64,"locked":locked})
        save()
        emit(sorted({key.split(":")[0] for key in state["artifacts"]}))
    elif args[:3]==["acr","repository","show-tags"]:
        repository=arg("--repository")
        emit([key.split(":",1)[1] for key in state["artifacts"] if key.startswith(repository+":")])
    elif args[:3] in (["acr","repository","show"],["acr","repository","update"]):
        record=state["artifacts"][arg("--image")]
        if args[2]=="update":
            if not writer_allows(arg("--image").split(":")[0],data_prefix+"metadata/write"):
                sys.exit("ABAC denied canonical metadata write")
            record["locked"]=True
            if mode=="changed-lock": record["digest"]="sha256:"+"e"*64
            save()
        emit({"digest":record["digest"],"changeableAttributes":{
            "writeEnabled":not record.get("locked",False),
            "deleteEnabled":not record.get("locked",False)}})
    elif args[:3]==["acr","manifest","show"]:
        repository,digest=arg("--name").split("@")
        record=next(value for key,value in state["artifacts"].items()
                    if key.startswith(repository+":") and value["digest"]==digest)
        emit(record["manifest"])
    elif args[:2]==["acr","run"]:
        from unittest.mock import patch
        context=Path.cwd()
        request=json.loads((context/"request.json").read_text())
        identifier="vi"+str(len(state.setdefault("runs",{}))+1)
        state["runs"][identifier]={"runId":identifier,"status":"Running","runType":"QuickRun",
            "platform":{"os":"linux","architecture":"amd64"},"outputImages":[]}
        save()
        sys.path.insert(0,str(context/"verifier"))
        import remote_inspection
        remote_home=root/"remote-home"
        docker_config=remote_home/".docker"
        docker_config.mkdir(parents=True,exist_ok=True)
        (docker_config/"config.json").write_text(json.dumps(
            {"auths":{host:{"identitytoken":"synthetic-registry-token"}}}))
        (docker_config/"config.json").chmod(0o600)
        os.environ.update(ACR_RUN_ID=identifier,FAKE_REMOTE_VERIFICATION="yes",
                          DOCKER_CONFIG=str(docker_config),HOME=str(remote_home))
        output=context/"result/inspection.json"
        try:
            with patch.object(remote_inspection.platform,"system",return_value="Linux"), \
                 patch.object(remote_inspection.platform,"machine",return_value="x86_64"), \
                 patch.object(remote_inspection.urllib.request,"urlopen",
                              side_effect=lambda *a,**kw:(root/"kubelogin.zip").open("rb")):
                remote_inspection.execute(request,context/"source.tar",root/"bin/docker",output)
        except (ValueError,OSError,remote_inspection.subprocess.CalledProcessError) as error:
            state=json.loads(state_path.read_text())
            state["runs"][identifier]["status"]="Failed";save()
            print("REMOTE VERIFICATION FAILED: "+str(error),file=sys.stderr)
            sys.exit(1)
        state=json.loads(state_path.read_text())
        report=output.read_bytes()
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode="w") as tar:
            member=tarfile.TarInfo("inspection.json");member.size=len(report)
            tar.addfile(member,io.BytesIO(report))
        blob=gzip.compress(archive.getvalue())
        layer="sha256:"+hashlib.sha256(blob).hexdigest()
        manifest={"schemaVersion":2,"layers":[{"digest":layer,"size":len(blob),
            "mediaType":"application/vnd.docker.image.rootfs.diff.tar.gzip"}]}
        digest="sha256:"+hashlib.sha256(json.dumps(manifest).encode()).hexdigest()
        repository="plane-"+request["component"];tag="verify-"+request["context_id"]
        state["artifacts"][repository+":"+tag]={"digest":digest,"manifest":manifest,
            "blob_base64":base64.b64encode(blob).decode(),"locked":False}
        state["runs"][identifier].update(status="Succeeded",outputImages=[{
            "registry":host,"repository":repository,"tag":tag,"digest":digest}])
        save();print("VISIBLE REMOTE VERIFICATION")
    elif args[:2]==["acr","build"]:
        key=arg("--image")
        if arg("--source-acr-auth-id")!="[caller]": sys.exit("ABAC requires caller authentication")
        if not writer_allows(key.split(":")[0],data_prefix+"content/write"):
            sys.exit("ABAC denied build push")
        digest="sha256:"+hashlib.sha256(key.encode()).hexdigest()
        state["artifacts"][key]={"digest":digest,"locked":False}
        state.setdefault("manifests",{})[digest]=dict(state["artifacts"][key])
        run_id="ca"+str(len(state.setdefault("runs",{}))+1)
        repository,tag=key.split(":")
        state["runs"][run_id]={"runId":run_id,"status":"Succeeded","runType":"QuickRun",
            "createTime":"2026-09-17T11:00:00+00:00",
            "platform":{"os":"linux","architecture":"amd64"},
            "outputImages":[{"registry":host,"repository":repository,"tag":tag,"digest":digest}]}
        lines=Path(arg("--file")).read_text().replace("\\\n"," ").splitlines()
        steps=[line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
        state.setdefault("run_logs",{})[run_id]="\n".join(
            f"Step {i}/{len(steps)} : {step}" for i,step in enumerate(steps,1))
        if mode=="failed-run": state["runs"][run_id]["status"]="Failed"
        if mode=="staging-race": state["artifacts"][key]["digest"]="sha256:"+"f"*64
        save()
        print("WARNING: Queued a build with ID: "+run_id,file=sys.stderr)
        if "--no-wait" not in args:
            print("VISIBLE BUILD "+key)
            print("VISIBLE PUSH "+digest)
            if mode=="failed-run": sys.exit("synthetic native build failure")
            if mode=="lost-build-result": sys.exit("synthetic log stream interruption")
    elif args[:3]==["acr","task","list-runs"]:
        if "--image" in args: sys.exit("run matching must not depend on mutable image tags")
        values=list(state.get("runs",{}).values())
        if mode=="missing-build-run": values=[]
        if mode=="ambiguous-build-run" and values:
            values.append({**values[-1],"runId":"duplicate"})
        emit(values)
    elif args[:3]==["acr","task","logs"]:
        run=state["runs"][arg("--run-id")]
        image=run["outputImages"][0]
        print(state.get("run_logs",{}).get(arg("--run-id"),""))
        print("VISIBLE BUILD "+image["repository"]+":"+image["tag"])
        print("VISIBLE PUSH "+image["digest"])
    elif args[:3]==["acr","task","show-run"]:
        run=state["runs"][arg("--run-id")]
        if mode=="failed-run": run={**run,"status":"Failed"}
        emit(run)
    elif args[:2]==["acr","import"]:
        if "--force" in args: sys.exit("unsafe forced import")
        key=arg("--image")
        collision=mode=="recipe-import-conflict" and key.startswith("radius-recipes/")
        collision=collision or (mode=="import-conflict" and key.startswith("plane-"))
        if key in state["artifacts"] or collision: sys.exit("tag already exists")
        repository,digest=arg("--source").split("@")
        record=state.get("manifests",{}).get(digest)
        if record is None:
            record=next(value for key,value in state["artifacts"].items()
                        if key.startswith(repository+":") and value["digest"]==digest)
        state["artifacts"][key]=dict(record); save()
        print("VISIBLE IMPORT "+key)
    else: sys.exit("unexpected Azure command: "+repr(args))
elif tool=="rad":
    if args==["version","--cli"]: print("RELEASE VERSION: v0.60.2")
    elif "install" in args and "kubernetes" in args:
        if mode=="radius-install-failure": sys.exit("synthetic Radius installation failure")
        state["radius_installs"]=state.get("radius_installs",0)+1;save()
        print("VISIBLE RADIUS INSTALL")
    elif "workspace" in args and "create" in args:
        Path(arg("--config")).write_text("{}")
    elif "credential" in args and "register" in args:
        if arg("--client-id")!=radius_client or arg("--tenant-id")!=tenant:
            sys.exit("wrong Radius identity")
    elif "publish-extension" in args:
        inner=io.BytesIO(); outer=io.BytesIO()
        with tarfile.open(fileobj=inner,mode="w:gz") as archive:
            for name in ("index.json","types.json"):
                member=tarfile.TarInfo(name);member.size=2
                archive.addfile(member,io.BytesIO(b"{}"))
        with tarfile.open(fileobj=outer,mode="w:gz") as archive:
            member=tarfile.TarInfo("types.tgz");member.size=len(inner.getvalue())
            archive.addfile(member,io.BytesIO(inner.getvalue()))
        Path(arg("--target")).write_bytes(outer.getvalue())
        print("VISIBLE EXTENSION")
    elif "publish" in args:
        registry_credentials(os.environ["DOCKER_CONFIG"])
        key=arg("--target").split("/",1)[1]
        if not writer_allows(key.split(":")[0],data_prefix+"content/write"):
            sys.exit("ABAC denied canonical Recipe push")
        artifact(key,Path(arg("--file")).stem); save()
        print("VISIBLE PUBLISH "+key)
    else: sys.exit("unexpected Radius command")
elif tool=="kubelogin":
    if args[:1]!=["convert-kubeconfig"]: sys.exit("unexpected kubelogin operation")
elif tool=="kubectl":
    if "namespace" in args or "namespaces" in args:
        sys.exit("bootstrap must not require an application namespace")
    if "config" in args:
        profile=json.loads(Path(arg("--kubeconfig")).read_text())
        if "current-context" in args: print(profile["current-context"])
        elif "view" in args: emit(profile)
        else: sys.exit("unexpected kubeconfig operation")
    elif "nodes" in args: emit({"items":[{"metadata":{"name":"owned-management-node"}}]})
    elif "pods" in args:
        accounts=["applications-rp","bicep-de","ucp","dynamic-rp"]
        if mode=="radius-wi-failure": accounts.pop()
        emit({"items":[{"metadata":{"name":account},"spec":{"serviceAccountName":account,
            "containers":[{"env":[{"name":"AZURE_CLIENT_ID","value":radius_client},
                                 {"name":"AZURE_TENANT_ID","value":tenant},
                                 {"name":"AZURE_FEDERATED_TOKEN_FILE","value":"/token"}]}]}}
            for account in accounts]})
    elif "rollout" in args:
        if mode=="radius-rollout-failure": sys.exit("synthetic rollout failure")
        print("ready")
    elif "annotate" in args or "patch" in args: print("updated")
    else: sys.exit("unexpected Kubernetes operation")
elif tool=="docker":
    if os.environ.get("FAKE_REMOTE_VERIFICATION")!="yes":
        sys.exit("Azure verification must not invoke workstation Docker")
    native=list(args)
    while native and native[0] in ("--host","--config"): native=native[2:]
    if native[:3]==["context","inspect","desktop-linux"]:
        emit("unix:///synthetic/docker.sock")
    elif native[:1]==["info"]:
        emit({"OSType":"linux","OperatingSystem":"Docker Desktop"})
    elif native[:1]==["pull"]:
        registry_credentials(arg("--config"))
        reference=native[-1]
        image_id="sha256:"+hashlib.sha256(reference.encode()).hexdigest()
        component="api" if "/plane-api@" in reference else "provisioner"
        state.setdefault("image_ids",{})[image_id]={"reference":reference,"component":component}
        state["pulled"]=image_id;save();print("VISIBLE PULL")
    elif native[:2]==["image","inspect"]:
        image_id=state["pulled"];image=state["image_ids"][image_id];component=image["component"]
        module="plane_demo.management."+("api" if component=="api" else "provisioner")
        emit([{"Id":image_id,"Architecture":"amd64","Os":"linux","RepoDigests":[image["reference"]],
              "Config":{"User":"10001:10001","WorkingDir":"/app","Entrypoint":None,"Volumes":None,
                        "Env":["PYTHONPATH=/app/src"],
                        "Cmd":["python","-m",module],
                        "Labels":{"org.opencontainers.image.revision":spec["revision"]}}}])
    elif native[:2]==["container","create"]:
        image_id=native[-1];component=state["image_ids"][image_id]["component"]
        cid=("a" if component=="api" else "b")*64
        key,value=arg("--label").split("=",1)
        if mode=="foreign-container": value="foreign"
        state.setdefault("containers",{})[cid]={"Id":cid,"Name":"/"+arg("--name"),"Image":image_id,
            "Config":{"Labels":{key:value}},"State":{"Running":False},
            "HostConfig":{"NetworkMode":"none","ReadonlyRootfs":True},"Mounts":[]}
        save();print(cid)
    elif native[:2]==["container","inspect"]:
        emit([state["containers"][native[-1]]])
    elif native[:2]==["container","export"]:
        if mode=="export-failure": sys.exit("synthetic export failure")
        cid=native[-1];component=state["image_ids"][state["containers"][cid]["Image"]]["component"]
        sys.path.insert(0,str(root/"scripts/operations/azure"))
        import image_inspection
        path=Path(arg("--output")); source=path.parent/"source"
        sources=image_inspection.expected_files(source,component)
        content={name:p.read_bytes() for name,p in sources.items()}
        content["usr/local/bin/python3.13"]=b"synthetic interpreter"
        content["app/.venv/bin/python"]=b"synthetic venv launcher"
        if component=="provisioner":
            for name in image_inspection.expected_tools(source,path.parent/"kubelogin.zip"):
                content[name]=("synthetic "+name.rsplit("/",1)[1]).encode()
        if mode=="changed-api" and component=="api":
            content["app/src/plane_demo/management/api.py"]=b"wrong code"
        if mode=="extra-api-private" and component=="api":
            content["app/scripts/hidden.sh"]=b"privileged"
        if mode=="missing-private" and component=="provisioner":
            content.pop("app/src/plane_demo/management/providers/secret_store.py")
        if mode=="poison-interpreter":
            content["app/.venv/bin/python"]=b"poisoned interpreter but matching source files"
        if mode=="poison-pyc":
            original=content["app/src/plane_demo/management/api.py"]
            code=compile("raise RuntimeError('poison')","src/plane_demo/management/api.py","exec")
            content["app/src/plane_demo/management/__pycache__/api.cpython-313.pyc"]=(
                importlib.util.MAGIC_NUMBER+(3).to_bytes(4,"little")+
                importlib.util.source_hash(original)+marshal.dumps(code))
        if mode=="missing-kubelogin": content.pop("usr/local/bin/kubelogin",None)
        if mode=="wrong-kubelogin" and component=="provisioner":
            content["usr/local/bin/kubelogin"]=b"wrong binary"
        with tarfile.open(path,mode="w") as archive:
            directories={str(parent) for name in content for parent in Path(name).parents
                         if str(parent)!="."}
            for name in sorted(directories,key=lambda value:(value.count("/"),value)):
                original=source/name.removeprefix("app/")
                copied=name in ("app/src","app/scripts","app/infra","app/sql") or (
                    name.startswith(("app/src/","app/scripts/","app/infra/","app/sql/")))
                member=tarfile.TarInfo(name);member.type=tarfile.DIRTYPE
                member.mode=stat.S_IMODE(original.stat().st_mode) if (
                    copied and original.is_dir()) else 0o755
                if mode=="untraversable-source" and name=="app/src/plane_demo/management":
                    member.mode=0o700
                archive.addfile(member)
            for name,value in content.items():
                member=tarfile.TarInfo(name);member.size=len(value)
                member.mode=stat.S_IMODE(sources[name].stat().st_mode) if name in sources else (
                    0o755 if name.startswith(("usr/local/bin/","home/plane/.rad/bin/"))
                    or name=="app/.venv/bin/python" else 0o644)
                if mode=="unreadable-source" and name=="app/src/plane_demo/management/api.py":
                    member.mode=0o600
                if mode=="nonexecutable-kubelogin" and name=="usr/local/bin/kubelogin":
                    member.mode=0o644
                archive.addfile(member,io.BytesIO(value))
    elif native[:2]==["container","rm"]:
        state["containers"].pop(native[-1]);save();print(native[-1])
    else: sys.exit("unexpected Docker command")
elif tool=="curl":
    url=next(value for value in args if value.startswith("https://"))
    if url.startswith("https://prices.azure.com/"):
        if "Redis" in url:
            emit({"NextPageLink":None,"Items":[
                {"armSkuName":"Azure_Managed_Redis_Balanced_"+name,"skuName":name,
                 "productName":"Azure Managed Redis - Balanced","armRegionName":spec["location"],
                 "type":"Consumption","currencyCode":"USD","unitOfMeasure":"1 Hour",
                 "retailPrice":price}
                for name,price in (("B0",0.016),("B1",0.032),("B3",0.065),
                                   ("B5",0.156),("B10",0.315),("B20",0.629))]})
            sys.exit()
        emit({"NextPageLink":None,"Items":[
            {"armSkuName":name,"armRegionName":spec["location"],"type":"Consumption",
             "currencyCode":"USD","unitOfMeasure":"1 Hour","productName":"Virtual Machines Linux",
             "meterName":name,"retailPrice":price}
            for name,price in (("Standard_D4as_v7",0.182),("Standard_D4s_v7",0.254),
                               ("Standard_E4as_v7",0.3))]})
    elif "github.com/Azure/kubelogin/" in url:
        Path(arg("--output")).write_bytes((root/"kubelogin.zip").read_bytes())
    elif "api.ipify.org" in url:
        print(spec.get("operator_ip","8.8.4.4"),end="")
    elif "graph.microsoft.com" in url:
        Path(arg("--output")).write_text(json.dumps({"id":"22222222-2222-2222-2222-222222222222"}))
    elif "/oauth2/token" in url:
        config=Path(arg("--config")).read_text()
        if "refresh_token=synthetic-registry-token" not in config: sys.exit("wrong token exchange")
        Path(arg("--output")).write_text('{"access_token":"synthetic-pull-token"}')
    elif "/blobs/" in url:
        digest=url.rsplit("/",1)[1]
        record=next(value for value in state["artifacts"].values()
                    if value.get("manifest",{}).get("layers",[{}])[0].get("digest")==digest)
        if "blob_base64" in record:
            blob=base64.b64decode(record["blob_base64"])
            if mode=="remote-report-corrupt": blob=b"corrupt report"
            Path(arg("--output")).write_bytes(blob)
        else:
            Path(arg("--output")).write_text(record["blob"])
    else: sys.exit("unexpected HTTP request")
else: sys.exit("unexpected native tool")
"""


@pytest.fixture
def checkout(tmp_path):
    for relative in (
        "scripts/lib/output.sh",
        "scripts/operations/output.py",
        "scripts/operations/azure/prerequisites.py",
        "scripts/operations/azure/node_sizes.py",
        "scripts/operations/azure/compute_selection.py",
        "scripts/operations/azure/postgres_sizes.py",
        "scripts/operations/azure/resource_sizes.py",
        "scripts/operations/azure/catalog.py",
        "scripts/operations/config.py",
        "scripts/lib/env.sh",
        "scripts/lib/discovery.sh",
        "scripts/operations/install-radius.sh",
        "scripts/operations/azure/azure.shlib",
        "scripts/operations/azure/bootstrap.sh",
        "scripts/operations/azure/build.sh",
        "scripts/operations/azure/images.shlib",
        "scripts/operations/azure/image_inspection.py",
        "scripts/operations/azure/remote_inspection.py",
        "scripts/operations/azure/build_provenance.py",
        "scripts/operations/azure/provenance.shlib",
        "scripts/operations/azure/registry-policy.json",
        "scripts/operations/azure/registry_policy.py",
        "scripts/operations/azure/plane-policy.json",
        "scripts/operations/azure/plane_policy.py",
        "src/plane_demo/management/providers/identity.py",
        "src/plane_demo/management/providers/azure_environments.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        original = ROOT / relative
        if relative == "scripts/operations/azure/bootstrap.sh":
            original = ROOT / "scripts/operations/azure/foundation.sh"
        shutil.copyfile(original, path)
    for relative in (
        "src/selected.txt",
        "sql/management.sql",
        "scripts/selected.sh",
        "infra/bootstrap/azure.bicep",
        "infra/bootstrap/network.bicep",
        "infra/radius/recipes/azure/cluster.bicep",
        "infra/radius/recipes/azure/postgresql.bicep",
        "infra/radius/recipes/azure/gateway.bicep",
        "infra/radius/recipes/azure/redis.bicep",
        "infra/radius/recipes/azure/bicepconfig.json",
        "infra/radius/types/clusters.yaml",
        "images/api/Dockerfile",
        "images/provisioner/Dockerfile",
        "pyproject.toml",
        "uv.lock",
        ".dockerignore",
    ):
        path = tmp_path / "committed" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("committed-source\n")
        if relative.startswith("infra/bootstrap/"):
            working = tmp_path / relative
            working.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, working)
    for directory in ("src", "sql", "scripts", "images", "infra"):
        shutil.copytree(
            ROOT / directory,
            tmp_path / "committed" / directory,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.tgz", ".terraform", ".build", ".env*"),
        )
    for path in (tmp_path / "committed").rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
    helper_spec = importlib.util.spec_from_file_location(
        "image_inspection_fixture", ROOT / "scripts/operations/azure/image_inspection.py"
    )
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    dockerfile = tmp_path / "committed/images/provisioner/Dockerfile"
    text = dockerfile.read_text()
    for name, digest in helper.expected_tools(tmp_path / "committed").items():
        content = ("synthetic " + name.rsplit("/", 1)[1]).encode()
        text = text.replace(digest, hashlib.sha256(content).hexdigest())
    archive = tmp_path / "kubelogin.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        entry = zipfile.ZipInfo("bin/linux_amd64/kubelogin")
        zipped.writestr(entry, b"synthetic kubelogin")
    text = text.replace(
        helper.kubelogin_spec(tmp_path / "committed")["sha256"],
        hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    dockerfile.write_text(text)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").symlink_to(sys.executable)
    (tmp_path / "bin").mkdir()
    for tool in TOOLS:
        path = tmp_path / "bin" / tool
        path.write_text(f"#!{sys.executable}\n" + FAKE)
        path.chmod(0o700)
    home = tmp_path / "home"
    (home / ".rad/bin").mkdir(parents=True)
    shutil.copyfile(tmp_path / "bin/bicep", home / ".rad/bin/bicep")
    (home / ".rad/bin/bicep").chmod(0o700)
    configure(tmp_path)
    return tmp_path


def configure(root, **changes):
    spec = {
        "project": "sample",
        "deployment": "learn",
        "subscription": SUBSCRIPTION,
        "location": "westeurope",
        "revision": REVISION,
        "resource_sizes": SIZES,
        **changes,
    }
    values = {
        "DEMO_PROJECT": spec["project"],
        "DEMO_DEPLOYMENT": spec["deployment"],
        "DEMO_ENV": spec.get("environment", "azure"),
    }
    if values["DEMO_ENV"] == "azure":
        values.update(AZURE_SUBSCRIPTION_ID=SUBSCRIPTION, AZURE_LOCATION=spec["location"])
        if spec.get("node_vm_size", NODE_SIZE) is not None:
            values["AZURE_NODE_VM_SIZE"] = spec.get("node_vm_size", NODE_SIZE)
        if spec.get("postgres_sku", POSTGRES_SIZE) is not None:
            values["AZURE_POSTGRES_SKU"] = spec.get("postgres_sku", POSTGRES_SIZE)
            values["AZURE_POSTGRES_TIER"] = "GeneralPurpose"
        if spec.get("saved_resource_sizes", True):
            values.update(
                {
                    key: str(spec["resource_sizes"][field])
                    for key, field, _ in RESOURCE_SIZING.values()
                }
            )
    values.update(spec.get("extra_env", {}))
    (root / ".env").write_text(
        "".join(f"{key}={json.dumps(value)}\n" for key, value in values.items())
    )
    (root / ".env").chmod(0o600)
    (root / "spec.json").write_text(json.dumps(spec))
    return spec


def run(root, script, *args, confirmed=True, input=None):
    result = subprocess.run(
        ["bash", str(root / f"scripts/operations/azure/{script}.sh"), *args],
        cwd=root,
        env={
            **os.environ,
            "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
            "HOME": str(root / "home"),
            "FAKE_ROOT": str(root),
            "CONFIRM_AZURE": "yes" if confirmed else "no",
            "DOCKER_CONFIG": str(root / "foreign-docker"),
            "AZURE_CONFIG_DIR": str(root / "operator-azure-cache"),
        },
        capture_output=True,
        input=input,
        text=True,
        check=False,
        timeout=60,
    )
    assert not list(root.glob(".azure-stage.*")), "private stage workspace was retained"
    assert not (root / ".state").exists()
    return result


def calls(root):
    path = root / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def selected(root, tool, prefix):
    return [
        item
        for item in calls(root)
        if item["tool"] == tool and item["args"][: len(prefix)] == prefix
    ]


def image_imports(root):
    return [
        call
        for call in selected(root, "az", ["acr", "import"])
        if call["args"][call["args"].index("--image") + 1].startswith("plane-")
    ]


def final_proof_writes(root):
    return [
        call
        for call in selected(root, "az", ["tag", "update"])
        if "=v2:" in call["args"][call["args"].index("--tags") + 1]
    ]


def candidate_runs(state):
    return {
        name: run
        for name, run in state["runs"].items()
        if any(image.get("tag", "").startswith("build-") for image in run.get("outputImages", []))
    }


def seed_verified_build(root):
    result = run(root, "build")
    assert result.returncode == 0, result.stderr
    (root / "calls.jsonl").write_text("")
    return json.loads(result.stdout)


def assert_inspection_is_read_only(root):
    allowed_azure = (
        ("deployment", "sub", "show"),
        ("account", "show"),
        ("keyvault", "list"),
        ("keyvault", "show"),
        ("network", "private-endpoint", "show"),
        ("acr", "show"),
        ("acr", "login"),
        ("acr", "repository", "list"),
        ("acr", "repository", "show-tags"),
        ("acr", "repository", "show"),
        ("acr", "manifest", "show"),
        ("acr", "task", "show-run"),
        ("role", "assignment", "list"),
        ("role", "definition", "list"),
        ("rest", "--method", "get"),
    )
    for call in calls(root):
        args = call["args"]
        if call["tool"] == "az":
            assert any(tuple(args[: len(prefix)]) == prefix for prefix in allowed_azure), args
            assert args[args.index("--subscription") + 1] == SUBSCRIPTION
            if args[:2] == ["acr", "login"]:
                assert "--expose-token" in args
        if call["tool"] == "rad":
            assert "publish" not in args
            if "publish-extension" in args:
                target = Path(args[args.index("--target") + 1])
                assert target.is_relative_to(root) and ".azure-stage." in str(target)
                assert "--force" not in args
        if call["tool"] == "docker":
            assert not any(
                word in args for word in ("build", "push", "tag", "run", "start", "exec")
            )
        assert call["tool"] not in {"kubectl", "helm"}


def test_native_policy_verification_preserves_scoped_inherited_enumeration(checkout):
    configure(checkout, mode="existing-recipes")
    result = run(checkout, "build", "--recipes-only")
    assert result.returncode == 0, result.stderr
    (query,) = selected(checkout, "az", ["role", "assignment", "list"])
    args = query["args"]
    registry = json.loads(result.stdout)["recipes"]["cluster"]["reference"].split(".azurecr.io/")[0]
    expected_scope = (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-sample-learn-azure-platform/"
        f"providers/Microsoft.ContainerRegistry/registries/{registry}"
    )
    assert args[args.index("--scope") + 1] == expected_scope
    assert "--include-inherited" in args
    assert "--all" not in args
    assert args[args.index("--fill-principal-name") + 1] == "false"
    assert args[args.index("--fill-role-definition-name") + 1] == "false"


def test_pinned_cli_rejects_scope_all_combination_in_normal_verification_path(checkout):
    helper = checkout / "scripts/operations/azure/azure.shlib"
    text = helper.read_text()
    assert "--include-inherited --fill-principal-name false" in text
    helper.write_text(
        text.replace(
            "--include-inherited --fill-principal-name false",
            "--include-inherited --all --fill-principal-name false",
        )
    )
    result = run(checkout, "build", "--recipes-only")
    assert result.returncode != 0 and not result.stdout
    assert "group or scope are not required when --all is used" in result.stderr
    assert not selected(checkout, "az", ["acr", "login"])
    assert not selected(checkout, "az", ["acr", "build"])
    assert not any(call["tool"] == "rad" and "publish" in call["args"] for call in calls(checkout))


@pytest.mark.parametrize("script", ["bootstrap", "build"])
def test_help_needs_no_configuration_or_cloud_tools(checkout, script):
    (checkout / ".env").unlink()
    result = run(checkout, script, "--help", confirmed=False)
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert not calls(checkout)


@pytest.mark.parametrize("script", ["bootstrap", "build"])
def test_mutation_requires_operator_intent(checkout, script):
    result = run(checkout, script, confirmed=False)
    assert result.returncode != 0
    assert "CONFIRM_AZURE=yes" in result.stderr
    assert not calls(checkout)


@pytest.mark.parametrize("script", ["bootstrap", "build"])
def test_local_environment_refuses_without_cloud_access(checkout, script):
    configure(checkout, environment="local")
    result = run(checkout, script)
    assert result.returncode != 0
    assert "requires DEMO_ENV=azure" in result.stderr
    assert not calls(checkout)


@pytest.mark.parametrize(
    "mode", [None, "existing-owned", "existing-owned-nodes", "owned-case-group"]
)
def test_bootstrap_uses_selected_identity_and_fresh_successful_outputs(checkout, mode):
    spec = configure(checkout, mode=mode)
    result = run(checkout, "bootstrap")
    assert result.returncode == 0, result.stderr
    foundation = json.loads(result.stdout)["foundation"]
    identity = hashlib.sha256(f"{SUBSCRIPTION}/sample/learn/azure".encode()).hexdigest()[:20]
    assert foundation["registryName"] == "acr" + identity
    assert foundation["vaultName"] == "kv-" + identity
    assert foundation["location"] == "westeurope"
    (create,) = selected(checkout, "az", ["deployment", "sub", "create"])
    assert create["template_exists"]
    providers = selected(checkout, "az", ["provider", "show"])
    assert {call["args"][call["args"].index("--namespace") + 1] for call in providers} == {
        "Microsoft.Network",
        "Microsoft.Compute",
        "Microsoft.Storage",
        "Microsoft.ContainerService",
        "Microsoft.ManagedIdentity",
        "Microsoft.ContainerRegistry",
        "Microsoft.KeyVault",
        "Microsoft.DBforPostgreSQL",
        "Microsoft.Cache",
    }
    assert all(calls(checkout).index(call) < calls(checkout).index(create) for call in providers)
    (validate,) = selected(checkout, "az", ["deployment", "sub", "validate"])
    assert calls(checkout).index(validate) < calls(checkout).index(create)
    parameters = {key: value["value"] for key, value in create["parameters"].items()}
    names = parameters.pop("applicationCredentialNames")
    credential_scope = CredentialScope(spec["project"], spec["deployment"], "azure")
    expected = {
        credential_scope.secret_name(slot, role)
        for slot, roles in {
            "management": (
                "demoKey",
                "mgmt_api",
                "mgmt_provisioner",
                "cp_shared",
                "management_admin",
            ),
            "shared-control": ("demoKey", "cp_api", "cp_reconciler", "dp_reconciler"),
            "shared-data": ("demoKey",),
        }.items()
        for role in roles
    }
    assert len(names) == 10 and set(names) == expected
    assert parameters == {
        "projectName": spec["project"],
        "deploymentName": spec["deployment"],
        "environment": "azure",
        "location": "westeurope",
        "registryName": "acr" + identity,
        "vaultName": "kv-" + identity,
        "operatorObjectId": "22222222-2222-2222-2222-222222222222",
        "operatorIp": "8.8.4.4",
        "deploymentHash": identity,
        "externalVaultResourceGroup": "",
        "registryExists": mode == "existing-owned",
        "nodeVmSize": NODE_SIZE,
        "nodeCount": 2,
        "postgresSkuName": POSTGRES_SIZE,
        "postgresSkuTier": "GeneralPurpose",
        **SIZES,
    }
    assert create["args"][create["args"].index("--name") + 1] == "sample-learn-azure-bootstrap"
    (show,) = selected(checkout, "az", ["deployment", "sub", "show"])
    assert calls(checkout).index(show) > calls(checkout).index(create)
    assert "Foundation completed" in result.stderr
    for call in calls(checkout):
        assert "synthetic-graph-token" not in json.dumps(call)
        if call["tool"] == "az":
            assert call["args"][call["args"].index("--subscription") + 1] == SUBSCRIPTION
            assert call["args"][:2] != ["account", "set"]
        if call["tool"] == "curl" and "--config" in call["args"]:
            assert call["config_mode"] == 0o600
            assert call["private_parent"] == 0o700


def test_first_bootstrap_prompts_for_all_sizes_and_passes_nondefault_choices_to_arm(checkout):
    configure(checkout, saved_resource_sizes=False, node_vm_size=None, postgres_sku=None)
    # Prompt order: nodes, AKS tier, disk, storage, Redis, gateway, registry, vault, VM, PostgreSQL.
    result = run(checkout, "bootstrap", input="3\n2\n3\n4\n6\n3\n3\n2\n2\n2\n")
    assert result.returncode == 0, result.stderr
    expected = {field: choices[-1] for _, field, choices in RESOURCE_SIZING.values()}
    (create,) = selected(checkout, "az", ["deployment", "sub", "create"])
    assert {field: create["parameters"][field]["value"] for field in expected} == expected
    assert load_config(checkout / ".env").resource_sizes == expected
    assert json.loads(result.stdout)["foundation"]["redisSkuName"] == "Balanced_B20"
    assert "3 clusters x 4 nodes" in result.stderr
    assert "not live capacity" in result.stderr
    assert "elapsed" not in result.stderr


def test_first_foundation_accepts_enter_for_all_ten_recommended_choices(checkout):
    configure(checkout, saved_resource_sizes=False, node_vm_size=None, postgres_sku=None)
    result = run(checkout, "bootstrap", input="\n" * 10)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("(recommended)") == 10
    (create,) = selected(checkout, "az", ["deployment", "sub", "create"])
    assert {field: create["parameters"][field]["value"] for field in SIZES} == SIZES
    assert create["parameters"]["nodeVmSize"]["value"] == NODE_SIZE
    assert create["parameters"]["postgresSkuName"]["value"] == POSTGRES_SIZE
    assert load_config(checkout / ".env").resource_sizes == SIZES


@pytest.mark.parametrize("stage", ["bootstrap", "build"])
def test_old_layout_stops_public_stages_before_deployment(checkout, stage):
    configure(checkout, mode="old-group-layout")
    result = run(checkout, stage)
    assert result.returncode != 0 and not result.stdout
    if stage == "bootstrap":
        for message in (
            "Old or incomplete resource-group layout for sample-learn-azure-bootstrap",
            "Existing resources are retained.",
            "Azure can retain old subscription deployment records",
            "after their resource groups are deleted.",
            "make init ENV=azure with a fresh --deployment name in ARGS",
            "then retry bootstrap.",
            'RUN_AZURE_SCENARIOS.md under "Select deployment identity"',
            "Do not bypass the layout guard.",
        ):
            assert message in result.stderr
    assert not selected(checkout, "az", ["deployment", "sub", "validate"])
    assert not selected(checkout, "az", ["deployment", "sub", "create"])
    assert not selected(checkout, "az", ["acr", "build"])
    assert not selected(checkout, "az", ["provider", "register"])


@pytest.mark.parametrize("stage", ["bootstrap", "build"])
def test_excess_radius_grant_cannot_pass_the_real_foundation_gate(checkout, stage):
    configure(checkout, mode="excess-plane-grant")
    result = run(checkout, stage)
    assert result.returncode != 0
    assert "Unexpected Radius grant" in result.stderr
    assert not selected(checkout, "az", ["acr", "build"])
    assert not selected(checkout, "rad", ["install", "kubernetes"])


def test_foundation_finishes_before_the_separate_guarded_radius_phase(checkout):
    result = run(checkout, "bootstrap")
    assert result.returncode == 0, result.stderr
    assert selected(checkout, "az", ["deployment", "sub", "show"])
    assert not selected(checkout, "az", ["aks", "get-credentials"])
    assert not any(call["tool"] == "rad" and "install" in call["args"] for call in calls(checkout))
    assert not selected(checkout, "az", ["acr", "build"])
    assert "Radius installation is a separate guarded phase" in result.stderr


@pytest.mark.parametrize("state", ["Registering", "Registered"])
def test_bootstrap_registers_new_subscription_before_service_checks(checkout, state):
    configure(checkout, mode="unregistered-providers", provider_registration_state=state)
    result = run(checkout, "bootstrap")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["foundation"]["subscriptionId"] == SUBSCRIPTION
    assert result.stderr.count("WARNING: Registering is still on-going.") == 10
    all_calls = calls(checkout)
    registrations = [
        (index, call)
        for index, call in enumerate(all_calls)
        if call["tool"] == "az" and call["args"][:2] == ["provider", "register"]
    ]
    assert len(registrations) == 10
    assert not selected(checkout, "az", ["feature", "register"])
    (feature,) = selected(checkout, "az", ["feature", "show"])
    assert feature["args"][feature["args"].index("--name") + 1] == (
        "AllowBringYourOwnPublicIpAddress"
    )
    (availability,) = selected(checkout, "az", ["acr", "check-name"])
    (validate,) = selected(checkout, "az", ["deployment", "sub", "validate"])
    (create,) = selected(checkout, "az", ["deployment", "sub", "create"])
    for index, registration in registrations:
        args = registration["args"]
        assert args[-4:] == ["--subscription", SUBSCRIPTION, "--output", "none"]
        namespace = args[args.index("--namespace") + 1]
        for query in (all_calls[index - 1], all_calls[index + 1]):
            assert query["tool"] == "az"
            assert query["args"] == [
                "provider",
                "show",
                "--namespace",
                namespace,
                "--subscription",
                SUBSCRIPTION,
                "--output",
                "json",
            ]
        assert index + 1 < all_calls.index(availability)
        assert f"{namespace}: {state}" in result.stderr
    assert all_calls.index(feature) < registrations[-1][0]
    assert all_calls.index(availability) < all_calls.index(validate) < all_calls.index(create)


def test_bootstrap_registers_public_ip_feature_before_service_checks(checkout):
    configure(checkout, feature_state="NotRegistered")
    result = run(checkout, "bootstrap")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["foundation"]["subscriptionId"] == SUBSCRIPTION
    all_calls = calls(checkout)
    feature_calls = [
        (index, call)
        for index, call in enumerate(all_calls)
        if call["tool"] == "az" and call["args"][:1] == ["feature"]
    ]
    assert [call["args"][:2] for _, call in feature_calls] == [
        ["feature", "show"],
        ["feature", "register"],
        ["feature", "show"],
    ]
    for _, call in feature_calls:
        args = call["args"]
        assert args[args.index("--namespace") + 1] == "Microsoft.Network"
        assert args[args.index("--name") + 1] == "AllowBringYourOwnPublicIpAddress"
        output = "none" if args[1] == "register" else "json"
        assert args[-4:] == ["--subscription", SUBSCRIPTION, "--output", output]
    (refresh,) = selected(checkout, "az", ["provider", "register"])
    assert refresh["args"][refresh["args"].index("--namespace") + 1] == "Microsoft.Network"
    (availability,) = selected(checkout, "az", ["acr", "check-name"])
    (validate,) = selected(checkout, "az", ["deployment", "sub", "validate"])
    (create,) = selected(checkout, "az", ["deployment", "sub", "create"])
    assert (
        feature_calls[-1][0]
        < all_calls.index(refresh)
        < all_calls.index(availability)
        < all_calls.index(validate)
        < all_calls.index(create)
    )
    assert "Microsoft.Network/AllowBringYourOwnPublicIpAddress  Registered" in result.stderr


def test_pending_public_ip_feature_blocks_actual_bootstrap_create(checkout):
    configure(checkout, feature_state="Pending")
    result = run(checkout, "bootstrap")
    assert result.returncode != 0 and not result.stdout
    assert "AllowBringYourOwnPublicIpAddress  Pending service approval" in result.stderr
    assert not selected(checkout, "az", ["feature", "register"])
    assert not selected(checkout, "az", ["provider", "register"])
    assert not selected(checkout, "az", ["acr", "check-name"])
    assert not selected(checkout, "az", ["deployment", "sub", "validate"])
    assert not selected(checkout, "az", ["deployment", "sub", "create"])


def test_bootstrap_selects_and_persists_live_node_size_before_arm_create(checkout):
    configure(checkout, node_vm_size=None)
    result = run(checkout, "bootstrap", input="2\n")
    assert result.returncode == 0, result.stderr
    assert "Select 1-3" in result.stderr
    assert "Standard_D4as_v7" in result.stderr and "Standard_E4as_v7" in result.stderr
    assert 'AZURE_NODE_VM_SIZE="Standard_D4s_v7"' in (checkout / ".env").read_text()
    (create,) = selected(checkout, "az", ["deployment", "sub", "create"])
    assert create["parameters"]["nodeVmSize"]["value"] == "Standard_D4s_v7"
    assert create["parameters"]["nodeCount"]["value"] == 2
    assert json.loads(result.stdout)["foundation"]["nodeVmSize"] == "Standard_D4s_v7"
    all_calls = calls(checkout)
    for command in (["vm", "list-skus"], ["vm", "list-usage"], ["aks", "list"]):
        (discovery,) = selected(checkout, "az", command)
        assert all_calls.index(discovery) < all_calls.index(create)
        assert discovery["args"][-4:] == ["--subscription", SUBSCRIPTION, "--output", "json"]


def test_cancelled_node_selection_does_not_create_resources_or_replace_env(checkout):
    configure(checkout, node_vm_size=None)
    before = (checkout / ".env").read_bytes()
    result = run(checkout, "bootstrap", input="q\n")
    assert result.returncode != 0 and not result.stdout
    assert (checkout / ".env").read_bytes() == before
    assert "selection cancelled" in result.stderr
    assert not selected(checkout, "az", ["deployment", "sub", "validate"])
    assert not selected(checkout, "az", ["deployment", "sub", "create"])


def test_bootstrap_selects_postgres_compute_before_arm_create(checkout):
    configure(checkout, postgres_sku=None)
    result = run(checkout, "bootstrap", input="2\n")
    assert result.returncode == 0, result.stderr
    assert "PostgreSQL compute: choose an available option" not in result.stdout
    assert "PostgreSQL compute" in result.stderr and "Select 1-3" in result.stderr
    saved = (checkout / ".env").read_text()
    assert 'AZURE_POSTGRES_SKU="Standard_D2ds_v4"' in saved
    assert 'AZURE_POSTGRES_TIER="GeneralPurpose"' in saved
    (create,) = selected(checkout, "az", ["deployment", "sub", "create"])
    assert create["parameters"]["postgresSkuName"]["value"] == "Standard_D2ds_v4"
    assert create["parameters"]["postgresSkuTier"]["value"] == "GeneralPurpose"
    assert json.loads(result.stdout)["foundation"]["postgresSkuName"] == "Standard_D2ds_v4"
    discoveries = selected(checkout, "az", ["postgres", "flexible-server", "list-skus"])
    assert len(discoveries) == 2
    for discovery in discoveries:
        assert discovery["args"][-4:] == ["--subscription", SUBSCRIPTION, "--output", "json"]
        assert calls(checkout).index(discovery) < calls(checkout).index(create)


def test_bootstrap_prompts_for_both_compute_choices_on_first_run(checkout):
    configure(checkout, node_vm_size=None, postgres_sku=None)
    result = run(checkout, "bootstrap", input="1\n3\n")
    assert result.returncode == 0, result.stderr
    (create,) = selected(checkout, "az", ["deployment", "sub", "create"])
    assert create["parameters"]["nodeVmSize"]["value"] == NODE_SIZE
    assert create["parameters"]["postgresSkuName"]["value"] == "Standard_D2ds_v5"


@pytest.mark.parametrize("answer", ["q\n", ""])
def test_cancelled_postgres_selection_does_not_submit_a_foundation(checkout, answer):
    configure(checkout, postgres_sku=None)
    before = load_config(checkout / ".env")
    result = run(checkout, "bootstrap", input=answer)
    assert result.returncode != 0 and not result.stdout
    assert load_config(checkout / ".env") == before
    assert "PostgreSQL selection cancelled" in result.stderr
    assert not selected(checkout, "az", ["deployment", "sub", "validate"])
    assert not selected(checkout, "az", ["deployment", "sub", "create"])


@pytest.mark.parametrize("capabilities", [[], {"unexpected": "shape"}])
def test_postgres_discovery_failure_prevents_foundation_creation(checkout, capabilities):
    configure(checkout, postgres_capabilities=capabilities)
    result = run(checkout, "bootstrap")
    assert result.returncode != 0 and not result.stdout
    assert not selected(checkout, "az", ["deployment", "sub", "validate"])
    assert not selected(checkout, "az", ["deployment", "sub", "create"])


def test_existing_foundation_without_postgres_selection_requires_a_fresh_demo(checkout):
    configure(checkout, mode="old-postgres-selection")
    result = run(checkout, "bootstrap")
    assert result.returncode != 0 and "fresh deployment name" in result.stderr
    assert not selected(checkout, "az", ["deployment", "sub", "create"])


def test_node_quota_failure_blocks_bootstrap_before_resource_creation(checkout):
    configure(
        checkout,
        vm_usage=[
            {"name": {"value": name}, "currentValue": 0, "limit": 0}
            for name in (
                "cores",
                "StandardDasv7Family",
                "StandardDsv7Family",
                "StandardEasv7Family",
            )
        ],
    )
    before = (checkout / ".env").read_bytes()
    result = run(checkout, "bootstrap", input="1\n")
    assert result.returncode != 0 and not result.stdout
    assert "No eligible x64 node sizes" in result.stderr
    assert (checkout / ".env").read_bytes() == before
    assert not selected(checkout, "az", ["deployment", "sub", "create"])


@pytest.mark.parametrize("command", [["vm", "list-skus"], ["vm", "list-usage"], ["aks", "list"]])
def test_node_discovery_failure_blocks_bootstrap_before_resource_creation(checkout, command):
    configure(checkout, fail=["az", *command])
    before = (checkout / ".env").read_bytes()
    result = run(checkout, "bootstrap")
    assert result.returncode != 0 and not result.stdout
    assert "synthetic native command failure" in result.stderr
    assert (checkout / ".env").read_bytes() == before
    assert not selected(checkout, "az", ["deployment", "sub", "validate"])
    assert not selected(checkout, "az", ["deployment", "sub", "create"])


@pytest.mark.parametrize("phase", ["initial", "after-registration"])
def test_malformed_provider_query_blocks_actual_bootstrap_create(checkout, phase):
    configure(checkout, mode="unregistered-providers", malformed_provider_query=phase)
    result = run(checkout, "bootstrap")
    assert result.returncode != 0 and not result.stdout
    assert "Azure registration returned invalid JSON" in result.stderr
    registrations = selected(checkout, "az", ["provider", "register"])
    assert len(registrations) == (1 if phase == "after-registration" else 0)
    assert not selected(checkout, "az", ["deployment", "sub", "validate"])
    assert not selected(checkout, "az", ["deployment", "sub", "create"])


@pytest.mark.parametrize(
    "failed",
    [
        ["az", "provider", "show"],
        ["az", "provider", "register"],
        ["az", "feature", "show"],
        ["az", "feature", "register"],
        ["az", "deployment", "sub", "validate"],
    ],
)
def test_prerequisite_failure_blocks_actual_bootstrap_create(checkout, failed):
    configure(checkout, mode="unregistered-providers", feature_state="NotRegistered", fail=failed)
    result = run(checkout, "bootstrap")
    assert result.returncode != 0 and not result.stdout
    assert "synthetic native command failure" in result.stderr
    assert not selected(checkout, "az", ["deployment", "sub", "create"])


@pytest.mark.parametrize(
    ("mode", "phase"),
    [
        ("wrong-radius-identity", "live foundation verification"),
    ],
)
def test_bootstrap_never_reports_success_when_management_radius_is_incomplete(
    checkout, mode, phase
):
    configure(checkout, mode=mode)
    result = run(checkout, "bootstrap")
    assert result.returncode != 0 and not result.stdout
    assert f"bootstrap is incomplete at {phase}" in result.stderr
    assert "Azure resources are retained" in result.stderr
    assert "Bootstrap completed" not in result.stderr
    assert selected(checkout, "az", ["deployment", "sub", "create"])
    assert not selected(checkout, "az", ["acr", "build"])
    if phase != "management Radius installation":
        assert not any(
            call["tool"] == "rad" and "install" in call["args"] for call in calls(checkout)
        )


def test_radius_fault_is_not_hidden_as_a_successful_radius_installation(checkout):
    configure(checkout, mode="radius-wi-failure")
    result = run(checkout, "bootstrap")
    assert result.returncode == 0, result.stderr
    assert not json.loads((checkout / "fake-state.json").read_text()).get("radius_installs")
    assert "Radius installation is a separate guarded phase" in result.stderr


@pytest.mark.parametrize(
    "mode",
    [
        "foreign-group",
        "foreign-resource",
        "global-foreign",
        "retained-vault",
        "foreign-deployment",
        "foreign-node-cluster",
        "foreign-case-group",
    ],
)
def test_bootstrap_refuses_foreign_or_retained_resources_before_deployment(checkout, mode):
    configure(checkout, mode=mode)
    result = run(checkout, "bootstrap")
    assert result.returncode != 0
    assert not selected(checkout, "az", ["deployment", "sub", "create"])


@pytest.mark.parametrize("mode", ["failed-create", "unready-foundation", "foreign-foundation"])
def test_bootstrap_requires_create_and_fresh_show_to_succeed(checkout, mode):
    configure(checkout, mode=mode)
    result = run(checkout, "bootstrap")
    assert result.returncode != 0
    assert not result.stdout


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "127.0.0.1",
        "169.254.2.3",
        "203.0.113.1",
        "256.1.1.1",
        "08.8.8.8",
        "8.8.8.8/32",
        "8.8.8.8\nx",
    ],
)
def test_bootstrap_never_authorizes_invalid_or_private_operator_addresses(checkout, address):
    configure(checkout, operator_ip=address)
    result = run(checkout, "bootstrap")
    assert result.returncode != 0
    assert "canonical public IPv4" in result.stderr
    assert not selected(checkout, "az", ["deployment", "sub", "create"])


@pytest.mark.parametrize("mode", [None, "existing-recipes"])
def test_recipe_publication_verifies_real_content_without_local_digest_inventory(checkout, mode):
    configure(checkout, mode=mode, extra_env={"DEMO_REVISION": REVISION})
    result = run(checkout, "build", "--recipes-only")
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["source_revision"] == REVISION
    assert set(output["recipes"]) == {"cluster", "postgresql", "gateway", "redis"}
    assert all(value["content_verified"] for value in output["recipes"].values())
    publication = [
        call for call in calls(checkout) if call["tool"] == "rad" and "publish" in call["args"]
    ]
    assert len(publication) == (0 if mode else 4)
    assert len(selected(checkout, "az", ["acr", "manifest", "show"])) == (4 if mode else 8)
    assert not selected(checkout, "az", ["acr", "repository", "update"])
    assert not selected(checkout, "az", ["acr", "build"])
    assert "private OAuth refresh credentials prepared" in result.stderr
    assert not any(call["tool"] == "docker" and "login" in call["args"] for call in calls(checkout))
    if not mode:
        assert "VISIBLE PUBLISH" in result.stderr
        assert all(
            "br:" in call["args"][call["args"].index("--target") + 1]
            and "/radius-recipe-staging/" in call["args"][call["args"].index("--target") + 1]
            for call in publication
        )
        imports = selected(checkout, "az", ["acr", "import"])
        assert len(imports) == 4
        for call in imports:
            assert call["args"][call["args"].index("--source") + 1].startswith(
                "radius-recipe-staging/"
            )
            assert call["args"][call["args"].index("--image") + 1].startswith("radius-recipes/")
            assert "--force" not in call["args"]
    assert not (checkout / "foreign-docker").exists()
    for call in calls(checkout):
        assert "synthetic-registry-token" not in json.dumps(call)
        assert "synthetic-pull-token" not in json.dumps(call)
        if call["tool"] == "az":
            assert call["args"][call["args"].index("--subscription") + 1] == SUBSCRIPTION
        if call["tool"] == "rad" and "publish" in call["args"]:
            assert ".azure-stage." in call["home"]
            assert ".azure-stage." in call["docker_config"]
            assert "--config" in call["args"]
        if call["tool"] == "curl":
            assert call["config_mode"] == 0o600
            assert "--proto" in call["args"]


@pytest.mark.parametrize("mode", ["wrong-recipe-content", "wrong-blob-digest"])
def test_existing_recipe_is_not_adopted_or_locked_on_content_mismatch(checkout, mode):
    configure(checkout, mode=mode)
    result = run(checkout, "build", "--recipes-only")
    assert result.returncode != 0
    assert "differs" in result.stderr
    assert not selected(checkout, "az", ["acr", "repository", "update"])
    assert not any(call["tool"] == "rad" and "publish" in call["args"] for call in calls(checkout))


@pytest.mark.parametrize(
    "mode", ["foreign-foundation", "foreign-registry-host", "foreign-registry"]
)
def test_artifacts_refuse_wrong_foundation_or_registry_owner(checkout, mode):
    configure(checkout, mode=mode)
    result = run(checkout, "build")
    assert result.returncode != 0
    assert not selected(checkout, "az", ["acr", "build"])
    assert not selected(checkout, "az", ["acr", "login"])


@pytest.mark.parametrize(
    ("script", "prefix"),
    [
        ("bootstrap", ["az", "account", "show"]),
        ("bootstrap", ["az", "group", "list"]),
        ("build", ["az", "acr", "repository", "list"]),
        ("build", ["az", "acr", "manifest", "show"]),
    ],
)
def test_native_errors_never_become_missing_resources_or_success(checkout, script, prefix):
    configure(checkout, fail=prefix)
    result = run(checkout, script)
    assert result.returncode != 0
    assert "synthetic native command failure" in result.stderr
    assert not selected(checkout, "az", ["deployment", "sub", "create"])
    assert not selected(checkout, "az", ["acr", "build"])


def test_images_build_selected_commit_and_inspect_before_worker_base_or_promotion(checkout):
    configure(checkout, extra_env={"DEMO_REVISION": REVISION})
    result = run(checkout, "build")
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["source_revision"] == REVISION
    assert output["content_verified"] is True
    assert output["status"] == "artifacts_verified"
    builds = selected(checkout, "az", ["acr", "build"])
    assert len(builds) == 2
    assert result.stderr.count("VISIBLE BUILD") == 2
    for component, build in zip(("api", "provisioner"), builds, strict=True):
        assert build["args"][build["args"].index("--image") + 1].startswith(
            f"plane-{component}:build-{REVISION}-"
        )
        assert build["args"][build["args"].index("--platform") + 1] == "linux/amd64"
        assert f"SOURCE_REVISION={REVISION}" in build["args"]
        assert build["args"][build["args"].index("--source-acr-auth-id") + 1] == "[caller]"
        assert "--no-wait" not in build["args"] and "--no-logs" not in build["args"]
        assert build["args"][build["args"].index("--output") + 1] == "none"
        assert build["source_value"] == "committed-source\n"
        assert build["env_in_context"] is False
        assert build["source_mode"] == 0o644
        assert build["source_directory_mode"] == 0o755
        assert build["extension_mode"] == 0o644
        assert build["workspace_mode"] == 0o700
        assert ".azure-stage." in build["cwd"]
        assert f"/plane-{component}@sha256:" in output["images"][component]
    assert f"API_IMAGE={output['images']['api']}" in builds[1]["args"]
    assert "TARGETARCH=amd64" in builds[1]["args"]
    assert not any(
        call["tool"] == "docker" and any(word in call["args"] for word in ("run", "start", "exec"))
        for call in calls(checkout)
    )
    removals = [
        call for call in calls(checkout) if call["tool"] == "docker" and "rm" in call["args"]
    ]
    assert len(removals) == 2
    assert len(selected(checkout, "az", ["acr", "task", "list-runs"])) == 4
    assert not selected(checkout, "az", ["acr", "task", "logs"])
    assert len(selected(checkout, "az", ["acr", "run"])) == 2
    assert all(call["remote_verification"] for call in calls(checkout) if call["tool"] == "docker")
    assert calls(checkout).index(removals[0]) < calls(checkout).index(builds[1])
    imports = image_imports(checkout)
    assert len(imports) == 2 and all("--force" not in call["args"] for call in imports)
    assert calls(checkout).index(removals[0]) < calls(checkout).index(imports[0])
    assert all(proof["content_verified"] for proof in output["inspections"].values())
    assert not json.loads((checkout / "fake-state.json").read_text())["containers"]


def test_read_only_inspect_needs_no_confirmation_and_returns_complete_descriptors(checkout):
    seed_verified_build(checkout)
    result = run(checkout, "build", "--inspect", confirmed=False)
    assert result.returncode == 0, result.stderr
    descriptor = json.loads(result.stdout)
    assert set(descriptor) == {
        "source_revision",
        "recipes",
        "images",
        "inspections",
        "content_verified",
        "status",
    }
    assert descriptor["source_revision"] == REVISION
    assert descriptor["content_verified"] is True
    assert descriptor["status"] == "artifacts_verified"
    assert set(descriptor["recipes"]) == {"cluster", "postgresql", "gateway", "redis"}
    assert set(descriptor["images"]) == set(descriptor["inspections"]) == {"api", "provisioner"}
    assert all(value["content_verified"] for value in descriptor["inspections"].values())
    assert all(value["content_verified"] for value in descriptor["recipes"].values())
    assert (
        "app/scripts/operations/install-radius.sh"
        in (descriptor["inspections"]["provisioner"]["source_hashes"])
    )
    assert_inspection_is_read_only(checkout)
    assert not selected(checkout, "az", ["acr", "run"])
    assert not any(call["tool"] == "docker" for call in calls(checkout))
    (archive,) = selected(checkout, "git", ["-C", str(checkout), "archive"])
    assert REVISION in archive["args"]
    assert not json.loads((checkout / "fake-state.json").read_text())["containers"]


def test_inspect_returns_the_same_verified_descriptor_as_build(checkout):
    built = run(checkout, "build")
    assert built.returncode == 0, built.stderr
    (checkout / "calls.jsonl").write_text("")
    inspected = run(checkout, "build", "--inspect", confirmed=False)
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout) == json.loads(built.stdout)
    assert_inspection_is_read_only(checkout)


@pytest.mark.parametrize("fault", ["missing", "pending", "corrupt"])
def test_read_only_inspection_refuses_missing_or_corrupt_remote_evidence(checkout, fault):
    seed_verified_build(checkout)
    if fault in {"missing", "pending"}:
        state_path = checkout / "fake-state.json"
        state = json.loads(state_path.read_text())
        key = "plane-demo-check-api-" + REVISION
        previous = state["arm_tags"].pop(key)
        if fault == "pending":
            state["arm_tags"][key] = "pending-v1:" + previous.split(":")[1]
        state_path.write_text(json.dumps(state))
    else:
        configure(checkout, mode="remote-report-corrupt")
    result = run(checkout, "build", "--inspect", confirmed=False)
    assert result.returncode != 0 and not result.stdout
    assert not selected(checkout, "az", ["acr", "run"])
    assert not selected(checkout, "az", ["tag", "update"])
    assert not any(call["tool"] == "docker" for call in calls(checkout))


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("missing_recipe", "cluster"),
        ("missing_recipe", "redis"),
        ("missing_image", "api"),
        ("missing_image", "provisioner"),
        ("unlocked", "plane-api"),
        ("mode", "wrong-recipe-content"),
        ("mode", "remote-report-corrupt"),
        ("mode", "old-certificate-names"),
        ("mode", "pending-vault-endpoint"),
    ],
)
def test_inspect_refuses_incomplete_or_changed_prerequisites_without_repair(
    checkout, option, value
):
    seed_verified_build(checkout)
    state = json.loads((checkout / "fake-state.json").read_text())
    if option in {"missing_recipe", "missing_image", "unlocked"}:
        repository = (
            f"radius-recipes/{value}"
            if option == "missing_recipe"
            else f"plane-{value}"
            if option == "missing_image"
            else value
        )
        for key in list(state["artifacts"]):
            if key.startswith(repository + ":") and (
                ":src-" in key or key.endswith(":" + REVISION)
            ):
                if option == "unlocked":
                    state["artifacts"][key]["locked"] = False
                else:
                    del state["artifacts"][key]
    if option == "mode" and value == "wrong-recipe-content":
        for key in list(state["artifacts"]):
            if key.startswith("radius-recipes/cluster:src-"):
                del state["artifacts"][key]
    (checkout / "fake-state.json").write_text(json.dumps(state))
    configure(
        checkout,
        existing_recipes=True,
        locked_artifacts=True,
        **{option: value},
    )
    result = run(checkout, "build", "--inspect", confirmed=False)
    assert result.returncode != 0
    assert not result.stdout
    assert_inspection_is_read_only(checkout)
    if (checkout / "fake-state.json").exists():
        assert not json.loads((checkout / "fake-state.json").read_text()).get("containers")


def test_existing_images_are_exported_and_verified_without_rebuilding(checkout):
    seed_verified_build(checkout)
    result = run(checkout, "build")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "artifacts_verified"
    assert not selected(checkout, "az", ["acr", "build"])
    assert not selected(checkout, "az", ["acr", "import"])
    assert (
        len(
            [
                call
                for call in calls(checkout)
                if call["tool"] == "docker" and "export" in call["args"]
            ]
        )
        == 2
    )


@pytest.mark.parametrize(
    "mode", ["changed-api", "extra-api-private", "missing-private", "export-failure"]
)
def test_image_failure_cleans_owned_containers_and_never_claims_success(checkout, mode):
    seed_verified_build(checkout)
    configure(checkout, mode=mode)
    result = run(checkout, "build")
    assert result.returncode != 0
    assert not result.stdout
    assert not selected(checkout, "az", ["acr", "build"])
    assert not selected(checkout, "az", ["acr", "import"])
    assert not json.loads((checkout / "fake-state.json").read_text())["containers"]


def test_new_api_inspection_failure_prevents_promotion_and_worker_build(checkout):
    configure(checkout, mode="changed-api")
    result = run(checkout, "build")
    assert result.returncode != 0 and not result.stdout
    assert len(selected(checkout, "az", ["acr", "build"])) == 1
    assert not image_imports(checkout)
    assert not json.loads((checkout / "fake-state.json").read_text())["containers"]


@pytest.mark.parametrize("mode", ["unreadable-source", "untraversable-source"])
def test_runtime_uid_cannot_accept_inaccessible_image_source(checkout, mode):
    configure(checkout, mode=mode)
    result = run(checkout, "build")
    assert result.returncode != 0 and not result.stdout
    assert "image_runtime_" in result.stderr
    assert len(selected(checkout, "az", ["acr", "build"])) == 1
    assert not image_imports(checkout)
    assert not final_proof_writes(checkout)
    assert not json.loads((checkout / "fake-state.json").read_text())["containers"]


def test_original_umask_extraction_failure_is_rejected_by_actual_image_gate(checkout):
    script = checkout / "scripts/operations/azure/build.sh"
    text = script.read_text()
    assert 'tar -xpf "$AZURE_WORKSPACE/source.tar"' in text
    script.write_text(
        text.replace(
            'tar -xpf "$AZURE_WORKSPACE/source.tar"', 'tar -xf "$AZURE_WORKSPACE/source.tar"'
        )
    )
    result = run(checkout, "build")
    assert result.returncode != 0 and not result.stdout
    (build,) = selected(checkout, "az", ["acr", "build"])
    assert build["source_mode"] == 0o600
    assert build["source_directory_mode"] == 0o700
    assert "image_runtime_" in result.stderr
    assert not image_imports(checkout)
    assert not final_proof_writes(checkout)


def test_foreign_container_is_neither_exported_nor_removed(checkout):
    configure(checkout, mode="foreign-container")
    result = run(checkout, "build")
    assert result.returncode != 0
    assert "remote_inspection_container_owner_mismatch" in result.stderr
    for command in ("export", "rm"):
        assert not any(
            call["tool"] == "docker" and command in call["args"] for call in calls(checkout)
        )


def test_canonical_tag_race_never_uses_force_or_builds_worker(checkout):
    configure(checkout, mode="import-conflict")
    result = run(checkout, "build")
    assert result.returncode != 0 and not result.stdout
    assert len(selected(checkout, "az", ["acr", "build"])) == 1
    assert all("--force" not in call["args"] for call in calls(checkout))
    assert not json.loads((checkout / "fake-state.json").read_text())["containers"]


@pytest.mark.parametrize("inspect_only", [False, True])
def test_preexisting_images_without_arm_provenance_are_never_attested(checkout, inspect_only):
    configure(checkout, existing_images=True, existing_recipes=True, locked_artifacts=True)
    result = run(
        checkout, "build", *(["--inspect"] if inspect_only else []), confirmed=not inspect_only
    )
    assert result.returncode != 0
    assert "arm_build_provenance_missing_or_mismatched" in result.stderr
    assert not selected(checkout, "az", ["acr", "build"])
    assert not selected(checkout, "az", ["tag", "update"])
    assert not any(call["tool"] == "docker" and "pull" in call["args"] for call in calls(checkout))


@pytest.mark.parametrize(
    "mode",
    [
        "poison-pyc",
        "wrong-kubelogin",
        "missing-kubelogin",
        "nonexecutable-kubelogin",
    ],
)
def test_remote_reinspection_rejects_poisoned_executable_candidates(checkout, mode):
    seed_verified_build(checkout)
    verifier = checkout / "scripts/operations/azure/remote_inspection.py"
    verifier.write_text(verifier.read_text() + "\n")
    configure(checkout, mode=mode)
    result = run(checkout, "build")
    assert result.returncode != 0 and not result.stdout
    assert not selected(checkout, "az", ["acr", "build"])
    assert not final_proof_writes(checkout)
    assert all(call["remote_verification"] for call in calls(checkout) if call["tool"] == "docker")
    assert not json.loads((checkout / "fake-state.json").read_text())["containers"]


def test_remote_evidence_cannot_authorize_a_different_canonical_image(checkout):
    seed_verified_build(checkout)
    state_path = checkout / "fake-state.json"
    state = json.loads(state_path.read_text())
    state["artifacts"]["plane-api:" + REVISION]["digest"] = "sha256:" + "f" * 64
    state_path.write_text(json.dumps(state))
    result = run(checkout, "build", "--inspect", confirmed=False)
    assert result.returncode != 0
    assert "arm_build_provenance_missing_or_mismatched" in result.stderr
    assert not selected(checkout, "az", ["acr", "run"])
    assert not selected(checkout, "az", ["tag", "update"])


def test_fresh_digest_is_taken_from_authenticated_run_not_staging_tag(checkout):
    configure(checkout, mode="staging-race")
    result = run(checkout, "build")
    assert result.returncode == 0, result.stderr
    descriptor = json.loads(result.stdout)
    state = json.loads((checkout / "fake-state.json").read_text())
    for component, build_run in zip(
        ("api", "provisioner"), candidate_runs(state).values(), strict=True
    ):
        assert descriptor["images"][component].endswith(build_run["outputImages"][0]["digest"])
    shows = selected(checkout, "az", ["acr", "repository", "show"])
    assert not any(
        call["args"][call["args"].index("--image") + 1].startswith(
            ("plane-api:build-", "plane-provisioner:build-")
        )
        for call in shows
    )
    assert len(final_proof_writes(checkout)) == 2


def test_failed_run_cannot_create_arm_proof_or_worker_base(checkout):
    configure(checkout, mode="failed-run")
    result = run(checkout, "build")
    assert result.returncode != 0
    assert not final_proof_writes(checkout)
    assert not image_imports(checkout)
    assert len(selected(checkout, "az", ["acr", "build"])) == 1


@pytest.mark.parametrize("failure", ["lost-build-result", "lock-command-failure"])
def test_build_retry_resumes_the_same_api_run_after_interruption(checkout, failure):
    configure(
        checkout,
        mode=failure,
        fail=["az", "acr", "repository", "update"] if failure == "lock-command-failure" else None,
    )
    first = run(checkout, "build")
    assert first.returncode != 0
    initial = json.loads((checkout / "fake-state.json").read_text())
    assert len(candidate_runs(initial)) == 1
    (api_run,) = candidate_runs(initial).values()
    assert any(value.startswith("pending-v1:") for value in initial["arm_tags"].values())
    configure(checkout)
    (checkout / "calls.jsonl").write_text("")
    resumed = run(checkout, "build")
    assert resumed.returncode == 0, resumed.stderr
    builds = selected(checkout, "az", ["acr", "build"])
    assert (
        len(builds) == 1
        and "provisioner" in builds[0]["args"][builds[0]["args"].index("--image") + 1]
    )
    state = json.loads((checkout / "fake-state.json").read_text())
    assert len(candidate_runs(state)) == 2
    assert json.loads(resumed.stdout)["images"]["api"].endswith(
        api_run["outputImages"][0]["digest"]
    )
    assert all(
        value.startswith("v2:")
        for key, value in state["arm_tags"].items()
        if key.startswith("plane-demo-proof-")
    )
    (checkout / "calls.jsonl").write_text("")
    repeated = run(checkout, "build")
    assert repeated.returncode == 0, repeated.stderr
    assert not selected(checkout, "az", ["acr", "build"])
    assert not selected(checkout, "az", ["acr", "run"])
    assert not image_imports(checkout) and not selected(checkout, "az", ["tag", "update"])


def test_ambiguous_submission_is_not_queued_again_on_retry(checkout):
    configure(checkout, mode="missing-build-run")
    assert run(checkout, "build").returncode != 0
    before = json.loads((checkout / "fake-state.json").read_text())
    (checkout / "calls.jsonl").write_text("")
    retried = run(checkout, "build")
    assert retried.returncode != 0 and "No second build was queued" in retried.stderr
    assert not selected(checkout, "az", ["acr", "build"])
    assert json.loads((checkout / "fake-state.json").read_text())["runs"] == before["runs"]


def test_explicit_recovery_verifies_an_unrecorded_api_run_without_rebuilding(checkout):
    previous = seed_verified_build(checkout)
    state_path = checkout / "fake-state.json"
    state = json.loads(state_path.read_text())
    key = "plane-demo-proof-api-" + REVISION
    run_id = state["arm_tags"].pop(key).split(":")[1]
    state["artifacts"].pop("plane-api:" + REVISION)
    state_path.write_text(json.dumps(state))
    recovered = run(checkout, "build", "--recover-build", f"api={run_id}")
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["images"] == previous["images"]
    assert not selected(checkout, "az", ["acr", "build"])
    assert len(image_imports(checkout)) == 1
    assert any(
        "=recovery-v1:" in call["args"][call["args"].index("--tags") + 1]
        for call in selected(checkout, "az", ["tag", "update"])
    )


def test_explicit_recovery_rejects_different_build_instructions(checkout):
    seed_verified_build(checkout)
    state_path = checkout / "fake-state.json"
    state = json.loads(state_path.read_text())
    run_id = state["arm_tags"].pop("plane-demo-proof-api-" + REVISION).split(":")[1]
    state["artifacts"].pop("plane-api:" + REVISION)
    state["run_logs"][run_id] += "\nStep 21/21 : RUN replace-python\n"
    state_path.write_text(json.dumps(state))
    result = run(checkout, "build", "--recover-build", f"api={run_id}")
    assert result.returncode != 0 and "recovery_build_instructions_mismatch" in result.stderr
    assert not selected(checkout, "az", ["acr", "build"])
    assert not selected(checkout, "az", ["tag", "update"]) and not image_imports(checkout)


def test_recipe_canonical_race_is_no_force_and_never_overwrites(checkout):
    configure(checkout, mode="recipe-import-conflict")
    result = run(checkout, "build", "--recipes-only")
    assert result.returncode != 0 and not result.stdout
    publications = [
        call for call in calls(checkout) if call["tool"] == "rad" and "publish" in call["args"]
    ]
    assert len(publications) == 1
    assert ":publish-" in publications[0]["args"][publications[0]["args"].index("--target") + 1]
    imports = selected(checkout, "az", ["acr", "import"])
    assert len(imports) == 1 and "--force" not in imports[0]["args"]
    assert not selected(checkout, "az", ["acr", "repository", "update"])


def test_bootstrap_rerun_preserves_arm_owned_proofs_and_unrelated_tags(checkout):
    seed_verified_build(checkout)
    state_path = checkout / "fake-state.json"
    state = json.loads(state_path.read_text())
    state["arm_tags"]["unrelated"] = "preserve-me"
    state_path.write_text(json.dumps(state))
    configure(checkout, mode="existing-owned")
    result = run(checkout, "bootstrap")
    assert result.returncode == 0, result.stderr
    (create,) = selected(checkout, "az", ["deployment", "sub", "create"])
    assert create["parameters"]["registryExists"]["value"] is True
    assert json.loads(state_path.read_text())["arm_tags"] == state["arm_tags"]
    assert not selected(checkout, "az", ["tag", "update"])


@pytest.mark.parametrize("script", ["bootstrap", "build"])
@pytest.mark.parametrize("mode", [None, "external-case"])
def test_selected_external_vault_keeps_ownership_and_configuration(checkout, script, mode):
    configure(checkout, mode=mode, extra_env={"DEMO_KEY_VAULT": "shared-vault"})
    result = run(checkout, script, *(["--recipes-only"] if script == "build" else []))
    assert result.returncode == 0, result.stderr
    if script == "bootstrap":
        foundation = json.loads(result.stdout)["foundation"]
        assert foundation["vaultOwned"] is False
        assert foundation["vaultResourceGroup"] == "shared-secrets"
        assert "/resourceGroups/shared-secrets/" in foundation["vaultId"]
        (create,) = selected(checkout, "az", ["deployment", "sub", "create"])
        assert create["parameters"]["externalVaultResourceGroup"]["value"] == "shared-secrets"
        assert create["parameters"]["vaultName"]["value"] == "shared-vault"
    for call in selected(checkout, "az", ["keyvault"]):
        assert call["args"][1] in {"list", "show"}
    assert not selected(checkout, "az", ["rest"])


@pytest.mark.parametrize(
    "mode",
    [
        "missing-external-vault",
        "external-public",
        "external-tenant",
        "external-access-policy",
        "external-no-bypass",
    ],
)
def test_external_vault_validation_fails_without_changing_any_resource(checkout, mode):
    configure(checkout, mode=mode, extra_env={"DEMO_KEY_VAULT": "shared-vault"})
    result = run(checkout, "bootstrap")
    assert result.returncode != 0
    assert not selected(checkout, "az", ["deployment", "sub", "create"])
    assert not selected(checkout, "az", ["keyvault", "update"])


@pytest.mark.parametrize(
    "group", ["rg-sample-learn-azure-management-app", "RG-SAMPLE-LEARN-AZURE-MANAGEMENT-APP"]
)
def test_external_vault_cannot_be_in_demo_owned_cleanup_groups(checkout, group):
    configure(
        checkout,
        extra_env={"DEMO_KEY_VAULT": "shared-vault"},
        external_group=group,
    )
    result = run(checkout, "bootstrap")
    assert result.returncode != 0
    assert "outside" in result.stderr
    assert not selected(checkout, "az", ["deployment", "sub", "create"])


def test_pending_private_endpoint_approval_is_not_reported_as_ready(checkout):
    configure(checkout, mode="pending-vault-endpoint", extra_env={"DEMO_KEY_VAULT": "shared-vault"})
    result = run(checkout, "bootstrap")
    assert result.returncode != 0 and not result.stdout
    assert "owner approval" in result.stderr


def test_lock_mismatch_cannot_claim_verified_artifacts(checkout):
    configure(checkout, mode="changed-lock")
    result = run(checkout, "build")
    assert result.returncode != 0
    assert "lock did not match" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("stage", ["bootstrap", "build", "inspect"])
def test_legacy_registry_modes_are_refused_without_live_migration(checkout, stage):
    configure(checkout, mode="legacy-registry")
    result = run(
        checkout,
        "build" if stage == "inspect" else stage,
        *(["--inspect"] if stage == "inspect" else []),
        confirmed=stage != "inspect",
    )
    assert result.returncode != 0 and not result.stdout
    assert "legacy-mode migration is not implicit" in result.stderr
    assert not selected(checkout, "az", ["deployment", "sub", "create"])
    assert not selected(checkout, "az", ["acr", "build"])
    assert not selected(checkout, "az", ["acr", "update"])


@pytest.mark.parametrize("inspect_only", [False, True])
def test_unrestricted_effective_repository_writer_blocks_artifact_consumption(
    checkout, inspect_only
):
    configure(checkout, mode="unrestricted-writer")
    result = run(
        checkout, "build", *(["--inspect"] if inspect_only else []), confirmed=not inspect_only
    )
    assert result.returncode != 0
    assert "canonical_recipe_writer_not_isolated" in result.stderr
    assert not selected(checkout, "az", ["acr", "build"])
    assert not selected(checkout, "az", ["acr", "login"])


def test_canonical_recipe_safety_does_not_depend_on_publisher_tag_locks(checkout):
    configure(checkout, mode="existing-recipes", locked_artifacts=False)
    result = run(checkout, "build", "--recipes-only")
    assert result.returncode == 0, result.stderr
    assert all(
        value["immutability"] == "acr-abac-arm-import-v1"
        for value in json.loads(result.stdout)["recipes"].values()
    )
    assert not selected(checkout, "az", ["acr", "repository", "update"])


def test_revision_mismatch_cannot_reach_build_or_publication(checkout):
    configure(checkout, mode="wrong-revision", extra_env={"DEMO_REVISION": REVISION})
    result = run(checkout, "build")
    assert result.returncode != 0
    assert "different commit" in result.stderr
    assert not selected(checkout, "az", ["acr", "build"])


@pytest.mark.parametrize("script", ["bootstrap", "build"])
def test_compiler_version_is_pinned_before_cloud_queries(checkout, script):
    configure(checkout, mode="wrong-bicep")
    result = run(checkout, script)
    assert result.returncode != 0
    assert "0.42.1" in result.stderr
    assert not selected(checkout, "az", [])


@pytest.fixture(scope="module")
def compiled_bootstrap():
    compiler = Path.home() / ".rad/bin/bicep"
    if not compiler.is_file():
        pytest.skip("Radius bundled Bicep is not installed")
    result = subprocess.run(
        [str(compiler), "build", str(ROOT / "infra/bootstrap/azure.bicep"), "--stdout"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_compiled_bootstrap_identity_has_no_fixed_project_or_random_salt(compiled_bootstrap):
    parameters = compiled_bootstrap["parameters"]
    for name in ("projectName", "deploymentName", "location", "registryName", "vaultName"):
        assert "defaultValue" not in parameters[name]
    assert "nameSalt" not in parameters
    assert parameters["environment"]["allowedValues"] == ["azure"]
    assert "deploymentName" in compiled_bootstrap["variables"]["prefix"]
    tags = compiled_bootstrap["variables"]["requiredTags"]
    assert "parameters('projectName')" in tags
    assert "parameters('deploymentName')" in tags
    assert "parameters('environment')" in tags
    for parameter in (
        "coordinatorServiceAccountSubject",
        "certificateIssuerServiceAccountSubject",
        "harnessServiceAccountSubject",
    ):
        assert "deploymentName" in parameters[parameter]["defaultValue"]
        assert "radplanes" not in parameters[parameter]["defaultValue"]


def test_compiled_network_preserves_private_vault_and_explicit_global_names(compiled_bootstrap):
    network = next(item for item in compiled_bootstrap["resources"] if item["name"] == "network")
    template = network["properties"]["template"]
    registry = next(
        item
        for item in template["resources"]
        if item["type"] == "Microsoft.ContainerRegistry/registries"
    )
    vault = next(
        item for item in template["resources"] if item["type"] == "Microsoft.KeyVault/vaults"
    )
    assert registry["name"] == "[parameters('registryName')]"
    assert registry["condition"] == "[not(parameters('registryExists'))]"
    assert registry["apiVersion"] == "2025-11-01"
    assert registry["properties"]["roleAssignmentMode"] == "AbacRepositoryPermissions"
    assert registry["properties"]["adminUserEnabled"] is False
    assert registry["properties"]["anonymousPullEnabled"] is False
    assert vault["name"] == "[parameters('vaultName')]"
    assert vault["properties"]["publicNetworkAccess"] == "Disabled"
    assert vault["properties"]["enablePurgeProtection"] is True
    assert vault["properties"]["enableRbacAuthorization"] is True
    assert vault["condition"] == "[empty(parameters('externalVaultResourceGroup'))]"
    assert "externalVaultResourceGroup" in template["outputs"]["foundation"]["value"]["vaultOwned"]
    allocation = template["outputs"]["allocations"]["copy"]["input"]
    assert allocation["certificateName"] == (
        "[format('gateway-{0}-{1}', parameters('prefix'), parameters('slots')[copyIndex()])]"
    )
    assert allocation["acmeStateSecretName"] == (
        "[format('acme-{0}-{1}', parameters('prefix'), parameters('slots')[copyIndex()])]"
    )
    assert "deploymentHash" not in template["parameters"]
    assert "namespace" in compiled_bootstrap["outputs"]["allocations"]["copy"]["input"]


def test_compiled_foundation_forwards_explicit_sizing_into_resources(compiled_bootstrap):
    for _, field, _ in RESOURCE_SIZING.values():
        if field != "nodeCount":
            assert "defaultValue" not in compiled_bootstrap["parameters"][field]
        assert f"parameters('{field}')" in compiled_bootstrap["outputs"]["foundation"]["value"]
    modules = {item["name"]: item for item in compiled_bootstrap["resources"]}
    network = modules["network"]["properties"]
    resources = {item["type"]: item for item in network["template"]["resources"]}
    for field, resource, property_name in (
        ("registrySkuName", "Microsoft.ContainerRegistry/registries", "sku"),
        ("vaultSkuName", "Microsoft.KeyVault/vaults", "properties"),
    ):
        assert network["parameters"][field]["value"] == f"[parameters('{field}')]"
        value = resources[resource][property_name]
        if property_name == "properties":
            value = value["sku"]
        assert value["name"] == f"[parameters('{field}')]"
    management = modules["management-cluster"]["properties"]
    cluster = next(
        item
        for item in management["template"]["resources"]
        if item["type"] == "Microsoft.ContainerService/managedClusters"
    )
    for field in ("aksTier", "nodeCount", "nodeOsDiskSizeGb"):
        assert management["parameters"][field]["value"] == f"[parameters('{field}')]"
    assert cluster["sku"]["tier"] == "[parameters('aksTier')]"
    pool = cluster["properties"]["agentPoolProfiles"][0]
    assert pool["count"] == "[parameters('nodeCount')]"
    assert pool["osDiskSizeGB"] == "[parameters('nodeOsDiskSizeGb')]"


def test_credential_grants_reuse_only_exact_secret_get_set_capability(compiled_bootstrap):
    role = next(
        resource
        for resource in compiled_bootstrap["resources"]
        if resource["type"] == "Microsoft.Authorization/roleDefinitions"
        and "acme-state-writer" in resource["name"]
    )
    assert role["properties"]["permissions"] == [
        {
            "actions": [],
            "notActions": [],
            "notDataActions": [],
            "dataActions": [
                "Microsoft.KeyVault/vaults/secrets/readMetadata/action",
                "Microsoft.KeyVault/vaults/secrets/getSecret/action",
                "Microsoft.KeyVault/vaults/secrets/setSecret/action",
            ],
        }
    ]
    assert "[variables('selectedVaultId')]" in role["properties"]["assignableScopes"]
    grant = next(
        resource
        for resource in compiled_bootstrap["resources"]
        if resource.get("copy", {}).get("name") == "applicationCredentials"
    )
    assert grant["resourceGroup"] == "[variables('vaultResourceGroup')]"
    params = grant["properties"]["parameters"]
    assert "/secrets/" in params["objectScope"]["value"]
    assert "applicationCredentialNames" in params["objectScope"]["value"]
    assert "coordinator-identity" in params["principalId"]["value"]
    assert "acme-state-writer" in params["roleDefinitionId"]["value"]
    assert "deploymentHash" in grant["name"]


@pytest.mark.parametrize("recipe", ["cluster", "postgresql", "redis", "gateway"])
def test_azure_recipes_preserve_selected_project_tags(recipe):
    compiler = Path.home() / ".rad/bin/bicep"
    if not compiler.is_file():
        pytest.skip("Radius bundled Bicep is not installed")
    result = subprocess.run(
        [
            str(compiler),
            "build",
            str(ROOT / f"infra/radius/recipes/azure/{recipe}.bicep"),
            "--stdout",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    template = json.loads(result.stdout)
    tags = template["variables"]["requiredTags"]
    assert "parameters('tags')" in tags and "'project'" not in tags
    assert "'SecurityControl', 'Ignore'" in tags
    assert "'managedBy', 'radius-todolist-app'" in tags
    resources = template["resources"]
    resources = resources.values() if isinstance(resources, dict) else resources
    resources = {item["type"]: item for item in resources}
    if recipe == "cluster":
        cluster = resources["Microsoft.ContainerService/managedClusters"]
        assert cluster["sku"]["tier"] == "[parameters('aksTier')]"
        pool = cluster["properties"]["agentPoolProfiles"][0]
        assert pool["count"] == "[parameters('nodeCount')]"
        assert pool["osDiskSizeGB"] == "[parameters('nodeOsDiskSizeGb')]"
    elif recipe == "postgresql":
        storage = resources["Microsoft.DBforPostgreSQL/flexibleServers"]["properties"]["storage"]
        assert storage["storageSizeGB"] == "[parameters('storageSizeGb')]"
    elif recipe == "redis":
        assert "defaultValue" not in template["parameters"]["skuName"]
        assert resources["Microsoft.Cache/redisEnterprise"]["sku"]["name"] == (
            "[parameters('skuName')]"
        )
    else:
        assert (
            resources["Microsoft.Network/applicationGateways"]["properties"]["sku"]["capacity"]
            == "[parameters('capacity')]"
        )
