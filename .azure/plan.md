# Three-plane Radius deployment checkpoint

Status: Validated (fresh management deploying with parent-only Redis lifecycle Recipe).
Full API-driven onboarding, outage acceptance, local deployment, and final
teardown remain open. A source-layout change is not a new deployment result.

## Scope and authorization

The user approved implementation, real Azure deployment, automatic parent
commits, and phase/fix verification with rubber-duck and security reviews on
2026-09-09. Two shared tenants, reuse, and immediate-child readiness were
proved before governance automation interrupted isolated onboarding. Failure
evidence was preserved; no interrupted operation was replayed or relabeled.
The controlled reset now has independent Azure absence proof. F054's
parent-only Redis lifecycle Recipe is published and locked; runtime images
remain the inspected `ad031e2` artifacts. A new full run is required.
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

`.state/azure/provisioning-lifecycle-fixed.json` is the next candidate with the
new locked Redis lifecycle Recipe and inspected images. It was activated after
the latest old-state disk clearance was verified. `deploy-management-lifecycle`
is running. Prove fresh management, then
the complete three-tenant and outage scenario before local implementation.
The bootstrap uses standalone Bicep through the scoped
Azure CLI; application deployment uses Radius and its per-cluster Recipes.

Before any later cloud deployment, set the workflow status to
`Ready for Validation`, run the Azure validation workflow, record its result,
then use the deployment workflow. Bootstrap validation records exact template
and parameter hashes in `.state/azure/validation.json`; changed hashes require
validation again. Never infer deployment success from compilation or Job submission.

## 7. Validation Proof

2026-09-10T03:46:34Z: real bootstrap what-if and ARM validation passed again
with the same exact template and parameter hashes. No bootstrap mutation is
needed. `make check` passed 420 tests and 196 subtests, all 22 Bicep files,
three generated extensions, Ruff, and ShellCheck; 50 live-dependency tests
were explicitly skipped.

`provisioning-lifecycle-fixed.json` matches the locked Recipe manifest and
inspected images. All non-archive image inputs still match their actual image
hashes, and the regenerated extension payload bytes match the inspected
archive members. The reset is independently verified: no child AKS/node groups
or app-group resources remain; management/foundation remain. All five temporary
Reader grants and the reset federation are absent. The old application
namespace and its three exact state disks are removed, with interruption
evidence archived. Fresh management and a new full acceptance run are next;
the interrupted operation is not resumed.

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
