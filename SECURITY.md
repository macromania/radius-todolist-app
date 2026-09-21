# Security policy

This is an experimental, trusted-operator demonstration. Only the current `main`
branch is maintained; no production security or response-time guarantee is made.
Use synthetic data and a dedicated deployment.

## Report a vulnerability privately

Use [GitHub private vulnerability reporting](https://github.com/macromania/radius-todolist-app/security/advisories/new)
when the repository's **Security** tab offers **Report a vulnerability**.
Include the affected revision, a minimal reproduction, expected access boundaries,
and impact. Omit real credentials and use synthetic data.

Private vulnerability reporting must be enabled and verified when this repository
becomes public. GitHub does not provide this feature for private repositories.
If the reporting form is unavailable, ask a maintainer for a private channel
without including vulnerability details. Do not disclose details, credentials,
or exploit logs in a public issue or pull request.

If credentials have been exposed, revoke or rotate them with their owner.
Removing a file from the latest commit does not remove it from Git history.

## Intended boundaries

Per-plane demo keys do not provide production tenant authentication or cost-abuse
protection. Azure bootstrap requires a trusted operator with substantial
subscription permissions. Local management Radius has Docker daemon authority.
Names and labels are ownership controls, not protection from a hostile operator.
Azure resources retain `SecurityControl=Ignore`, an organization-specific tag.
Review its effect under your subscription's policies before deploying; the demo
does not establish whether a policy uses that tag to exclude resources.

APIs must not gain operator tools or deployment credentials. Data API requests
use their own key, local ConfigMaps, and Redis without parent-database access or
namespace Secret access. Azure database connections use verified TLS; local
non-TLS connections are explicitly internal.

Interrupted infrastructure work is not automatically replayed. Preserve owner
checks, locks, and fault journals when diagnosing problems. Never expose a
database or broaden a role assignment to bypass a failed demonstration.

## Before publication

Review the history, branches, tags, logs, releases, and artifacts that will become
accessible. Use redacted findings and revoke any exposed credentials before
publication. Confirm redistribution rights and dependency notices. Keep fork
pull-request checks cloud-free, without deployment secrets or privileged
self-hosted runners. Repository visibility and image publication are separate
maintainer decisions.
