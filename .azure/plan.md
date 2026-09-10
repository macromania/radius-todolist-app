# Three-plane Radius deployment checkpoint

Status: Validated (archived Azure deployment; complete teardown verified).
Fresh admission and existing-tenant functional/outage proof are recorded
separately. Fresh Redis lifecycle and unchanged-resource postconditions are
verified. All child clusters and application resources are removed. Bootstrap
resource/role removal is verified. Local remains open. This checkpoint does not
describe an active Azure deployment; fresh deployment requires new validation.

## Scope and authorization

The user approved implementation, real Azure deployment, automatic parent
commits, and phase/fix verification with rubber-duck and security reviews on
2026-09-09. Two shared tenants, reuse, and immediate-child readiness were
proved before governance automation interrupted isolated onboarding. Failure
evidence was preserved; no interrupted operation was replayed or relabeled.
The controlled reset now has independent Azure absence proof. F054's
parent-only Redis lifecycle Recipe is published and locked, but live tracking
disproved that fix's completeness: implicit tag resources are still tracked.
Runtime images remain the inspected `ad031e2` artifacts. Fresh onboarding
succeeded for all three tenants; F055 export discovery blocked later acceptance.
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
completed and passed HTTPS/authentication and empty-state checks.
`demo-acceptance-lifecycle` failed on F055 after all three provisioning
operations succeeded. Keep its failed evidence and the working tenants. The
reviewed harness-only `verify-existing` run passed current-state and both outage
checks without replaying admission or claiming fresh admission proof. F054's
actual tracking/lifecycle correction and final cleanup remain open before local
implementation.
The bootstrap uses standalone Bicep through the scoped
Azure CLI; application deployment uses Radius and its per-cluster Recipes.

Before any later cloud deployment, set the workflow status to
`Ready for Validation`, run the Azure validation workflow, record its result,
then use the deployment workflow. Bootstrap validation records exact template
and parameter hashes in `.state/azure/validation.json`; changed hashes require
validation again. Never infer deployment success from compilation or Job submission.

## 7. Validation Proof

2026-09-10T14:29:36Z: the unchanged independent `operations/verify-clean.py`
reported `clean`, persisted in `final-cleanup/final-verification.json`.
All owned groups, project roles/assignments, and active tagged resources are
absent. The protected vault is soft-deleted with scheduled purge
2026-09-17T14:14:19Z, not claimed as purged. A transient disagreement between
role-list and exact-GET results was resolved by later full verification, not by
weakening checks. Final `make check`: 522 tests, 245 subtests, 50 explicit
dependency skips, 22 Bicep files, three extensions, Ruff, and ShellCheck.
Independent Azure closeout walkthrough/security reviews were clean.

2026-09-10T13:42:00Z: independent Azure reads verified all four child AKS/node
groups absent and all five app groups empty after the Radius-only executor
completed. At 13:43:05Z, the exact temporary access journal was fully cleared
and baseline grants/federation preserved. The explicit bootstrap provider-only
preview then passed all ownership/custom-role checks, with management as its
only AKS deletion. Its direct walkthrough/security review were clean.
Execution rechecked child/app absence and access revocation; final actual
bootstrap/role deletion remains to be verified before local work begins.

2026-09-10T10:13:38Z: fresh Redis creation, real metadata tagging twice,
idempotence, supported stored tracking, and Radius deletion are verified.
The all-in-one gate stopped only on the CLI's empty-application SecretStore
preflight bug; its failed record remains unchanged. Reviewed native Radius
owner-API cleanup then verified auth/backing-Secret absence, fresh Azure
cache/endpoint/NIC absence, and unchanged existing-resource fingerprints.
No active tenant configuration changed. The remaining Azure step is the
authorized whole-environment teardown, including explicit treatment of old
records that still track the former tag extension.

2026-09-10T07:57:49Z: `uv run --no-sync python
operations/validate-bootstrap.py` passed real ARM what-if and validation with
the unchanged foundation hashes recorded in `.state/azure/validation.json`.
F054's source passed `make check`: 514 tests, 245 subtests, all 22 Bicep files,
three generated extensions, Ruff, and ShellCheck; 50 live-dependency tests
were explicitly skipped. Independent F054 rubber-duck/security reviews were
clean. Current image/Recipe/configuration manifests are archived under
`pre-f054-artifacts/` before building the candidate. No existing Redis record
is to be redeployed or migrated; validate the new Recipe through a separate
fresh resource using the existing isolated-data allocation and Radius identity.
Image-content/SDK inspection and actual create/tag/track/delete proof remain
required before treating F054 as resolved.

The `1b26a76` image builds completed. Actual tokenless AKS inspection matched
23 API and 54 provisioner files/artifacts, confirmed UID 10001 and API
privilege exclusions, and imported Azure Identity 1.25.2/HTTPX 0.28.1 from the
candidate. At 08:09:29Z, a separate read-only Job used the existing
isolated-data Radius workload identity for real token exchange and an ARM
resource-group GET. Candidate image and Recipe references are verified;
fresh NIC tag application, stored tracking, and deletion remain the next gate.

The one-off lifecycle gate's preparation reviews passed. Its first attempts
failed before Recipe creation on a duplicate Radius group argument; suppressed
diagnostics were also corrected. F056/F057 retain the failed payloads and
record fix reviews and 19 gate regressions. The regenerated immutable
`create4` then exposed a case-sensitive group-ID comparison (F058); a scoped
read-only Job confirmed the actual ID. The existing case-insensitive ID helper,
21 gate tests, and both fix reviews now cover that response and foreign-group
refusal. `f054-redis-lifecycle-create5` is running from the inspected image,
with no change to existing tenant applications. Do not treat submission as
lifecycle or teardown proof.

`create5` passed Radius preflight but the read-only baseline found a real
provider response mismatch: Managed Redis returns the verified display name
`Central US`, rather than `centralus`. F059 accepts exactly those two names;
237 focused tests and the direct walkthrough/security review passed. No
foundation or Recipe inputs changed, so the recorded ARM validation remains
applicable. Rebuild and inspect the new committed runtime image before
regenerating the gate; existing tenant images/configuration remain unchanged.

The `31e131a` rebuild and actual AKS image inspection are complete: 23 API and
54 provisioner source/artifact hashes, UID, package imports, API exclusions,
and extension payloads passed. The source-pinned `create6` lifecycle Job is
running against a separate candidate configuration. Existing tenant workloads
remain on their previous verified images; a completed lifecycle/absence result
is still required.

2026-09-10T07:03:57Z: `verify-existing` run
`80a3a427f3874169b6604800dd05580e`, source `3c66555`, passed functional,
topology/isolation, authentication, timeline/idempotency, and both real parent
outages. Both fault records confirm restoration within 10 seconds; overall
report/catch-up recovery remained under 30 seconds. Independent live AKS reads
at 07:08:24Z found no fault policies and all shared control/data workloads
available. Both functional-phase reviews were clean. `FINDINGS.md` records
the evidence scopes/hashes; the earlier fresh run remains failed.

The F055 harness-only change passed 185 tests and 211 subtests, Ruff, whitespace
checks, and independent rubber-duck/security reviews. No bootstrap inputs,
runtime images, or deployed applications changed. The existing ARM validation
below remains applicable; this harness Job does not redeploy infrastructure.

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
