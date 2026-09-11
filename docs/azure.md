# Azure implementation and operations

Azure was implemented and proved before local. Historical admission and
existing-state functional/outage checks have separate evidence scopes; failed
records were not relabeled. The demonstrated Azure resources and temporary
access were removed. The protected soft-deleted vault retains its scheduled
September 17, 2026 purge date.

Use these contracts rather than archived credentials or endpoints:

- [Infrastructure](azure-infrastructure.md): bootstrap, custom types, Recipes,
  private datastores, HTTPS, identities, and historical integration gates.
- [Provisioning](provisioning.md): singleton run path, scoped commands,
  prerequisites, and deployment ownership.
- [Cleanup](cleanup.md): explicit owner ordering and independent verification.
- [Findings](../FINDINGS.md): exact source revisions, run times, results, and limitations.

The final shared data-API runtime-identity correction was compiled for Azure
and local and proved on local Radius. Historical Azure results predate that
correction; they are not a new post-correction Azure deployment claim.
