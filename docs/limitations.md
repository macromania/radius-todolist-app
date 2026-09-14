# Accepted demo limitations

This is a trusted-operator architecture demonstration, not a production tenant
platform. These limits are recorded in the [decisions](../DECISIONS.md);
they are not unresolved implementation failures.

- Shared demo keys provide simple per-plane API access. Production tenant
  authentication is outside the POC. Use synthetic data.
- There is one provisioner, no HA workflow engine, no automatic retry/adoption
  of interrupted infrastructure work, and no tenant migration or deletion API.
  A restarted provisioner marks old running operations interrupted.
- Readiness is immediate-child reporting, not transitive dependency health.
  Children retain last-applied state during parent outages and later apply the
  latest version.
- Azure PostgreSQL/Redis remain private and TLS-verified. Local PostgreSQL and
  Redis deliberately use non-TLS transport on internal cluster/node paths;
  local HTTP gateways bind reserved loopback ports only.
- Local execution requires Docker Desktop's `desktop-linux` context with a local
  Unix socket. Management Radius's Docker socket has whole-daemon authority;
  project names and labels are not a hostile isolation boundary.
- Management Secret encryption is verified locally. Child datastore Secrets,
  Terraform state, and PVCs are not claimed encrypted at rest. Terraform state
  contains child administrator credentials; protected state remains sensitive.
- Datastore Pod replacement demonstrated persistence on existing PVCs, not
  forced-crash durability, backup/restore, disaster recovery, or clean process exit.
- Certificate issuance is implemented; automatic certificate renewal is not.
  Azure's protected soft-deleted vault is not claimed purged before its retention
  deadline.
- The verified deployments are removed. Historical endpoints, access files,
  attempt records, and evidence are not a live deployment or automatic resume path.
  Local images/cache and the shared kind network remain intentionally.

Run scopes and source revisions matter. Azure's historical functional evidence
and the final local identity-boundary proof are distinguished in
[FINDINGS.md](../FINDINGS.md). Deeper SQL/authentication/reconciliation
simplification was deferred; this delivery does not perform that redesign.
