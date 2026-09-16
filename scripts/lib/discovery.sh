#!/usr/bin/env bash

demo_slot() {
  case "$1" in
    management) DEMO_ROLE=management; DEMO_SLOT_INDEX=0 ;;
    shared-control) DEMO_ROLE=control; DEMO_SLOT_INDEX=1 ;;
    shared-data) DEMO_ROLE=data; DEMO_SLOT_INDEX=2 ;;
    isolated-1-control) DEMO_ROLE=control; DEMO_SLOT_INDEX=3 ;;
    isolated-1-data) DEMO_ROLE=data; DEMO_SLOT_INDEX=4 ;;
    *) demo_error 'Unknown plane slot'; return 1 ;;
  esac
  DEMO_SLOT="$1"
  DEMO_STEM="$DEMO_PROJECT-$DEMO_DEPLOYMENT-$DEMO_ENV"
  DEMO_CONTEXT="$DEMO_STEM-$DEMO_SLOT"
  DEMO_NAMESPACE="$DEMO_CONTEXT-$DEMO_ROLE"
}

demo_workspace() {
  DEMO_WORKSPACE=$(mktemp -d "${TMPDIR:-/tmp}/plane-discovery.XXXXXX") || return
  chmod 700 "$DEMO_WORKSPACE"
}

demo_remove_workspace() {
  [[ -n "${DEMO_WORKSPACE:-}" && -d "$DEMO_WORKSPACE" && ! -L "$DEMO_WORKSPACE" ]] || return 0
  case "${DEMO_WORKSPACE##*/}" in
    plane-discovery.*) rm -rf -- "$DEMO_WORKSPACE" ;;
    *) demo_error 'Unexpected workspace path'; return 1 ;;
  esac
}

demo_kube() {
  kubectl --kubeconfig "$DEMO_KUBECONFIG" --context "$DEMO_CONTEXT" \
    --namespace "$DEMO_NAMESPACE" --request-timeout=30s "$@"
}

demo_open_cluster() {
  local cluster cluster_id node_ids node_json kube_json expected_server host
  demo_slot "$1" || return
  demo_status progress "Discover $DEMO_ENV cluster access: $DEMO_SLOT"
  mkdir -m 700 "$DEMO_WORKSPACE/$DEMO_SLOT"
  DEMO_KUBECONFIG="$DEMO_WORKSPACE/$DEMO_SLOT/kubeconfig"
  if [[ "$DEMO_ENV" == azure ]]; then
    cluster_id="/subscriptions/$AZURE_SUBSCRIPTION_ID/resourceGroups/rg-$DEMO_CONTEXT/providers/Microsoft.ContainerService/managedClusters/aks-$DEMO_CONTEXT"
    cluster=$(az aks show --subscription "$AZURE_SUBSCRIPTION_ID" \
      --resource-group "rg-$DEMO_CONTEXT" --name "aks-$DEMO_CONTEXT" --output json) || return
    printf '%s' "$cluster" | jq -e --arg id "$cluster_id" --arg project "$DEMO_PROJECT" \
      --arg deployment "$DEMO_DEPLOYMENT" '
        (.id | ascii_downcase) == ($id | ascii_downcase) and
        .tags.project == $project and .tags.deployment == $deployment and
        .provisioningState == "Succeeded"
      ' >/dev/null || { demo_error 'Azure cluster identity or readiness differs'; return 1; }
    expected_server=$(printf '%s' "$cluster" | jq -er '"https://" + (.privateFqdn // .fqdn)') || return
    az aks get-credentials --subscription "$AZURE_SUBSCRIPTION_ID" \
      --resource-group "rg-$DEMO_CONTEXT" --name "aks-$DEMO_CONTEXT" \
      --context "$DEMO_CONTEXT" --file "$DEMO_KUBECONFIG" --format exec --only-show-errors || return
    chmod 600 "$DEMO_KUBECONFIG"
  else
    host=$(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_CONFIG \
      docker context inspect desktop-linux --format '{{json .Endpoints.docker.Host}}' | jq -er .) || return
    [[ "$host" == unix:///* && "$host" != *$'\n'* && "$host" != *$'\r'* \
      && "$host" != *\?* && "$host" != *\#* && "$host" != */../* ]] || {
      demo_error 'Docker Desktop must use a local Unix socket'; return 1;
    }
    unset DOCKER_CONTEXT DOCKER_CONFIG
    node_ids=$(docker --host "$host" ps -aq --no-trunc \
      --filter "label=io.x-k8s.kind.cluster=$DEMO_CONTEXT") || return
    [[ "$node_ids" =~ ^[a-f0-9]{64}$ ]] || { demo_error 'Expected one owned kind node'; return 1; }
    node_json=$(docker --host "$host" inspect --type container "$node_ids") || return
    printf '%s' "$node_json" | jq -e --arg name "$DEMO_CONTEXT" --arg id "$node_ids" \
      --arg port "$((35495 + DEMO_SLOT_INDEX))" '
        length == 1 and .[0].Id == $id and .[0].Name == ("/" + $name + "-control-plane") and
        .[0].Config.Labels["io.x-k8s.kind.cluster"] == $name and
        .[0].State.Running == true and
        .[0].HostConfig.PortBindings["6443/tcp"] == [{"HostIp":"127.0.0.1","HostPort":$port}]
      ' >/dev/null || { demo_error 'Kind node identity or port differs'; return 1; }
    DOCKER_HOST="$host" KIND_EXPERIMENTAL_PROVIDER=docker \
      kind get kubeconfig --name "$DEMO_CONTEXT" > "$DEMO_KUBECONFIG" || return
    chmod 600 "$DEMO_KUBECONFIG"
    kubectl --kubeconfig "$DEMO_KUBECONFIG" config rename-context \
      "kind-$DEMO_CONTEXT" "$DEMO_CONTEXT" >/dev/null || return
    expected_server="https://127.0.0.1:$((35495 + DEMO_SLOT_INDEX))"
  fi
  kube_json=$(kubectl --kubeconfig "$DEMO_KUBECONFIG" config view --raw --output json) || return
  printf '%s' "$kube_json" | jq -e --arg context "$DEMO_CONTEXT" --arg server "$expected_server" \
    --arg environment "$DEMO_ENV" '
    (.clusters|length)==1 and (.contexts|length)==1 and (.users|length)==1 and
    .["current-context"]==$context and .contexts[0].name==$context and
    .contexts[0].context.cluster==.clusters[0].name and
    .contexts[0].context.user==.users[0].name and
    (.clusters[0].cluster.server==$server or
      ($environment=="azure" and .clusters[0].cluster.server==($server+":443"))) and
    (.clusters[0].cluster["certificate-authority-data"]|type)=="string" and
    (.clusters[0].cluster["certificate-authority-data"]|length)>0 and
    .clusters[0].cluster["insecure-skip-tls-verify"] != true and
    .clusters[0].cluster["proxy-url"] == null
  ' >/dev/null || { demo_error 'Kubernetes access does not match the discovered cluster'; return 1; }
  if [[ "$DEMO_ENV" == local ]]; then
    printf '%s' "$kube_json" | jq -e '
      (.users[0].user|keys|sort)==["client-certificate-data","client-key-data"] and
      (.users[0].user|all(.[]; type=="string" and length>0))
    ' >/dev/null || { demo_error 'Local access must use embedded certificates'; return 1; }
  else
    printf '%s' "$kube_json" | jq -e '
      .users[0].user.exec.command=="kubelogin" and .users[0].user["auth-provider"]==null
    ' >/dev/null || { demo_error 'Expected AKS user access through kubelogin'; return 1; }
    kubelogin convert-kubeconfig --kubeconfig "$DEMO_KUBECONFIG" \
      --context "$DEMO_CONTEXT" --login azurecli || return
  fi
}

demo_check_namespace() {
  local namespace_json
  namespace_json=$(demo_kube get namespace "$DEMO_NAMESPACE" --output json) || return
  printf '%s' "$namespace_json" | jq -e --arg namespace "$DEMO_NAMESPACE" \
    --arg project "$DEMO_PROJECT" --arg deployment "$DEMO_DEPLOYMENT" --arg env "$DEMO_ENV" '
      .metadata.name==$namespace and .metadata.labels["plane-demo/project"]==$project and
      .metadata.labels["plane-demo/deployment"]==$deployment and
      .metadata.labels["plane-demo/environment"]==$env
    ' >/dev/null || { demo_error 'Application namespace ownership differs'; return 1; }
}

demo_open_slot() {
  demo_open_cluster "$1" || return
  demo_check_namespace
}

demo_endpoint() {
  local resource_id gateway url expected_application expected_environment gateway_host
  resource_id="/planes/radius/local/resourceGroups/$DEMO_STEM/providers/Demo.Platform/gateways/gateway"
  expected_application="/planes/radius/local/resourceGroups/$DEMO_STEM/providers/Applications.Core/applications/$DEMO_ROLE"
  expected_environment="/planes/radius/local/resourceGroups/$DEMO_STEM/providers/Applications.Core/environments/$DEMO_SLOT"
  gateway=$(demo_kube get --raw \
    "/apis/api.ucp.dev/v1alpha3$resource_id?api-version=2025-08-01-preview") || return
  printf '%s' "$gateway" | jq -e --arg id "$resource_id" --arg app "$expected_application" \
    --arg environment "$expected_environment" '
      (.id|ascii_downcase)==($id|ascii_downcase) and
      (.properties.application|ascii_downcase)==($app|ascii_downcase) and
      (.properties.environment|ascii_downcase)==($environment|ascii_downcase) and
      .properties.provisioningState=="Succeeded"
    ' >/dev/null || { demo_error 'Radius gateway is not ready or has another owner'; return 1; }
  url=$(printf '%s' "$gateway" | jq -er '.properties.url') || return
  gateway_host=$(printf '%s' "$gateway" | jq -er '.properties.host') || return
  if [[ "$DEMO_ENV" == local ]]; then
    [[ "$url" == "http://127.0.0.1:$((35490 + DEMO_SLOT_INDEX))" ]] || {
      demo_error 'Local gateway address differs from its reserved port'; return 1;
    }
    [[ "$gateway_host" == 127.0.0.1 ]] || { demo_error 'Local gateway host differs'; return 1; }
  else
    [[ "$url" =~ ^https://[a-z0-9][a-z0-9.-]*\.cloudapp\.azure\.com$ ]] || {
      demo_error 'Azure gateway must use its trusted HTTPS hostname'; return 1;
    }
    [[ "$url" == "https://$gateway_host" ]] || { demo_error 'Azure gateway host differs'; return 1; }
  fi
  printf '%s\n' "$url"
}
