# Three-plane Radius deployment checkpoint

Status: Validated (external interruption restored; controlled five-cluster reset in progress).
Full API-driven onboarding, outage acceptance, local deployment, and final
teardown remain open. A source-layout change is not a new deployment result.

## Scope and authorization

The user approved implementation, real Azure deployment, automatic parent
commits, and phase/fix verification with rubber-duck and security reviews on
2026-09-09. The first real shared tenant reached control HTTPS, then failed
during data deployment. F044/F045 are corrected in `ad031e2`; its rebuilt
images passed in-cluster source/artifact checks, and the updated Redis Recipe
is published and locked. The reviewed reset completed after explicit recovery
of the failed Recipe's orphan resources. Fresh management is verified and the
first fresh tenant provisioned and applied its configuration. Acceptance then
stopped on a false TLS measurement; the corrected probe passed against the
actual verified Redis TLS connection. Continue the remaining checks without
reprovisioning that tenant or rewriting its failed harness evidence.
Deeper SQL/code simplification remains deferred until end-to-end proof.

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

Preserve `.state/azure/`, bootstrap ownership, and the hash-verified failed
evidence archive. Reset verification confirmed empty app groups and absent
child AKS while retaining the foundation. The owned old application namespace,
three state volumes, and exact Azure disks were removed after UID/ownership and
archive checks. Temporary reset access was revoked. Do not replay the failed
operation or bulk-delete local state.

`.state/azure/provisioning-next.json` validates against the inspected images and
locked Recipes; it was activated only after reset verification. Management
deployment, endpoint/worker readiness, and empty initial tenant state are proved.
Wait for the fresh three-tenant and outage outcome before starting local
implementation. The bootstrap uses standalone Bicep through the scoped
Azure CLI; application deployment uses Radius and its per-cluster Recipes.

Before any later cloud deployment, set the workflow status to
`Ready for Validation`, run the Azure validation workflow, record its result,
then use the deployment workflow. Bootstrap validation records exact template
and parameter hashes in `.state/azure/validation.json`; changed hashes require
validation again. Never infer deployment success from compilation or Job submission.

## 7. Validation Proof

2026-09-09T19:30:26Z: renewed
`uv run --no-sync python operations/validate-bootstrap.py` passed real ARM
what-if and validate, recording the unchanged exact template/parameter hashes.
What-if reported 86 deploy entries, 122 unsupported expansions, and 29 ignored
entries, with no proposed deletion. No bootstrap mutation is needed for the
fresh application run.

`make check` passed Ruff, all 22 Bicep files, three generated extensions,
404 tests and 150 subtests, and ShellCheck. Fifty explicit dependency tests
were skipped, not counted as passing. The inspected `ad031e2` images and locked
Recipe references pass `OperatorConfig` validation in `provisioning-next.json`.
Regenerating `.tgz` extensions changes packing metadata: a separate tokenless
AKS inspection verified identical `index.json` and `types.json` payload bytes
for all three regenerated extensions. All other inspected image inputs still
match source. This is content proof, not an inference from image digests.

The live Radius-only reset preview passed ownership/UID/scope checks and
produced the expected owner order using source `2b3b7ff`. After explicit orphan
recovery, guarded execution completed and operator queries independently
verified removal. Temporary access was revoked, and old application state was
verified absent at 2026-09-09T21:06:53Z. Fresh management is deploying; this does
not claim tenant acceptance or whole-environment teardown. Fresh management
subsequently completed and passed real HTTPS/authentication checks and an empty
tenant/operation inventory check. `demo-acceptance-fresh` is running all
onboarding/outage assertions from source `058d878`, with no continuation flag.

2026-09-09T17:46:04Z: the dedicated harness identity passed
`uv run --no-sync python operations/validate-bootstrap.py` (bundled Bicep compile,
subscription what-if, and ARM validate). `.state/azure/validation.json` records
the exact current template/parameter hashes. What-if reports two explicit
creates, 84 deploy entries, 122 unsupported expansions, and 19 ignored entries;
it does not provide a complete role-assignment diff. Compiled contract tests
check the exact harness grants. No deletions were proposed. Subscription auth
is valid, and subscription policy assignments were read without modification.
Ruff and the focused harness/compiled-infrastructure suite passed 149 tests
and 72 subtests. Both fix reviews were clean. Live harness access was later
proved; tenant acceptance remains incomplete.

For layout commit `43b24fc`, `make check` passed 320 offline tests and 47
subtests, all 22 Bicep files, three extensions, Ruff, and ShellCheck. Fifty
explicit dependency tests were skipped, not reported as passing.

The ACR API and provisioner builds succeeded. In-cluster verification imported
the grouped entrypoints and matched 21 API and 51 provisioner source/artifact
hashes, confirmed UID 10001, and checked that the API image excludes privileged
provisioner/provider/operations code. `.state/azure/images.json` records the
exact references and verification.

The management-specific recheck passed Ruff, 169 unit tests, all app/module/
environment Bicep compiles, and `OperatorConfig` validation against those exact
inspected images. Independent layout walkthrough/security reviews were clean.
Deployment success and full tenant/outage acceptance must still be observed.
