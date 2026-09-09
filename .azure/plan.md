# Three-plane Radius deployment checkpoint

Status: implementation in progress; Azure integration gates passed.
Full API-driven onboarding, outage acceptance, local deployment, and final
teardown remain open. A source-layout change is not a new deployment result.

## Scope and authorization

The user approved implementation, real Azure deployment, automatic parent
commits, and phase/fix verification with rubber-duck and security reviews on
2026-09-09. The current structure-only pass does not deploy or change live state.
Deeper SQL/code simplification is deferred until end-to-end proof.

Use the explicitly scoped subscription
`a3ed6c04-563f-4855-ac84-bdf1e5fbc3fc`, region `centralus`, and owned
`rg-radplanes-*` resources. Apply `SecurityControl=Ignore` to taggable resources;
do not disable organizational policy. Legacy `rg-todolist-*` is out of scope.

## Source of truth

- [Repository map and actual commands](../README.md)
- [Application contracts](../docs/contracts.md)
- [Provisioning prerequisites and state](../docs/provisioning.md)
- [Azure infrastructure contracts](../docs/azure-infrastructure.md)
- [Decisions](../DECISIONS.md) and [observed results](../FINDINGS.md)
- [Cleanup ownership and verification](../docs/cleanup.md)

The approved ExecPlan remains in the session's
`files/three-plane-execplan.md`; these repository documents contain durable
implementation contracts and evidence, not a duplicate architecture.

## Next deployment boundary

Preserve `.state/azure/`, protected credentials, ownership manifests, and
distinct operator/provisioner volumes. Rebuild and inspect changed images and
update layout-dependent command overrides before deployment. Existing live
images/evidence refer to the pre-layout source.

Before any later cloud deployment, set the workflow status to
`Ready for Validation`, run the Azure validation workflow, record its result,
then use the deployment workflow. Bootstrap validation records exact template
and parameter hashes in `.state/azure/validation.json`; changed hashes require
validation again. Never infer deployment success from compilation or Job submission.
