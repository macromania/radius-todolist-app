#!/usr/bin/env bash
# Bootstrap helper, not a cluster creator. Run before installing Radius/demo Secrets.
# Existing Secrets are not rewritten. This proves encryption of one new synthetic Secret.
# Deleting the kind node deletes its key. Reruns verify ownership and never rotate it.
# Python 3 is used only for host process supervision; the node needs GNU coreutils timeout.
# jq filters and node shell source must not expand in the operator shell.
# shellcheck disable=SC2016
set +x
set -euo pipefail

fail() { printf 'Node encryption failed: %s\n' "$1" >&2; exit 1; }
usage() {
    printf '%s\n' 'Usage: bash encryption.sh --cluster NAME --node NAME --kubeconfig FILE --context NAME [--docker-host unix:///PATH]'
}

cluster='' node='' kubeconfig='' context='' docker_host=''
while (($#)); do
    case "$1" in
        --cluster|--node|--kubeconfig|--context|--docker-host)
            (($# >= 2)) || fail missing_argument
            case "$1" in
                --cluster) [[ -z "$cluster" ]] || fail duplicate_argument; cluster=$2 ;;
                --node) [[ -z "$node" ]] || fail duplicate_argument; node=$2 ;;
                --kubeconfig) [[ -z "$kubeconfig" ]] || fail duplicate_argument; kubeconfig=$2 ;;
                --context) [[ -z "$context" ]] || fail duplicate_argument; context=$2 ;;
                --docker-host) [[ -z "$docker_host" ]] || fail duplicate_argument; docker_host=$2 ;;
            esac
            shift 2 ;;
        --help) usage; exit 0 ;;
        *) fail unknown_argument ;;
    esac
done
[[ "$cluster" =~ ^[a-z][a-z0-9-]{0,44}[a-z0-9]$ ]] || fail invalid_cluster
[[ "$node" == "$cluster-control-plane" ]] || fail invalid_node
[[ "$context" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$ ]] || fail invalid_context
[[ -f "$kubeconfig" && ! -L "$kubeconfig" ]] || fail explicit_kubeconfig_required
for tool in docker kubectl jq sleep python3; do
    command -v "$tool" >/dev/null 2>&1 || fail required_tool_missing
done
unset DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG KUBECONFIG KUBERNETES_MASTER
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
docker_scope=(--context desktop-linux)
if [[ -n "$docker_host" ]]; then
    [[ "$docker_host" =~ ^unix:///[-a-zA-Z0-9_./]+$ && "$docker_host" != *"/../"* \
        && "$docker_host" != *"/.." ]] || fail invalid_docker_socket
    docker_scope=(--host "$docker_host")
fi

# Bash 3.2 command substitutions do not provide reliable process-group ownership.
# This stdlib-only supervisor owns a new session and its output pipe, not just the CLI PID.
bounded() {
    python3 -I -S -c '
import os
import signal
import subprocess
import sys

def interrupted(number, frame):
    raise SystemExit(128 + number)

for number in (signal.SIGINT, signal.SIGTERM):
    signal.signal(number, interrupted)
process = None
status = 125
try:
    limit = float(sys.argv[1])
    if not 0 < limit <= 120:
        raise SystemExit(125)
    process = subprocess.Popen(
        sys.argv[2:], stdin=sys.stdin, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    output, _ = process.communicate(timeout=limit)
    status = process.returncode
except subprocess.TimeoutExpired:
    status = 124
except (OSError, subprocess.SubprocessError):
    status = 125
finally:
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.stdout.close()
        process.wait(timeout=2)
if status == 0:
    sys.stdout.buffer.write(output)
raise SystemExit(status if status >= 0 else 128 - status)
' "$@" 2>/dev/null
}
docker_cli() { bounded 30 docker "${docker_scope[@]}" "$@"; }
kube() {
    bounded 15 kubectl --kubeconfig "$kubeconfig" --context "$context" \
        --request-timeout=10s "$@"
}
jq_safe() { jq "$@" 2>/dev/null; }
# Keep this digest identical to the repository's pinned kind 1.35.0 image.
node_image='kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f'
profile=/etc/kubernetes/radplanes/encryption.yaml
manifest=/etc/kubernetes/manifests/kube-apiserver.yaml

inspection=$(docker_cli inspect --type container "$node") || fail node_inspection
identity=$(jq_safe -er --arg cluster "$cluster" --arg node "$node" --arg image "$node_image" '
    select(length == 1) | .[0] |
    select(.Name == ("/" + $node) and .State.Running == true and .Config.Image == $image) |
    select(.Config.Labels["io.x-k8s.kind.cluster"] == $cluster
        and .Config.Labels["io.x-k8s.kind.role"] == "control-plane") |
    select(all(.Mounts[]?; .Destination as $mount |
        all(["/etc/kubernetes/radplanes/encryption.yaml",
             "/etc/kubernetes/manifests/kube-apiserver.yaml"][];
            ($mount == "/" or startswith($mount + "/") or . == $mount) | not))) |
    select(.NetworkSettings.Ports["6443/tcp"] | length == 1) |
    .NetworkSettings.Ports["6443/tcp"][0] as $port |
    select($port.HostIp == "127.0.0.1"
        and ($port.HostPort | test("^(3549[0-9]|3551[0-9])$"))) |
    select(.Id | test("^[a-f0-9]{64}$")) | [.Id, $port.HostPort] | @tsv
' <<<"$inspection") || fail node_identity_mismatch
IFS=$'\t' read -r node_id api_port <<<"$identity"

# config view does not execute credential plugins. Reject them before any API call.
configuration=$(kube config view --minify -o json) || fail kubeconfig_read
jq_safe -e --arg context "$context" --arg cluster "kind-$cluster" \
    --arg server "https://127.0.0.1:$api_port" '
    .["current-context"] == $context and
    (.contexts | length) == 1 and (.clusters | length) == 1 and (.users | length) == 1 and
    .contexts[0].name == $context and .contexts[0].context.cluster == $cluster and
    .contexts[0].context.user == .users[0].name and .clusters[0].name == $cluster and
    (.clusters[0].cluster | keys) == ["certificate-authority-data", "server"] and
    .clusters[0].cluster.server == $server and
    (.clusters[0].cluster["certificate-authority-data"] | type == "string" and length > 0) and
    (.users[0].user | keys) == ["client-certificate-data", "client-key-data"] and
    all(.users[0].user[]; type == "string" and length > 0)
' <<<"$configuration" >/dev/null || fail unsafe_kubeconfig
unset configuration

namespace=$(kube get namespace kube-system -o json) || fail namespace_read
cluster_uid=$(jq_safe -er '
    select(.kind == "Namespace" and .metadata.name == "kube-system"
        and .metadata.deletionTimestamp == null) |
    .metadata.uid | select(test("^[a-f0-9-]{36}$"))
' <<<"$namespace") || fail namespace_identity_mismatch
node_object=$(kube get node "$node" -o json) || fail kubernetes_node_read
node_uid=$(jq_safe -er --arg node "$node" '
    select(.kind == "Node" and .metadata.name == $node
        and .status.nodeInfo.kubeletVersion == "v1.35.0") |
    .metadata.uid | select(test("^[a-f0-9-]{36}$"))
' <<<"$node_object") || fail kubernetes_node_mismatch
manifest_hash=$(docker_cli exec --user 0 "$node" sha256sum "$manifest") || fail manifest_read
manifest_hash=${manifest_hash%% *}
[[ "$manifest_hash" =~ ^[a-f0-9]{64}$ ]] || fail invalid_manifest_digest
pod=$(kube -n kube-system get pod "kube-apiserver-$node" -o json) || fail apiserver_read
jq_safe -e --arg node "$node" --arg uid "$node_uid" '
    .kind == "Pod" and .metadata.name == ("kube-apiserver-" + $node) and
    .metadata.namespace == "kube-system" and .spec.nodeName == $node and
    .metadata.annotations["kubernetes.io/config.source"] == "file" and
    (.metadata.annotations["kubernetes.io/config.mirror"] | type == "string" and length > 0) and
    any(.metadata.ownerReferences[]?; .kind == "Node" and .name == $node and .uid == $uid) and
    .metadata.labels.component == "kube-apiserver" and .spec.hostNetwork == true and
    .metadata.labels.tier == "control-plane" and
    ((.spec.initContainers // []) | length) == 0 and
    all(.spec.volumes[]?; .hostPath != null or
        ((.name | startswith("kube-api-access-")) and .projected != null)) and
    (.spec.containers | length) == 1 and .spec.containers[0].name == "kube-apiserver" and
    .spec.containers[0].image == "registry.k8s.io/kube-apiserver:v1.35.0" and
    .spec.containers[0].command[0] == "kube-apiserver"
' <<<"$pod" >/dev/null || fail apiserver_identity_mismatch

encryption_state() {
    jq_safe -er --arg path "$profile" '
        [(.spec.containers[0].command + (.spec.containers[0].args // []))[] |
            select(startswith("--encryption-provider-config"))] as $args |
        [.spec.containers[0].volumeMounts[]? |
            select(.name == "radplanes-encryption" or .mountPath == $path)] as $mounts |
        [.spec.volumes[]? |
            select(.name == "radplanes-encryption" or .hostPath.path == $path)] as $volumes |
        if $args == [] and $mounts == [] and $volumes == [] then "unconfigured"
        elif $args == [("--encryption-provider-config=" + $path)] and
            $mounts == [{"name":"radplanes-encryption", "mountPath":$path, "readOnly":true}] and
            $volumes == [{"name":"radplanes-encryption", "hostPath":{"path":$path,"type":"File"}}]
        then "configured" else error("conflicting encryption configuration") end
    '
}
state=$(encryption_state <<<"$pod") || fail conflicting_encryption_configuration

timeout_version=$(docker_cli exec --user 0 "$node" timeout --version) || fail node_timeout_required
[[ "$timeout_version" == "timeout (GNU coreutils)"* ]] || fail node_timeout_required

# All key generation, validation and persistence happen inside this exact node.
# Killing Docker's client does not cancel an exec inside the node. Bound that work there too.
key_status=$(docker_cli exec --user 0 -i "$node" \
    timeout --signal=TERM --kill-after=2s 20s sh -s -- \
    "$cluster" "$node" "$node_id" "$cluster_uid" "$state" <<'NODE_KEY'
set +x
set -eu
umask 077
directory=/etc/kubernetes/radplanes
profile=$directory/encryption.yaml
owner=$directory/encryption.owner
lock=/run/radplanes-encryption.lock
mkdir -m 700 "$lock" || exit 1
trap 'rmdir "$lock"' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
metadata=$(printf 'version=1\ncluster=%s\nnode=%s\ncontainer=%s\nclusterUID=%s' "$1" "$2" "$3" "$4")
prefix='{"apiVersion":"apiserver.config.k8s.io/v1","kind":"EncryptionConfiguration","resources":[{"resources":["secrets"],"providers":[{"aescbc":{"keys":[{"name":"local-key","secret":"'
suffix='"}]}},{"identity":{}}]}]}'
if [ "$5" = configured ]; then
    [ -d "$directory" ] && [ ! -L "$directory" ] &&
        [ "$(stat -c '%u:%g:%a' "$directory")" = 0:0:700 ] || exit 1
    for file in "$profile" "$owner"; do
        [ -f "$file" ] && [ ! -L "$file" ] &&
            [ "$(stat -c '%u:%g:%a' "$file")" = 0:0:600 ] || exit 1
    done
    key=$(sed -n 's/.*"secret":"\([A-Za-z0-9+/]\{43\}=\)".*/\1/p' "$profile")
    [ "${#key}" -eq 44 ]
    [ "$(printf '%s' "$key" | base64 --decode | wc -c | tr -d ' ')" = 32 ]
    [ "$(cat "$profile")" = "$prefix$key$suffix" ]
    digest=$(sha256sum "$profile"); digest=${digest%% *}
    [ "$(cat "$owner")" = "$(printf '%s\nsha256=%s' "$metadata" "$digest")" ]
    printf 'existing\n'
else
    # An interrupted install or an unowned existing directory requires explicit inspection.
    [ ! -e "$directory" ] && [ ! -L "$directory" ] || exit 1
    mkdir -m 700 "$directory"
    key=$(head -c 32 /dev/urandom | base64 | tr -d '\n')
    [ "${#key}" -eq 44 ]
    printf '%s%s%s\n' "$prefix" "$key" "$suffix" >"$profile.pending"
    chmod 600 "$profile.pending"
    mv "$profile.pending" "$profile"
    digest=$(sha256sum "$profile"); digest=${digest%% *}
    printf '%s\nsha256=%s\n' "$metadata" "$digest" >"$owner.pending"
    chmod 600 "$owner.pending"
    mv "$owner.pending" "$owner"
    printf 'created\n'
fi
NODE_KEY
) || fail node_key_ownership_or_installation_inspect_partial_node_state
[[ "$key_status" == created || "$key_status" == existing ]] || fail unexpected_key_status

if [[ "$key_status" == created ]]; then
    # A mirrored API object is not a static manifest. Keep only kubeadm runtime fields;
    # remove API identity, scheduling/default-service-account fields and token projection.
    replacement=$(jq_safe -c --arg path "$profile" '
        def keep($keys): with_entries(select(.key as $key | $keys | index($key)));
        {
            apiVersion:"v1", kind:"Pod",
            metadata:{name:"kube-apiserver", namespace:"kube-system", labels:.metadata.labels,
                annotations:(.metadata.annotations | with_entries(select(
                    .key | startswith("kubeadm.kubernetes.io/"))))},
            spec:(.spec | keep(["containers","hostNetwork","priorityClassName","securityContext","volumes"]))
        } |
        .spec.containers |= map(
            keep(["name","image","command","args","ports","resources","volumeMounts","livenessProbe",
                  "readinessProbe","startupProbe","securityContext","imagePullPolicy"]) |
            .volumeMounts |= map(select(.name | startswith("kube-api-access-") | not))
        ) |
        .spec.volumes |= map(select(.name | startswith("kube-api-access-") | not)) |
        .spec.containers[0].command += [("--encryption-provider-config=" + $path)] |
        .spec.containers[0].volumeMounts += [
            {name:"radplanes-encryption", mountPath:$path, readOnly:true}] |
        .spec.volumes += [{name:"radplanes-encryption", hostPath:{path:$path,type:"File"}}]
    ' <<<"$pod") || fail manifest_construction
    docker_cli exec --user 0 -i "$node" timeout --signal=TERM --kill-after=2s 20s sh -c '
        set +x
        set -eu
        umask 077
        file=/etc/kubernetes/manifests/kube-apiserver.yaml
        pending=/etc/kubernetes/radplanes/kube-apiserver.pending
        [ -f "$file" ] && [ ! -L "$file" ] && [ ! -e "$pending" ] || exit 1
        actual=$(sha256sum "$file"); actual=${actual%% *}
        [ "$actual" = "$1" ]
        cat >"$pending"
        chmod 600 "$pending"
        mv "$pending" "$file"
    ' sh "$manifest_hash" <<<"$replacement" >/dev/null ||
        fail manifest_installation_inspect_partial_node_state
fi

deadline=$((SECONDS + 180))
ready=false
while ((SECONDS < deadline)); do
    if kube get --raw=/readyz >/dev/null &&
        current=$(kube -n kube-system get pod "kube-apiserver-$node" -o json) &&
        [[ "$(encryption_state <<<"$current")" == configured ]] &&
        jq_safe -e --arg node "$node" '
            .spec.nodeName == $node and .metadata.namespace == "kube-system" and
            .metadata.name == ("kube-apiserver-" + $node) and
            any(.status.conditions[]?; .type == "Ready" and .status == "True")
        ' <<<"$current" >/dev/null; then
        ready=true
        break
    fi
    sleep 1
done
[[ "$ready" == true ]] || fail apiserver_restart_timeout
namespace=$(kube get namespace kube-system -o json) || fail namespace_recheck
jq_safe -e --arg uid "$cluster_uid" '.metadata.uid == $uid' \
    <<<"$namespace" >/dev/null || fail cluster_identity_changed

probe=$(kube -n default create -f - -o json <<'PROBE'
{"apiVersion":"v1","kind":"Secret","metadata":{"generateName":"radplanes-encryption-probe-","namespace":"default"},"type":"Opaque","stringData":{"probe":"node-encryption-synthetic-proof"}}
PROBE
) || fail probe_creation
probe_name=$(jq_safe -er '
    select(.kind == "Secret" and .metadata.namespace == "default") |
    .metadata.name | select(test("^radplanes-encryption-probe-[a-z0-9]{5}$"))
' <<<"$probe") || fail probe_identity_mismatch
probe_uid=$(jq_safe -er '.metadata.uid | select(test("^[a-f0-9-]{36}$"))' \
    <<<"$probe") || fail probe_identity_mismatch

cleanup_probe() {
    local current options
    probe_pending=false
    if ! current=$(kube -n default get secret "$probe_name" -o json); then
        printf 'Node encryption cleanup failed: probe_read\n' >&2
        return 1
    fi
    if ! jq_safe -e --arg uid "$probe_uid" --arg name "$probe_name" '
        .kind == "Secret" and .metadata.uid == $uid and
        .metadata.name == $name and .metadata.namespace == "default"
    ' <<<"$current" >/dev/null; then
        printf 'Node encryption cleanup failed: probe_identity_changed\n' >&2
        return 1
    fi
    if ! options=$(jq_safe -cn --arg uid "$probe_uid" \
        '{apiVersion:"v1",kind:"DeleteOptions",preconditions:{uid:$uid}}'); then
        printf 'Node encryption cleanup failed: delete_options\n' >&2
        return 1
    fi
    # Raw DELETE carries an atomic UID precondition. A replacement after the GET is safe.
    if ! kube delete --raw "/api/v1/namespaces/default/secrets/$probe_name" \
        -f - <<<"$options" >/dev/null; then
        printf 'Node encryption cleanup failed: uid_preconditioned_delete\n' >&2
        return 1
    fi
}
finish_probe() {
    local status=$?
    trap - EXIT
    trap '' INT TERM
    if [[ "$probe_pending" == true ]]; then
        if ! cleanup_probe && ((status == 0)); then
            status=1
        fi
    fi
    exit "$status"
}
probe_pending=true
trap finish_probe EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

registry_key="/registry/secrets/default/$probe_name"
stored=$(kube -n kube-system exec "etcd-$node" -- etcdctl \
    --endpoints=https://127.0.0.1:2379 --dial-timeout=5s --command-timeout=5s \
    --cacert=/etc/kubernetes/pki/etcd/ca.crt \
    --cert=/etc/kubernetes/pki/etcd/healthcheck-client.crt \
    --key=/etc/kubernetes/pki/etcd/healthcheck-client.key \
    get "$registry_key" --write-out=json) || fail ciphertext_read
jq_safe -e --arg key "$registry_key" '
    (.kvs | length) == 1 and (.kvs[0].key | @base64d) == $key and
    (.kvs[0].value | @base64d | startswith("k8s:enc:aescbc:v1:local-key:"))
' <<<"$stored" >/dev/null || fail ciphertext_proof
decrypted=$(kube -n default get secret "$probe_name" -o json) || fail probe_decryption
jq_safe -e --arg uid "$probe_uid" --arg name "$probe_name" '
    .metadata.uid == $uid and .metadata.name == $name and .metadata.namespace == "default"
' <<<"$decrypted" >/dev/null || fail probe_identity_changed
value=$(jq_safe -er '.data.probe | @base64d' <<<"$decrypted") || fail probe_decryption
[[ "$value" == node-encryption-synthetic-proof ]] || fail probe_decryption
unset value probe decrypted stored
cleanup_probe || fail probe_cleanup
jq_safe -n --arg cluster "$cluster" --arg node "$node" --arg id "$node_id" \
    --arg uid "$cluster_uid" --arg status "$key_status" \
    '{cluster:$cluster,node:$node,nodeId:$id,clusterUID:$uid,keyStatus:$status,
      provider:"aescbc",keyName:"local-key",syntheticCiphertextVerified:true}'
