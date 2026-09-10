# Local milestone 5: one-child feasibility gate

**Status: source and native images are validated; live feasibility is not yet proven.**
The first management installation stopped on an actual encryption check. A
reviewed fresh bootstrap now passes encryption and Radius/socket checks. The
first child request was rejected on an API-version mismatch before creation;
its corrected versioned submission is awaiting live proof. This is
not the full local tenant demo or a `LocalProvider`. Azure is closed and must not
be recreated for this gate.

## Ownership and scope

| Surface | Responsibility |
|---|---|
| `infra/radius/recipes/local/cluster/` | Radius-owned Terraform child creation, protected access Secret, and destruction |
| `images/radius-kind/Dockerfile` | Separate `executor` and `operator` targets; no API/provisioner image reuse |
| `operations/local/prepare.py` | Deterministic, allowlisted module archive and static in-cluster publication manifests |
| `operations/local/images.py` | Explicit image build and actual binary/content inspection |
| `operations/local/bootstrap.py` | Operator-owned management kind exception, Radius installation, management-only overlay |
| `operations/local/bootstrap-child.py` | Runs inside a management Pod; verifies child TLS and installs child Radius/workload |
| `harness/local/cluster-gate.py` | Drives and verifies the single-child experiment, including Radius deletion |
| `tests/operations/local/` | Offline command-path, refusal, protection, and Terraform mock-provider tests |

Management is `radplanes-local-management`. The only admitted child is
`radplanes-local-shared-control`, submitted as `Demo.Platform/clusters/shared-control`.
Project kubecontexts use those same `radplanes-local-<slot>` names, without kind's
default `kind-` prefix. Bootstrap renames the management context only in its
isolated `.state/local/home/.kube/config`. The Recipe renames the child context in
the protected Secret copy, retaining its cluster/user references, credentials,
and CA; the provider's original kubeconfig is unchanged. Allocation keys remain
`management`, `shared-control`, `shared-data`, `isolated-1-control`, and
`isolated-1-data`. This gate does not adapt shared provisioning or provider code.
The existing `environment` and `slot` schema is sufficient; `application` is optional
for this gate. The Recipe emits `kind://<cluster-name>`, the cluster name, and
`kubernetes://radplanes-local-access/<secret>#kubeconfig`. It omits Azure-only fields.
The three application declarations and `modules/child-cluster.bicep` are unchanged.
The harness submits the custom type directly rather than adding a gate branch to
those application declarations.
The generic create CLI uses the legacy API version, so child creation uses an
authenticated native Radius PUT with the registered `2025-08-01-preview`
version. The gate still waits for actual provisioning completion and observes
the executor; it does not equate HTTP acceptance with a ready cluster.

There is no host-side child `kind create`, Terraform apply, or child `kind delete`.
Management Radius's long-lived **`dynamic-rp`** runs Terraform. The separate
management bootstrap Job only uses the resulting child credentials and administrative
CLIs; it has no Docker socket or management service-account token.

## Staged commands

Run from the repository root with the project Python 3.13 environment. Preview
commands make no cluster/Docker calls. Preparation writes only ignored, private
`.state/local/` artifacts:

```sh
uv run python operations/local/prepare.py
uv run python operations/local/images.py build
uv run python operations/local/bootstrap.py create
uv run python operations/local/bootstrap.py install
uv run python harness/local/cluster-gate.py run
```

After reviewing the source, the parent executes each stage explicitly:

```sh
uv run python operations/local/images.py build --execute
uv run python operations/local/images.py inspect --execute
uv run python operations/local/bootstrap.py create --execute
uv run python operations/local/bootstrap.py install --execute
uv run python harness/local/cluster-gate.py run --execute
```

The image build uses a narrow `images/radius-kind/` context, native Docker Desktop
architecture, pinned public base digests, and checksum-verified tools. Build/pull
output remains visible. Inspection runs temporary, explicitly named containers
without networking or host mounts. It compares the derived `/dynamic-rp` binary
against the pinned upstream image, checks the inherited entrypoint/non-root user,
and executes the Docker/Terraform/rad/kubectl version checks. Bootstrap refuses
images whose current IDs differ from `.state/local/image-review.json`.

The installed image and socket are also checked from the real management executor.
The installer compares its daemon ID with the explicitly addressed host daemon and
its Radius binary hash with the inspected image. A source-derived image tag alone
is not accepted as content proof.

Management creation checks and briefly reserves **all ten** `ports.env` ports
35490-35499 before creating anything. It records current Docker CPU/memory/
architecture and container IDs rather than relying on the earlier preflight.
Reservations must be released before Docker binds them; a competing bind fails
creation, never triggers an alternate port or takeover.

| Purpose | Management | Gate child |
|---|---:|---:|
| Loopback gateway host port | 35490 | 35491 |
| Loopback Kubernetes host API port | 35495 | 35496 |
| Envoy internal NodePort | 31480 | 31480 |
| Parent PostgreSQL internal NodePort | 31543 | No host mapping |

The other six host ports remain reserved for the later topology. No current
context, global Radius configuration, Docker configuration, SSH directory, or
human registry credentials are changed/mounted. All host CLI commands use explicit
project names, context/config paths, and an isolated `.state/local/home`.

## What a live pass must prove

The gate refuses an existing child, existing access Secret, or existing Terraform
state instead of adopting it. During the custom-resource request it observes a
real kind-provider executable under `/proc` **inside the management `dynamic-rp`
container**, retaining the Pod UID and non-secret executable path. A state Secret
or resource metadata alone cannot pass.

It then reads the Radius-owned Terraform Kubernetes backend Secret in memory,
checks the completed `kind_cluster` and tracked access Secret, and retains only
non-secret state identity/version/serial information. Its expected backend name
follows Radius 0.60.2's truncated SHA-256 naming contract. This bounded state fits
one Secret; a changed/chunked contract must be investigated, not guessed.

The Recipe's external data source checks the child Docker ownership label and
retrieves the node's actual IPv4 address on the `kind` network. It replaces only
the host-facing API endpoint with `https://<node-IP>:6443`, retains the CA and
client credentials, and sets `tls-server-name` to an explicit kubeadm certificate
SAN. It does not rely on Docker container names resolving in Kubernetes Pod DNS.
The management Job must reach `/readyz` with CA verification and reject an incorrect
certificate name with an actual x509 error. There is no TLS bypass.

From that management Pod, the gate installs stock child Radius and submits a
harmless sleeping container through child Radius. It verifies real workload
rollouts. A child PostgreSQL client Job must reject a deliberately wrong password
and run a SQL query against the parent's internal NodePort with the correct
password. A separate child Envoy fixture must return this run's unique marker via
`http://127.0.0.1:35491/`.

Only after these checks does the harness delete the custom cluster resource
**through Radius**, without `--force`. It verifies child Docker containers,
Radius resource, Terraform state, and management access Secret are absent, and
checks pre-existing Docker container IDs remain. Unexpected extra/disappeared
containers fail verification; the harness does not delete them. It removes only
its run-labelled management Job/PostgreSQL fixtures. Management Radius, module
server, and bootstrap cluster deliberately remain operator-owned.

## Protection and accepted local risks

The pinned kind provider has no suitable native ephemeral/write-only kubeconfig
or secrets-manager integration. **Terraform state contains the child administrator
private key.** `sensitive()` limits expression disclosure; it does not encrypt
state, and the provider's own computed credentials are not marked sensitive.

Management's Kubernetes API encrypts Secrets in etcd using a generated AES-CBC
key held in a mode-0600 project file under mode-0700 state directories. kind 0.31
generates kubeadm v1beta3 even for Kubernetes 1.35, so both encryption and child
certificate-SAN patches must match that API and its map-shaped `extraArgs`.
Bootstrap verifies a harmless Secret's actual ciphertext before reporting ready
or permitting Radius installation. The gate
reads the actual etcd value through the scoped etcd Pod and verifies the encryption
prefix, both for the Radius encryption-key Secret and the Recipe's state/access
Secrets. These are read-only backing-store checks, never edits. Default service
accounts from `default`, `radius-system`, and `radplanes-local-access` must be
denied `get`, `list`, and `watch` access in both protected namespaces, including
cross-namespace bindings. A denial for one namespace's account is not proof
about another account.
Radius backend state is not assumed to be covered by Radius resource-field encryption.

The post-install overlay changes only management `dynamic-rp`, preserves its
binary/entrypoint and UID/GID 65532, and keeps `/terraform` and
`/terraform/.terraform-global/terraform`. A restricted root init container seeds
that volume with the checksum-pinned Terraform 1.15.8 binary and changes only its
ownership. It mounts neither credentials nor the socket. `fsGroup: 65532` permits
the sole main container to read the projected Radius encryption key; an unexpected
sidecar is rejected. The observed socket group is added as a supplemental group
only if Unix permissions require it. A root-only, non-group-writable socket is a
blocker, not permission to run the executor as root or chmod the daemon socket.

Helm's generic Terraform pre-download is disabled for these lightweight local
installations; management's explicit init provides the canonical binary/layout.
Other RPs and child Radius receive no derived image or socket overlay.
`dynamic-rp-config` is patched through Kubernetes to set `terraform.logLevel: OFF`;
`RADIUS_LOGGING_LEVEL=error` suppresses the otherwise plaintext Terraform plan
stdout, including destruction output. The chart does **not** wire an arbitrary
`global.terraform.loglevel` value into this dynamic-RP setting. Errors remain
visible, and the gate fails on private-key markers in executor logs without
exporting those logs. Unexpected diagnostic disclosure still needs operator review.

Docker Desktop's host client endpoint is
`unix:///Users/mahmutcanga/.docker/run/docker.sock`. The management node's
`extraMounts` uses `/var/run/docker.sock` as the **unproven VM-side candidate** and
exposes it inside that node at `/run/radplanes/docker.sock`; the RP hostPath refers
to the node-side path, not the Mac home directory. Socket existence, group behavior,
daemon identity, and Pod reachability are live gates. There is no Colima/TCP daemon
fallback. Socket access grants **all daemon control**, including unrelated
containers and host mounts. A read-only bind would not make the Docker API
read-only. Names, labels, and the shared `kind` network are not security boundaries.

The HTTP module service is ClusterIP-only, immutable and content-addressed. Its
ConfigMap contains only an allowlisted archive and a single-file server; no
credentials, state, directory listing, or human Git authentication. It is readable
inside the cluster and is not an authenticated production artifact registry.
Provider versions are exact and the committed lock covers Linux arm64/amd64 and
the current Darwin arm64 validator. Radius generates a wrapper root module, so
the child module lock is not claimed to enforce the wrapper's runtime lock.
The Registry's signed provider checksums and exact version constraints remain
the runtime provider source; a private mirror is out of scope.

The PostgreSQL and Envoy resources are disposable **harness fixtures**, not local
datastore/gateway Recipes for the full application. PostgreSQL uses password
authentication and explicitly `tlsRequired: false` / `sslmode=disable`, with
synthetic data on an `emptyDir`. Its lack of TLS/persistence is a dev-only gate
limitation. Azure TLS behavior is unchanged. Gateway HTTP binds only loopback.
The deleted ACR and cached amd64 application/provisioner images are never used.

## Failure handling and parent decisions

The gate has a 45-minute external reporting budget, a 900-second child submission
wait, bounded administrative commands, and a 20-minute management Job deadline.
These bounds **do not promise cancellation of Radius or the provider**. Provider
0.11.0 uses legacy CRUD, ignores standard Terraform timeout overrides, and has
only kind's fixed five-minute readiness wait. No custom timeout block pretends
otherwise.

A failed run remains failed under `.state/local/runs/<run-id>.json`. Interrupted
creation is not retried, adopted, resumed, or repaired automatically. If a failed
gate already recorded completed child/state ownership, the parent can request
only the supported Radius deletion:

```sh
uv run python harness/local/cluster-gate.py delete \
  --run .state/local/runs/<run-id>.json --execute
```

Cleanup success is recorded separately; it does not turn a failed gate into a
passed feasibility result. Missing state or a changed node/Secret UID blocks this
command. Partial creation without usable state needs a separately reviewed
operator decision; this implementation supplies no host child-deletion shortcut.
Likewise, bootstrap/install start records refuse automatic replay after interruption.
Management teardown remains parent-owned and is not implemented here.
`make check` and CI include the cloud-free local Python tests, Terraform
mock-provider validation, and Recipe shell checks; they never create clusters.

No schema change or Azure operation is needed. Before accepting milestone 5,
the parent must resolve any observed VM socket/Unix permission, image architecture,
kubeadm/etcd, module URL, Radius state, TLS/routing, scheduling, PostgreSQL, or Envoy
failure with actual evidence. Offline success is not evidence for those live seams.

## Offline validation

```sh
uv run ruff check operations/local harness/local tests/operations/local
uv run pytest -q tests/operations/local
uv run python operations/local/validate.py
shellcheck infra/radius/recipes/local/cluster/node-address.sh
```

The Terraform validator copies the exact archive into private temporary project
state, initializes the locked providers with no backend, validates the module,
and runs two tests with **all three providers mocked**. Its mock apply does not
contact kind, Docker, Kubernetes, or Radius. It checks fixed ports, reference-only
outputs, CA-preserving endpoint correction/SAN selection, and rejection of other
slots. Python tests exercise the actual entrypoints and command sequencing with
offline doubles, including protection checks and refusal/failure/cleanup paths.
The validator may download pinned public provider packages; it does not need
Azure or a live local cluster.

Implementation validation on 2026-09-10 passed 36 local Python tests, both
Terraform mock-provider tests, Ruff, Python 3.13 syntax checks, and ShellCheck.
The broader `tests/unit tests/operations tests/harness` run passed 544 tests and
226 subtests, with one existing Starlette/httpx deprecation warning. The host
Terraform validator was 1.14.3; the executor runtime is pinned to 1.15.8.
These results contain no live cluster, image-build, or connectivity claim.
